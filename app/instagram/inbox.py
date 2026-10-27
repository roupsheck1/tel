"""دریافت پیام‌های دایرکت اینستاگرام — منبع اصلی درخواست‌ها (بخش ۲.۱ سند).

جریان: پایش دوره‌ای صندوق (شامل درخواست‌های pending)، تشخیص پیام‌های جدید،
تأیید خودکار درخواست‌های در انتظار، و سپس:
- کاربر متصل نیست → ارسال کد اتصال ۶ رقمی
- کاربر متصل است → استخراج پست/ریلز و تحویل به موتور دانلود
"""
from __future__ import annotations

import json
import logging
import random
import re
import threading
import time

from ..database.repositories import LinksRepo
from ..services.download_engine import DownloadEngine
from .client import InstagramClient

log = logging.getLogger("instagram.inbox")

LINK_RE = re.compile(
    r"https?://(?:www\.)?instagram\.com/(?:[\w.]+/)?(?:p|reel|reels|tv)/([\w-]+)",
    re.IGNORECASE,
)


def _dump_message(m) -> str:
    """تبدیل کل محتوای پیام به متن برای جست‌وجوی لینک."""
    try:
        data = m.model_dump(mode="json")
    except Exception:
        try:
            data = m.dict()
        except Exception:
            return str(m)
    try:
        return json.dumps(data, default=str, ensure_ascii=False)
    except Exception:
        return str(data)


def extract_post(m) -> tuple[str, str, str] | None:
    """یافتن پست/ریلز در پیام؛ خروجی (url, shortcode, kind)."""
    # ۱) پست/ریلز اشتراکی با دکمه Share
    for field, kind in (("media_share", "post"), ("clip", "reel"), ("reel_share", "reel")):
        obj = getattr(m, field, None)
        code = getattr(obj, "code", None) if obj is not None else None
        if code:
            return f"https://www.instagram.com/{'reel' if kind == 'reel' else 'p'}/{code}/", code, kind
    # ۲) لینک داخل متن
    text = getattr(m, "text", None) or ""
    mt = LINK_RE.search(text)
    if mt:
        kind = "reel" if "reel" in mt.group(0) else "post"
        return mt.group(0), mt.group(1), kind
    # ۳) جست‌وجو در کل محتوا (xma، link و ...)
    mt = LINK_RE.search(_dump_message(m))
    if mt:
        kind = "reel" if "reel" in mt.group(0) else "post"
        return mt.group(0), mt.group(1), kind
    return None


class InstagramInbox:
    """پایشگر دایرکت با ردیابی پیام‌های دیده‌شده در پایگاه داده."""

    def __init__(self, client: InstagramClient, links: LinksRepo, engine: DownloadEngine,
                 loop_holder, poll_min: int = 30, poll_max: int = 60,
                 code_ttl: int = 1200, code_prefix_message: str = ""):
        self.client = client
        self.links = links
        self.engine = engine
        self.loop_holder = loop_holder  # نگهدارنده event loop تلگرام
        self.poll_min, self.poll_max = max(5, poll_min), max(poll_min + 1, poll_max)
        self.code_ttl = code_ttl
        self.bot_ig_username = ""
        self.last_scan_at: int | None = None
        self.last_success_at: int | None = None
        self.last_error: str | None = None
        self.first_scan_done = False
        self._stop = threading.Event()

    # ---------- حلقه پایش ----------
    def start_background(self) -> threading.Thread:
        t = threading.Thread(target=self._loop, name="ig-inbox", daemon=True)
        t.start()
        return t

    def _loop(self) -> None:
        log.info("پایش دایرکت آغاز شد (هر %s-%s ثانیه)", self.poll_min, self.poll_max)
        while not self._stop.is_set():
            try:
                self.scan_once()
                self.last_success_at = int(time.time())
            except Exception as e:
                self.last_error = str(e)
                log.exception("خطای پایش دایرکت: %s", e)
                time.sleep(300)  # عقب‌نشینی در خطا
            time.sleep(random.randint(self.poll_min, self.poll_max))

    def stop(self) -> None:
        self._stop.set()

    def scan_once(self) -> int:
        """یک دور کامل بررسی صندوق؛ خروجی تعداد پیام جدید پردازش‌شده."""
        # نکته: امضای instagrapi مقدار «amount» و سپس «selected_filter» متنی است؛
        # «(20, 20)» باعث خطای ValueError می‌شد و کل پایش از کار می‌افتاد.
        threads = self.client.with_client(lambda cl: cl.direct_threads(20))
        items = [(t, False) for t in threads]
        try:
            pending = self.client.with_client(
                lambda cl: getattr(cl, "direct_requests", lambda *a, **k: [])(20)
                if hasattr(cl, "direct_requests") else cl.direct_pending_inbox(20)
            )
            items += [(t, True) for t in pending]
        except Exception as e:
            log.debug("خواندن درخواست‌های pending ناموفق: %s", e)

        me = str(self.client.cl.user_id)
        processed = 0
        for thread, is_pending in items:
            try:
                processed += self._handle_thread(thread, is_pending, me)
            except Exception as e:
                self.last_error = str(e)
                log.exception("خطای پردازش گفتگو: %s", e)
        self.last_scan_at = int(time.time())
        # نخستین پایش فقط برای خط‌گذاری پایه (baseline) است؛
        # بدون True شدن این پرچم، هیچ پیام جدیدی هرگز پردازش نمی‌شد.
        self.first_scan_done = True
        return processed

    def _handle_thread(self, thread, is_pending: bool, me: str) -> int:
        if not thread.messages:
            return 0
        # فقط گفتگوهای یک‌نفره (بدون گروه)
        # در گفتگوهای pending فهرست users ممکن است خالی باشد و فرستنده در inviter باشد.
        users = list(getattr(thread, "users", None) or [])
        if not users:
            inviter = getattr(thread, "inviter", None)
            if inviter is not None:
                users = [inviter]
        if len(users) != 1:
            return 0
        seen = self._seen_row(str(thread.id))
        last_seen = seen
        new_msgs = []
        for m in thread.messages[:20]:
            if str(m.id) == last_seen:
                break
            if str(getattr(m, "user_id", "")) != me:
                new_msgs.append(m)
        # به‌روزرسانی نشانگر دیده‌شده
        self._mark_seen(str(thread.id), str(thread.messages[0].id))
        if not new_msgs or not self.first_scan_done:
            return 0
        username = users[0].username.lower()
        if is_pending:
            try:
                self.client.with_client(lambda cl, tid=thread.id: cl.direct_approve_participant_requests(
                    tid, len(new_msgs)) if hasattr(cl, "direct_approve_participant_requests")
                    else cl.direct_request_approve(tid))
            except Exception as e:
                log.warning("تأیید درخواست دایرکت ناموفق: %s", e)
        count = 0
        for m in reversed(new_msgs):  # قدیمی‌ترین اول
            if self._handle_message(thread, m, username):
                count += 1
        return count

    # ---------- منطق پیام ----------
    def _handle_message(self, thread, m, username: str) -> bool:
        link = self.links.find_by_ig(username)
        if link is None:
            # کاربر متصل نیست → کد اتصال (بخش ۵.۲)
            code = self.links.create_code(username, self.code_ttl)
            text = (f"کد اتصال شما: {code}\n"
                    f"تا {self.code_ttl // 60} دقیقه فرصت داری این کد را برای ربات تلگرام بفرستی.")
            self._send_dm(thread.id, text)
            return True
        post = extract_post(m)
        if not post:
            text = getattr(m, "text", None) or ""
            if text.strip():
                self._send_dm(
                    thread.id,
                    "حسابت وصل است ✅\nپست یا ریلز را همین‌جا بفرست (Share) یا لینکش را بفرست "
                    "تا در تلگرام برایت دانلود شود."
                )
            return False
        url, shortcode, kind = post
        log.info("درخواست جدید از @%s: %s", username, url)
        # ثبت آخرین فعالیت (بخش ۵.۳)
        self.links.touch_activity(link["telegram_id"])
        # تحویل به موتور دانلود در حلقه رویداد تلگرام
        self.loop_holder.call_soon_threadsafe(
            lambda: self.loop_holder.create_task(self._submit(link["telegram_id"], url, shortcode, kind))
        )
        return True

    async def _submit(self, telegram_id: int, url: str, shortcode: str, kind: str) -> None:
        """ثبت درخواست در موتور؛ در صورت رد، دلیل به کاربر در تلگرام اعلام می‌شود."""
        ok, result = await self.engine.submit(telegram_id, url, shortcode, kind)
        if not ok:
            log.info("درخواست %s از %s رد شد: %s", shortcode, telegram_id, result)
            await self.engine._safe_notify(telegram_id, f"❌ درخواست شما پذیرفته نشد:\n{result}")
        else:
            log.info("درخواست %s با شناسه %s ثبت شد", shortcode, result)

    # ---------- ابزار ----------
    def _send_dm(self, thread_id: str, text: str) -> None:
        try:
            # نکته: آرگومان دومِ موقعیتیِ direct_send همان user_ids است؛
            # باید thread_ids به‌صورت کلیدواژه پاس شود وگرنه پیام به مقصد اشتباه می‌رود.
            self.client.with_client(lambda cl: cl.direct_send(text, thread_ids=[thread_id]))
        except Exception as e:
            log.warning("ارسال پیام دایرکت ناموفق: %s", e)

    def _seen_row(self, thread_id: str) -> str | None:
        row = self.engine.d.downloads.db.query_one(
            "SELECT last_msg_id FROM ig_seen WHERE thread_id=?", (thread_id,)
        )
        return row["last_msg_id"] if row else None

    def _mark_seen(self, thread_id: str, msg_id: str) -> None:
        self.engine.d.downloads.db.execute(
            "INSERT INTO ig_seen (thread_id, last_msg_id, updated_at) VALUES (?,?,?) "
            "ON CONFLICT(thread_id) DO UPDATE SET last_msg_id=excluded.last_msg_id,"
            " updated_at=excluded.updated_at",
            (thread_id, msg_id, int(time.time())),
        )

    # ---------- سلامت (بخش ۲۰.۱) ----------
    def health(self) -> dict:
        return {
            "ready": self.client.is_ready,
            "last_scan_at": self.last_scan_at,
            "last_success_at": self.last_success_at,
            "last_error": self.last_error,
        }
