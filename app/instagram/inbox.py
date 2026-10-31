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
from datetime import datetime

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
    """استخراج استوری از story_share کلاسیک؛ خروجی (url, media_id, 'story').

    نکته: در URL استوری باید شناسهٔ استوری (story_media_id، عدد بزرگ) بیاید،
    نه pk خام رسانه؛ pk خام برای story_download اینستاگراپی است.
    """
    media_id = str(d.get("media_id") or "")
    reel_id = str(d.get("reel_id") or "")
    story_mid = str(d.get("story_media_id") or "")
    if "_" not in media_id:
        if reel_id and story_mid:
            media_id = f"{reel_id}_{story_mid}"
        elif not media_id.isdigit():
            media_id = ""
    if not media_id:
        return None
    parts = media_id.split("_")
    owner = parts[0]
    # شناسهٔ URL: story_media_id (در صورت وجود) وگرنه بخش دوم media_id
    url_id = story_mid or (parts[1] if len(parts) == 2 else parts[0])
    url = f"https://www.instagram.com/stories/{owner}/{url_id}/"
    return url, media_id, "story"


def _msg_ts(m) -> float:
    """زمان پیام به ثانیهٔ epoch — instagrapi گاهی datetime می‌دهد (نه عدد).

    بدون این نرمال‌سازی، مقایسهٔ «datetime >= int» استثنا می‌دهد و پردازش
    ترد نیمه‌کاره می‌ماند (باگ راند پنجم).
    """
    ts = getattr(m, "timestamp", 0) or 0
    if isinstance(ts, datetime):
        return ts.timestamp()
    try:
        return float(ts)
    except (TypeError, ValueError):
        return 0.0


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
    """پایشگر دایرکت با ردیابی پیام‌های دیده‌شده در پایگاه داده.

    سه صندوق پایش می‌شود:
    - inbox: گفتگوهای عادی
    - pending: درخواست‌های پیام (message requests)
    - spam: درخواست‌های مخفی/فیلترشدهٔ اینستاگرام — بدون این صندوق، پیام
      اکانت‌های جدید که اینستاگرام به‌جای «درخواست‌ها» به «مخفی» می‌فرستد
      هرگز دیده نمی‌شود و کاربر کد اتصال نمی‌گیرد.
    """

    # حداکثر عمر پیامی که در نخستین اسکن (baseline) هنوز پردازش می‌شود؛
    # پیام‌های قدیمی‌تر فقط خط‌گذاری می‌شوند (بازیابی پس از restart بدون
    # از دست رفتن درخواست‌های تازه).
    BASELINE_MAX_AGE = 6 * 3600

    def __init__(self, client: InstagramClient, links: LinksRepo, engine: DownloadEngine,
                 loop_holder, poll_min: int = 30, poll_max: int = 60,
                 code_ttl: int = 1200, code_prefix_message: str = "",
                 fast_poll_min: int = 6, fast_poll_max: int = 9,
                 fast_window: int = 600):
        self.client = client
        self.links = links
        self.engine = engine
        self.loop_holder = loop_holder  # نگهدارنده event loop تلگرام
        self.poll_min, self.poll_max = max(5, poll_min), max(poll_min + 1, poll_max)
        self.code_ttl = code_ttl
        self.fast_poll_min = max(3, fast_poll_min)
        self.fast_poll_max = max(fast_poll_min + 1, fast_poll_max)
        self.fast_window = max(60, fast_window)
        self.bot_ig_username = ""
        self.last_scan_at: int | None = None
        self.last_success_at: int | None = None
        self.last_error: str | None = None
        self.first_scan_done = False
        self._stop = threading.Event()
        self._fast_until = 0.0          # پایان پنجرهٔ پول سریع
        self._uid_cache: dict[str, tuple[str | None, float]] = {}  # uid → (username, fetched_at)

    # ---------- پول سریع ----------
    def arm_fast_poll(self, seconds: int | None = None) -> None:
        """فعال‌سازی پنجرهٔ پول سریع.

        وقتی کاربر در تلگرام «اتصال اینستاگرام» را می‌زند یا کدش رد می‌شود،
        به‌زودی در اینستاگرام پیام می‌دهد؛ در این پنجره صندوق هر چند ثانیه
        بررسی می‌شود تا کد سریع برسد.
        """
        self._fast_until = time.time() + (seconds or self.fast_window)
        log.info("پول سریع دایرکت تا %s ثانیهٔ دیگر فعال شد", int(self._fast_until - time.time()))

    @property
    def fast_poll_active(self) -> bool:
        return time.time() < self._fast_until

    def _sleep_interval(self) -> int:
        """فاصلهٔ تا اسکن بعدی: سریع در پنجرهٔ فعال، عادی در حالت عادی."""
        if self.fast_poll_active:
            return random.randint(self.fast_poll_min, self.fast_poll_max)
        return random.randint(self.poll_min, self.poll_max)

    # ---------- حلقه پایش ----------
    def start_background(self) -> threading.Thread:
        t = threading.Thread(target=self._loop, name="ig-inbox", daemon=True)
        t.start()
        return t

    def _loop(self) -> None:
        log.info("پایش دایرکت آغاز شد (هر %s-%s ثانیه؛ پول سریع: %s-%s ثانیه)",
                 self.poll_min, self.poll_max, self.fast_poll_min, self.fast_poll_max)
        while not self._stop.is_set():
            try:
                self.scan_once()
                self.last_success_at = int(time.time())
            except Exception as e:
                self.last_error = str(e)
                log.exception("خطای پایش دایرکت: %s", e)
                time.sleep(300)  # عقب‌نشینی در خطا
            time.sleep(self._sleep_interval())

    def stop(self) -> None:
        self._stop.set()

    def scan_once(self) -> int:
        """یک دور کامل بررسی هر سه صندوق؛ خروجی تعداد پیام جدید پردازش‌شده."""
        me = str(self.client.with_client(lambda cl: cl.user_id))
        processed = 0
        # نکته: امضای instagrapi مقدار «amount» و سپس «selected_filter» متنی است؛
        # «(20, 20)» باعث خطای ValueError می‌شد و کل پایش از کار می‌افتاد.
        boxes = (
            (lambda: self.client.with_client(lambda cl: cl.direct_threads(20)), False, "inbox"),
            (lambda: self.client.with_client(
                lambda cl: getattr(cl, "direct_requests", lambda *a, **k: [])(20)
                if hasattr(cl, "direct_requests") else cl.direct_pending_inbox(20)), True, "pending"),
            (lambda: self.client.with_client(
                lambda cl: getattr(cl, "direct_spam_inbox", lambda *a, **k: [])(20)
                if hasattr(cl, "direct_spam_inbox") else []), True, "spam"),
        )
        for fetch, is_pending, box in boxes:
            try:
                threads = fetch()
            except Exception as e:
                # خطای یک صندوق نباید کل پایش را متوقف کند؛ صندوق‌های بعدی
                # در همین دور و در دور بعد دوباره امتحان می‌شوند.
                self.last_error = str(e)
                log.warning("خواندن صندوق %s ناموفق: %s", box, e)
                continue
            for thread in threads:
                try:
                    processed += self._handle_thread(thread, is_pending, me, box)
                except Exception as e:
                    self.last_error = str(e)
                    log.exception("خطای پردازش گفتگو [%s]: %s", box, e)
        self.last_scan_at = int(time.time())
        # نخستین پایش علاوه بر خط‌گذاری پایه، پیام‌های تازه را هم پردازش
        # می‌کند (BASELINE_MAX_AGE)؛ بدون True شدن این پرچم هیچ پیامی
        # پردازش نمی‌شد.
        self.first_scan_done = True
        return processed

    # ---------- تشخیص طرف مقابل ----------
    def _thread_counterpart(self, thread, me: str) -> tuple[str, str | None] | None:
        """یوزرنیم (و در صورت امکان pk) طرف مقابل گفتگو.

        برای درخواست‌های pending/spam فهرست «users» اغلب خالی است؛ در آن
        حالت از inviter و در نهایت از فرستندهٔ واقعی پیام‌ها (با یک
        user_info کش‌شده) یوزرنیم پیدا می‌شود.
        """
        users = list(getattr(thread, "users", None) or [])
        if len(users) == 1:
            u = getattr(users[0], "username", None)
            pk = str(getattr(users[0], "pk", "") or "")
            if u:
                return u.lower(), pk or None
        elif len(users) > 1:
            return None  # گفتگوی گروهی پشتیبانی نمی‌شود
        inviter = getattr(thread, "inviter", None)
        if inviter is not None:
            u = getattr(inviter, "username", None)
            pk = str(getattr(inviter, "pk", "") or "")
            if u and pk != me:
                return u.lower(), pk or None
        # آخرین لایه: فرستندهٔ واقعی پیام‌ها (خود ربات نیست)
        senders = set()
        for m in (getattr(thread, "messages", None) or [])[:20]:
            uid = str(getattr(m, "user_id", "") or "")
            if uid and uid != me:
                senders.add(uid)
        if len(senders) == 1:
            uid = next(iter(senders))
            username = self._username_of_uid(uid)
            if username:
                return username, uid
        return None

    def _username_of_uid(self, uid: str) -> str | None:
        """یوزرنیم یک uid با کش؛ نتایج منفی ۱۰ دقیقه اعتبار دارند."""
        now = time.time()
        hit = self._uid_cache.get(uid)
        if hit:
            username, fetched_at = hit
            if username or now - fetched_at < 600:
                return username
        try:
            info = self.client.with_client(lambda cl: cl.user_info(int(uid)))
            username = (getattr(info, "username", None) or "").lower() or None
        except Exception as e:
            log.debug("یافتن نام‌کاربری uid=%s ناموفق: %s", uid, e)
            username = None
        self._uid_cache[uid] = (username, now)
        return username

    def _approve_thread(self, thread, box: str) -> bool:
        """تأیید درخواست دایرکت (pending/spam) تا پاسخ قابل ارسال شود."""
        try:
            ok = bool(self.client.with_client(
                lambda cl: cl.direct_request_approve(int(thread.id))))
            log.info("درخواست دایرکت [%s] %s تأیید شد: %s",
                     box, str(thread.id)[-6:], ok)
            return ok
        except Exception as e:
            log.warning("تأیید درخواست دایرکت [%s] %s ناموفق (ادامه با ارسال مستقیم): %s",
                        box, str(thread.id)[-6:], e)
            return False

    def _handle_thread(self, thread, is_pending: bool, me: str, box: str = "inbox") -> int:
        if not thread.messages:
            return 0
        seen = self._seen_row(str(thread.id))
        new_msgs = []
        for m in thread.messages[:20]:
            if str(m.id) == (seen or ""):
                break
            if str(getattr(m, "user_id", "")) != me:
                new_msgs.append(m)
        if not new_msgs:
            # فقط نشانگر را جلو می‌بریم (مثلاً پیام‌های خودِ ربات)
            self._mark_seen(str(thread.id), str(thread.messages[0].id))
            return 0
        users = list(getattr(thread, "users", None) or [])
        if len(users) > 1:
            # گروه: عمداً پشتیبانی نمی‌شود؛ نشانگر جلو می‌رود تا هر بار
            # دوباره ارزیابی نشود.
            self._mark_seen(str(thread.id), str(thread.messages[0].id))
            return 0
        counterpart = self._thread_counterpart(thread, me)
        if counterpart is None:
            # طرف مقابل قابل تشخیص نیست؛ نشانگر را جلو نمی‌بریم تا بعد از
            # رفع مشکل (مثلاً با به‌روزرسانی بعدی) دوباره بررسی شود.
            log.debug("گفتگو [%s] %s بدون طرف مقابل قابل تشخیص — فعلاً رد شد",
                      box, str(thread.id)[-6:])
            return 0
        username, user_pk = counterpart
        first_scan = not self.first_scan_done
        # به‌روزرسانی نشانگر دیده‌شده
        self._mark_seen(str(thread.id), str(thread.messages[0].id))
        if first_scan:
            # در نخستین اسکن فقط پیام‌های تازه پردازش می‌شوند؛ پیام‌های
            # قدیمی فقط خط‌گذاری می‌شوند (بازیابی بدون بازپخش تاریخ قدیمی).
            cutoff = time.time() - self.BASELINE_MAX_AGE
            fresh = [m for m in new_msgs if _msg_ts(m) >= cutoff]
            if not fresh:
                return 0
            new_msgs = fresh
        if is_pending:
            self._approve_thread(thread, box)
        count = 0
        for m in reversed(new_msgs):  # قدیمی‌ترین اول
            if self._handle_message(thread, m, username, user_pk):
                count += 1
        return count

    # ---------- منطق پیام ----------
    def _handle_message(self, thread, m, username: str, user_pk: str | None = None) -> bool:
        item_type = str(getattr(m, "item_type", "") or "")
        link = self.links.find_by_ig(username)
        if link is None:
            # کاربر متصل نیست → کد اتصال (بخش ۵.۲)
            code = self.links.create_code(username, self.code_ttl)
            text = (f"کد اتصال شما: {code}\n"
                    f"تا {self.code_ttl // 60} دقیقه فرصت داری همین کد را برای ربات تلگرام "
                    "بفرستی (فقط همین کد معتبر است؛ کدهای قبلی باطل شده‌اند).")
            sent = self._send_dm(thread.id, text, user_pk)
            if sent:
                log.info("کد اتصال برای @%s در دایرکت ارسال شد ✅", username)
            else:
                log.warning("کد اتصال برای @%s ساخته شد ولی ارسال آن ناموفق بود ❌ "
                            "(کاربر با پیام دوباره می‌تواند آن را دوباره بگیرد)", username)
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
                    "تا در تلگرام برایت دانلود شود.", user_pk,
                )
            elif item_type and item_type not in _PLAIN_TYPES:
                # پیامِ رسانه‌ایِ ناشناخته → راهنمای صریح به‌جای سکوت
                self._send_dm(
                    thread.id,
                    "این نوع پیام را نتونستم بخونم 🤔\n"
                    "بهترین راه: لینک پست/ریلز/استوری را به‌صورت متن همین‌جا بفرست.", user_pk,
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
        """ثبت درخواست در موتور؛ نتیجه با شناسه پیگیری به کاربر اعلام می‌شود.

        شناسه پیگیری (بخش ۵۶ سند ۲): کاربر می‌تواند شناسه را به پشتیبانی بدهد
        و مدیر بدون جست‌وجو در لاگ‌ها مسیر درخواست را ببیند.
        """
        ok, result = await self.engine.submit(telegram_id, url, shortcode, kind)
        if not ok:
            log.info("درخواست %s از %s رد شد: %s", shortcode, telegram_id, result)
            await self.engine._safe_notify(telegram_id, f"❌ درخواست شما پذیرفته نشد:\n{result}")
        else:
            log.info("درخواست %s با شناسه %s ثبت شد", shortcode, result)
            # اعلان ورود به صف — قابل خاموش‌کردن از پنل (§۳۱ سند ۲)
            if self.engine.d.settings.get("notify.on_queue", True):
                await self.engine._safe_notify(
                    telegram_id,
                    "📥 درخواست شما ثبت شد و در صف پردازش است.\n"
                    f"🔖 شناسه پیگیری: <code>#{result}</code>\n"
                    "فایل به‌محض آماده‌شدن همین‌جا ارسال می‌شود.")

    # ---------- ابزار ----------
    def _send_dm(self, thread_id: str, text: str, user_pk: str | None = None) -> bool:
        """ارسال پیام دایرکت؛ در صورت شکست، ارسال مستقیم به کاربر (user_ids).

        برای درخواست‌های تأییدنشده گاهی ارسال با thread_ids خطا می‌دهد؛
        مسیر جایگزین با user_ids معمولاً جواب می‌دهد.
        """
        try:
            # نکته: آرگومان دومِ موقعیتیِ direct_send همان user_ids است؛
            # باید thread_ids به‌صورت کلیدواژه پاس شود وگرنه پیام به مقصد اشتباه می‌رود.
            self.client.with_client(
                lambda cl: cl.direct_send(text, thread_ids=[int(thread_id)]))
            log.info("پیام دایرکت به گفتگو …%s ارسال شد", str(thread_id)[-6:])
            return True
        except Exception as e1:
            if user_pk:
                try:
                    self.client.with_client(
                        lambda cl: cl.direct_send(text, user_ids=[int(user_pk)]))
                    log.warning("ارسال با thread ناموفق بود (%s) — با user_id ارسال شد", e1)
                    return True
                except Exception as e2:
                    log.warning("ارسال پیام دایرکت ناموفق (thread: %s | user: %s)", e1, e2)
            else:
                log.warning("ارسال پیام دایرکت ناموفق: %s", e1)
        return False

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
            "fast_poll": self.fast_poll_active,
        }

    # ---------- دیاگنوستیک (مدیر) ----------
    def debug_report(self) -> str:
        """گزارش یک‌بارهٔ وضعیت سه صندوق برای /igdebug — بدون راز.

        در نخ جداگانه اجرا می‌شود (asyncio.to_thread) چون instagrapi
        همگام است و نباید حلقهٔ رویداد تلگرام را بلاک کند.
        """
        def _short(tid) -> str:
            return "…" + str(tid)[-6:]

        def _user_tag(u) -> str:
            uname = getattr(u, "username", None)
            return f"@{uname}" if uname else f"pk={getattr(u, 'pk', '?')}"

        lines = ["🔍 دیاگ اینستاگرام", ""]
        try:
            me = str(self.client.with_client(lambda cl: cl.user_id))
        except Exception as e:
            return f"❌ نشست اینستاگرام پاسخ نداد: {e}"
        lines.append(f"نشست: آماده ✅ (uid={me})")
        lines.append(f"پول: عادی {self.poll_min}-{self.poll_max}s | سریع "
                     f"{self.fast_poll_min}-{self.fast_poll_max}s "
                     f"({'فعال' if self.fast_poll_active else 'غیرفعال'})")
        counts = {}
        for name, fetch in (
            ("عادی", lambda: self.client.with_client(lambda cl: cl.direct_threads(20))),
            ("درخواست‌ها", lambda: self.client.with_client(lambda cl: cl.direct_requests(20))),
            ("مخفی/اسپم", lambda: self.client.with_client(lambda cl: cl.direct_spam_inbox(20))),
        ):
            try:
                threads = fetch()
            except Exception as e:
                counts[name] = f"خطا: {str(e)[:60]}"
                continue
            counts[name] = str(len(threads))
            if name == "عادی":
                continue
            for t in threads[:8]:
                users = list(getattr(t, "users", None) or [])
                inviter = getattr(t, "inviter", None)
                who = _user_tag(users[0]) if len(users) == 1 else (
                    _user_tag(inviter) if inviter is not None else "نامشخص")
                msgs = list(getattr(t, "messages", None) or [])
                last = msgs[0] if msgs else None
                last_info = ""
                if last is not None:
                    tstamp = _msg_ts(last)
                    when = time.strftime("%H:%M", time.localtime(tstamp)) if tstamp else "?"
                    snippet = (getattr(last, "text", None) or
                               str(getattr(last, "item_type", "?")))[:25]
                    last_info = f" | آخرین: {when} «{snippet}»"
                seen = self._seen_row(str(t.id))
                lines.append(f"  • [{name}] {_short(t.id)} | {who} | پیام={len(msgs)}"
                             f"{' | دیده‌شده' if seen else ' | جدید'}{last_info}")
        lines.insert(2, "صندوق‌ها: " + " | ".join(f"{k}={v}" for k, v in counts.items()))
        # کدهای اتصال اخیر (کد ماسک می‌شود)
        try:
            rows = self.links.db.query(
                "SELECT ig_username, code, expires_at FROM link_codes "
                "ORDER BY created_at DESC LIMIT 5")
            if rows:
                lines.append("")
                lines.append("کدهای اتصال اخیر:")
                now = int(time.time())
                for r in rows:
                    code = str(r["code"])
                    masked = code[:2] + "****" + code[-2:]
                    state = "معتبر" if r["expires_at"] > now else "منقضی"
                    lines.append(f"  • @{r['ig_username']} → {masked} ({state})")
        except Exception:
            pass
        return "\n".join(lines)

    def reprocess_stuck(self) -> str:
        """پاک‌کردن نشانگر «دیده‌شده» ترد‌های درخواست‌ها/مخفی تا دوباره پردازش شوند.

        برای بازیابی پیام‌هایی که به هر دلیل (مثلاً کرش نسخهٔ قبل) خط‌گذاری
        شده‌اند ولی هرگز پردازش نشده‌اند. اسکن بعدی آن‌ها را از نو ارزیابی
        می‌کند (پیام‌های قدیمی‌تر از BASELINE_MAX_AAGE فقط وقتی نشانگرشان
        پاک شده باشد دوباره بررسی می‌شوند).
        """
        try:
            # بررسی زنده بودن نشست (خروجی لازم نیست؛ فقط خطای زودهنگام)
            self.client.with_client(lambda cl: cl.user_id)
        except Exception as e:
            return f"❌ نشست اینستاگرام پاسخ نداد: {e}"
        freed = 0
        for name, fetch in (
            ("درخواست‌ها", lambda: self.client.with_client(lambda cl: cl.direct_requests(20))),
            ("مخفی/اسپم", lambda: self.client.with_client(lambda cl: cl.direct_spam_inbox(20))),
        ):
            try:
                threads = fetch()
            except Exception as e:
                log.warning("خواندن صندوق %s برای بازیابی ناموفق: %s", name, e)
                continue
            for t in threads:
                tid = str(t.id)
                if self._seen_row(tid) is not None:
                    self.engine.d.downloads.db.execute(
                        "DELETE FROM ig_seen WHERE thread_id=?", (tid,))
                    freed += 1
        # نخستین اسکن بعدی نباید این پیام‌ها را «قدیمی» رد کند؛ بنابراین
        # پرچم baseline را نگه می‌داریم و پیام‌ها به‌صورت عادی پردازش می‌شوند.
        self.arm_fast_poll()
        log.info("بازیابی دستی ترد‌های دایرکت: %s مورد آزاد شد", freed)
        return (f"♻️ بازیابی انجام شد: {freed} گفتگو آزاد شد.\n"
                "اسکن بعدی (چند ثانیه دیگر) آن‌ها را از نو بررسی و برای کاربران "
                "متصل‌نشده کد اتصال می‌فرستد.")
