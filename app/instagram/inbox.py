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
STORY_URL_RE = re.compile(
    r"https?://(?:www\.)?instagram\.com/stories/[\w.]+/(\d+)",
    re.IGNORECASE,
)
DEEPLINK_ID_RE = re.compile(r"(?:media_id|story_id|id)=(\d{8,})")

# انواع پیام بدون محتوای قابل استخراج (برای این‌ها پیام راهنما نمی‌فرستیم)
_PLAIN_TYPES = {"text", "like", "placeholder", "animated_media", "action_log",
                "video_call_action", "privacy_note", "reel_share_reaction"}


def _pk_to_code(pk) -> str:
    """تبدیل pk عددی رسانه به shortcode (همان الگوریتم InstagramIdCodec رسمی)."""
    num = int(pk)
    if num == 0:
        return "A"
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    arr = []
    while num:
        num, rem = divmod(num, 64)
        arr.append(alphabet[rem])
    return "".join(reversed(arr))


def _post_of_code(code: str, kind: str = "post"):
    path = "reel" if kind == "reel" else "p"
    return f"https://www.instagram.com/{path}/{code}/", code, kind


def _from_xma(x):
    """استخراج از قالب جدید xma (خروجی xma_clip/xma_media_share/generic_xma)."""
    url = str(getattr(x, "video_url", "") or "")
    if url:
        mt = STORY_URL_RE.search(url)
        if mt:
            return url, mt.group(1), "story"
        mt = LINK_RE.search(url)
        if mt:
            kind = "reel" if "reel" in mt.group(0) else "post"
            return mt.group(0), mt.group(1), kind
        mt = DEEPLINK_ID_RE.search(url)
        if mt:
            if "story" in url:
                return url, mt.group(1), "story"
            return _post_of_code(_pk_to_code(mt.group(1)))
    fbid = getattr(x, "preview_media_fbid", None)
    if fbid and str(fbid).isdigit():
        return _post_of_code(_pk_to_code(fbid))
    return None


def _from_xma_raw(it: dict, mode: str):
    """استخراج از دیکته خام raw_xma (مثل xma_story_share و xma_profile)."""
    target = str(it.get("target_url") or "")
    if target:
        mt = STORY_URL_RE.search(target)
        if mt:
            return target, mt.group(1), "story"
        mt = LINK_RE.search(target)
        if mt:
            kind = "reel" if "reel" in mt.group(0) else "post"
            return mt.group(0), mt.group(1), kind
        mt = DEEPLINK_ID_RE.search(target)
        if mt:
            if mode == "story" or "story" in target:
                return target, mt.group(1), "story"
            return _post_of_code(_pk_to_code(mt.group(1)))
    fbid = it.get("preview_media_fbid")
    if fbid and str(fbid).isdigit() and mode in ("story", "media"):
        # برای پروفایل، pk احتمالی کاربر است و نباید به پست تبدیل شود
        if mode == "story":
            return f"https://www.instagram.com/stories/user/{fbid}/", str(fbid), "story"
        return _post_of_code(_pk_to_code(fbid))
    return None


def _story_of_share(d: dict):
    """استخراج استوری از story_share کلاسیک؛ خروجی (url, media_id, 'story')."""
    media_id = str(d.get("media_id") or "")
    if "_" not in media_id:
        reel_id = str(d.get("reel_id") or "")
        story_mid = str(d.get("story_media_id") or "")
        if reel_id and story_mid:
            media_id = f"{reel_id}_{story_mid}"
        elif not media_id.isdigit():
            media_id = ""
    if not media_id:
        return None
    parts = media_id.split("_")
    if len(parts) == 2:
        url = f"https://www.instagram.com/stories/{parts[0]}/{parts[1]}/"
    else:
        url = f"https://www.instagram.com/stories/user/{parts[0]}/"
    return url, media_id, "story"


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
    """یافتن پست/ریلز/استوری در پیام؛ خروجی (url, shortcode, kind).

    قالب‌های پشتیبانی‌شده:
    - media_share / clip: پست و ریلز کلاسیک (شیء Media با code)
    - reel_share / felix_share: دیکته با media.code
    - xma_share / generic_xma: قالب جدید دکمه Share (xma_clip و xma_media_share)
    - story_share: استوری کلاسیک (media_id = owner_pk_media_pk)
    - visual_media: عکس/ویدیوی ارسالی مستقیم (media_id = pk)
    - raw_xma: دیکته خام xma_story_share و بقیه
    - لینک داخل متن و کل محتوای سریال‌شده پیام
    """
    # ۱) پست/ریلز کلاسیک: media_share و clip (شیء Media با code)
    for field, kind in (("media_share", "post"), ("clip", "reel")):
        obj = getattr(m, field, None)
        code = getattr(obj, "code", None)
        if code:
            real_kind = "reel" if getattr(obj, "media_type", 1) == 2 else kind
            return _post_of_code(code, real_kind)
    # ۲) reel_share و felix_share (دیکته با media.code)
    for field, kind in (("reel_share", "reel"), ("felix_share", "post")):
        d = getattr(m, field, None)
        if isinstance(d, dict):
            media = d.get("media")
            code = media.get("code") if isinstance(media, dict) else None
            if code:
                return _post_of_code(code, kind)
    # ۳) xma_share و generic_xma — قالب جدید دکمه Share (پست/ریلز/کلیپ)
    for field in ("xma_share", "generic_xma"):
        val = getattr(m, field, None)
        items = val if isinstance(val, list) else ([val] if val else [])
        for x in items:
            found = _from_xma(x)
            if found:
                return found
    # ۴) استوری: story_share کلاسیک
    d = getattr(m, "story_share", None)
    if isinstance(d, dict):
        found = _story_of_share(d)
        if found:
            return found
    # ۵) visual_media: عکس/ویدیوی ارسالی مستقیم در چت (media_id = pk)
    vm_media = getattr(getattr(m, "visual_media", None), "media", None)
    vm_mid = getattr(vm_media, "media_id", None)
    if vm_mid:
        return _post_of_code(_pk_to_code(vm_mid))
    # ۶) لینک داخل متن
    text = getattr(m, "text", None) or ""
    mt = STORY_URL_RE.search(text)
    if mt:
        return text.strip(), mt.group(1), "story"
    mt = LINK_RE.search(text)
    if mt:
        kind = "reel" if "reel" in mt.group(0) else "post"
        return mt.group(0), mt.group(1), kind
    # ۷) جست‌وجو در کل محتوای سریال‌شده (xma، link و ...)
    dump = _dump_message(m)
    mt = STORY_URL_RE.search(dump)
    if mt:
        return mt.group(0), mt.group(1), "story"
    mt = LINK_RE.search(dump)
    if mt:
        kind = "reel" if "reel" in mt.group(0) else "post"
        return mt.group(0), mt.group(1), kind
    # ۸) raw_xma پشتیبان (مثل xma_story_share خام که به شیء تبدیل نشده)
    raw = getattr(m, "raw_xma", None)
    if isinstance(raw, dict):
        for key, val in raw.items():
            for it in (val if isinstance(val, list) else [val] if val else []):
                if isinstance(it, dict):
                    mode = "story" if "story" in key else ("profile" if "profile" in key else "media")
                    found = _from_xma_raw(it, mode)
                    if found:
                        return found
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
        item_type = str(getattr(m, "item_type", "") or "")
        link = self.links.find_by_ig(username)
        if link is None:
            # کاربر متصل نیست → کد اتصال (بخش ۵.۲)
            code = self.links.create_code(username, self.code_ttl)
            text = (f"کد اتصال شما: {code}\n"
                    f"تا {self.code_ttl // 60} دقیقه فرصت داری همین کد را برای ربات تلگرام "
                    "بفرستی (فقط همین کد معتبر است؛ کدهای قبلی باطل شده‌اند).")
            self._send_dm(thread.id, text)
            return True
        post = extract_post(m)
        if not post:
            text = getattr(m, "text", None) or ""
            log.info("پیام از @%s قابل استخراج نبود (نوع: %s، متن: %s)",
                     username, item_type or "?", (text or "").replace("\n", " ")[:60])
            if text.strip():
                self._send_dm(
                    thread.id,
                    "حسابت وصل است ✅\nپست یا ریلز را همین‌جا بفرست (Share) یا لینکش را بفرست "
                    "تا در تلگرام برایت دانلود شود."
                )
            elif item_type and item_type not in _PLAIN_TYPES:
                # پیامِ رسانه‌ایِ ناشناخته → راهنمای صریح به‌جای سکوت
                self._send_dm(
                    thread.id,
                    "این نوع پیام را نتونستم بخونم 🤔\n"
                    "بهترین راه: لینک پست/ریلز/استوری را به‌صورت متن همین‌جا بفرست."
                )
            return False
        url, shortcode, kind = post
        log.info("درخواست جدید از @%s: %s [نوع: %s]", username, url, kind)
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
