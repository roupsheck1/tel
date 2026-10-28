"""کلاینت اینستاگرام با نشست پایدار (بخش ۴.۳ سند — اسرار از محیط).

نکته مهم (بخش ۳.۱): instagrapi یک کتابخانه غیررسمی است و دریافت دایرکت
با آن «سرویس تضمین‌شده رسمی» محسوب نمی‌شود؛ محدودیت‌ها در README و
گزارش وضعیت مستند شده‌اند.
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from instagrapi import Client
from instagrapi.exceptions import ClientError, TwoFactorRequired

log = logging.getLogger("instagram.client")

# نشانه‌های خطای نشست/احراز هویت — فقط برای این‌ها «ورود مجدد» منطقی است.
# خطاهای دیگر (مثل نبودِ پست یا «ویدیو ندارد») بی‌ربط به نشست‌اند و
# ورود مجددِ بی‌مورد فقط ریسک محدودشدن اکانت و کندی را بیشتر می‌کند.
_AUTH_ERROR_MARKERS = (
    "login required",
    "unauthorized",
    "not logged in",
    "401",
    "challenge required",
    "checkpoint",
    "feedback_required",
)


def _is_auth_error(text: str) -> bool:
    t = (text or "").lower()
    return any(m in t for m in _AUTH_ERROR_MARKERS)


class InstagramLoginError(Exception):
    pass


class InstagramClient:
    """پوشش امن و thread-safe کلاینت instagrapi با مدیریت نشست."""

    def __init__(self, username: str, password: str, session_file: Path,
                 proxy: str | None = None, two_factor_code: str | None = None):
        self.username = username
        self.password = password
        self.session_file = Path(session_file)
        self.proxy = proxy or None
        self.two_factor_code = two_factor_code or None
        self.cl: Client | None = None
        self._lock = threading.Lock()
        self.logged_in_at: int | None = None
        self.last_error: str | None = None

    # ---------- ورود ----------
    def login(self) -> bool:
        """ورود با نگهداری نشست؛ در صورت وجود نشست معتبر از آن استفاده می‌شود."""
        with self._lock:
            client = Client()
            client.delay_range = [2, 6]  # تأخیر تصادفی برای کاهش ریسک محدودسازی
            if self.proxy:
                client.set_proxy(self.proxy)
            if self.session_file.exists():
                try:
                    client.load_settings(str(self.session_file))
                except Exception as e:
                    log.warning("بارگذاری نشست ناموفق بود: %s", e)
            try:
                client.login(self.username, self.password)
            except TwoFactorRequired:
                if not self.two_factor_code:
                    self.last_error = "این اکانت دومرحله‌ای است؛ IG_2FA_CODE تنظیم نشده."
                    raise InstagramLoginError(self.last_error)
                client.login(self.username, self.password,
                             verification_code=self.two_factor_code)
            except ClientError as e:
                self.last_error = str(e)
                raise InstagramLoginError(f"ورود به اینستاگرام ناموفق: {e}")
            self.cl = client
            self.logged_in_at = int(time.time())
            self.last_error = None
            self._save_session()
            log.info("ورود موفق به اینستاگرام")
            return True

    def _save_session(self) -> None:
        try:
            self.session_file.parent.mkdir(parents=True, exist_ok=True)
            self.cl.dump_settings(str(self.session_file))
        except Exception as e:
            log.warning("ذخیره نشست ناموفق: %s", e)

    # ---------- ابزار عمومی ----------
    def with_client(self, fn):
        """اجرای امن یک عملیات؛ ورود مجدد فقط برای خطای نوع نشست.

        خطاهای غیرنشستی (مثل نبودِ پست) بلافاصله برمی‌گردند تا تلاش مجددِ
        بی‌فایده، خواب ۵ ثانیه‌ای و ورودهای بی‌مورد به اکانت انجام نشود.
        """
        for attempt in (1, 2):
            if self.cl is None:
                self.login()
            try:
                with self._lock:
                    return fn(self.cl)
            except Exception as e:
                self.last_error = str(e)
                log.warning("عملیات اینستاگرام ناموفق (تلاش %s): %s", attempt, e)
                if attempt == 1 and _is_auth_error(str(e)):
                    time.sleep(5)
                    try:
                        self.login()
                    except InstagramLoginError:
                        break
                else:
                    break  # خطای غیر نشستی: تلاش مجدد بی‌فایده است
        raise ClientError(f"عملیات اینستاگرام پس از تلاش مجدد ناموفق: {self.last_error}")

    def export_cookies(self, path: Path) -> bool:
        """نوشتن کوکی‌های نشست فعال (private + public) در قالب Netscape.

        yt-dlp برای بسیاری از پست‌های اینستاگرام و همهٔ استوری‌ها به کوکی
        لاگین‌شده نیاز دارد؛ بدون آن «empty media response» یا 429 می‌گیرد.
        خروجی False یعنی نشست آماده نیست و yt-dlp باید بدون کوکی اجرا شود.
        """
        try:
            def _do(cl):
                lines = ["# Netscape HTTP Cookie File",
                         "# Generated from bot session for yt-dlp"]
                count = 0
                for session in (getattr(cl, "private", None), getattr(cl, "public", None)):
                    if session is None:
                        continue
                    for c in session.cookies:
                        domain = c.domain or ".instagram.com"
                        include_sub = "TRUE" if domain.startswith(".") else "FALSE"
                        secure = "TRUE" if c.secure else "FALSE"
                        expires = str(int(c.expires)) if c.expires else "0"
                        path_ = c.path or "/"
                        lines.append(f"{domain}\t{include_sub}\t{path_}\t{secure}\t{expires}\t{c.name}\t{c.value}")
                        count += 1
                if count == 0:
                    return False
                Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")
                return True
            return bool(self.with_client(_do))
        except Exception as e:
            log.warning("خروجی کوکی برای yt-dlp ناموفق: %s", e)
            return False

    @property
    def is_ready(self) -> bool:
        return self.cl is not None and self.logged_in_at is not None
