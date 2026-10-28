"""دریافت محتوای اینستاگرام — چندروشی با جایگزین (بخش ۳.۲ و ۱۵ سند).

روش‌ها به‌ترتیب:
1. instagrapi با اکانت ربات (پشتیبانی از محتوای خصوصی در دسترس اکانت)
2. yt-dlp برای محتوای عمومی (بدون نیاز به حساب)
در صورت شکست روش اول، روش دوم امتحان می‌شود و خطاهای موقت مدیریت می‌شوند.

نکات مهم yt-dlp (کشف‌شده با آزمون واقعی، ۲۰۲۶-۱۰):
- اکسترکتور اینستاگرام yt-dlp «فقط ویدیو» دانلود می‌کند؛ پست‌های عکس باید
  از مسیر instagrapi بیایند.
- برای بسیاری از پست‌ها و همهٔ استوری‌ها، yt-dlp بدون کوکی لاگین‌شده
  «Instagram sent an empty media response» یا خطای 429 می‌گیرد؛ به همین
  دلیل کوکی‌های نشست ربات در قالب Netscape به yt-dlp داده می‌شود.
- خطای معنادار yt-dlp در خط «ERROR:» ابتدای stderr است؛ دنبالهٔ پیام
  فقط بویلرپلیت «گزارش مشکل» است و نباید به کاربر نشان داده شود.
"""
from __future__ import annotations

import glob
import logging
import subprocess
import sys
from pathlib import Path

from .client import InstagramClient

log = logging.getLogger("instagram.content")

MEDIA_EXTS = ("mp4", "jpg", "jpeg", "png", "webp")


def find_media(folder: Path) -> list[Path]:
    files: list[Path] = []
    for ext in MEDIA_EXTS:
        files += [Path(f) for f in glob.glob(str(folder / f"*.{ext}"))]
    return sorted(files)


def _first_line(text: str, limit: int = 200) -> str:
    """اولین خط غیرخالی متن، کوتاه‌شده برای پیام کاربر."""
    for line in (text or "").splitlines():
        s = line.strip()
        if s:
            return s[:limit]
    return "خطای ناشناخته"


# معادل فارسیِ کوتاه برای خطاهای رایج yt-dlp (پیام کامل در لاگ می‌ماند)
_YTDLP_HINTS: tuple[tuple[str, str], ...] = (
    ("no video in this post", "این پست ویدیو ندارد (yt-dlp فقط ویدیو دانلود می‌کند)"),
    ("empty media response", "اینستاگرام پاسخ خالی داد (پست خصوصی/حذف‌شده یا نیازمند ورود)"),
    ("requested content is not available", "محتوا در دسترس نیست (حذف‌شده یا خصوصی)"),
    ("login required", "این محتوا نیازمند ورود به اینستاگرام است"),
    ("rate limit", "محدودیت موقت اینستاگرام؛ کمی بعد دوباره بفرست"),
    ("429", "محدودیت موقت اینستاگرام؛ کمی بعد دوباره بفرست"),
    ("failed to extract", "استخراج اطلاعات محتوا ناموفق بود"),
    ("unable to download webpage", "دریافت صفحه از اینستاگرام ناموفق بود"),
)


def _ytdlp_reason(stderr: str) -> str:
    """استخراج خط «ERROR:» واقعی از stderr و بازگشتی علت کوتاه فارسی."""
    err = ""
    for line in (stderr or "").splitlines():
        s = line.strip()
        if s.startswith("ERROR:"):
            err = s[len("ERROR:"):].strip()
            break
    if not err:
        err = _first_line(stderr or "", 200)
    # خطای کامل برای لاگ (خطای اصلی، نه دنبالهٔ بویلرپلیت)
    log.error("خطای yt-dlp: %s", err[:500])
    for needle, hint in _YTDLP_HINTS:
        if needle in err.lower():
            return hint
    return _first_line(err, 120)


class InstagramContentDownloader:
    """پیاده‌سازی ContentDownloader برای اینستاگرام."""

    def __init__(self, client: InstagramClient, proxy: str | None = None):
        self.client = client
        self.proxy = proxy or None

    def download(self, url: str, shortcode: str, dest_dir: Path, kind: str = "post") -> list[Path]:
        dest_dir.mkdir(parents=True, exist_ok=True)
        errors: list[str] = []
        labels = {"_via_instagrapi": "instagrapi", "_via_ytdlp": "yt-dlp"}
        for method in (self._via_instagrapi, self._via_ytdlp):
            try:
                files = method(url, shortcode, dest_dir, kind)
                if files:
                    return files
            except Exception as e:
                label = labels.get(method.__name__, method.__name__)
                errors.append(f"{label}: {_first_line(str(e), 140)}")
                log.warning("روش %s ناموفق: %s", label, e)
        if errors:
            # هر دو علت در یک خط تا در پیام کاربر (خط اول) دیده شوند
            raise RuntimeError("دریافت محتوا ناموفق — " + " | ".join(errors))
        return []

    # ---------- روش ۱: instagrapi (قابل استفاده برای محتوای در دسترس اکانت) ----------
    def _via_instagrapi(self, url: str, shortcode: str, dest_dir: Path, kind: str = "post") -> list[Path]:
        def _do(cl):
            if kind == "story":
                # استوری: shortcode همان media_id کامل (owner_pk_media_pk) یا pk خام است
                cl.story_download(shortcode, folder=dest_dir)
                return find_media(dest_dir)
            pk = cl.media_pk_from_url(url)
            info = cl.media_info(pk)
            target = Path(dest_dir)
            if info.media_type == 2:      # ویدیو/ریلز
                cl.video_download(pk, target)
            elif info.media_type == 1:    # عکس
                cl.photo_download(pk, target)
            elif info.media_type == 8:    # آلبوم
                cl.album_download(pk, target)
            else:
                raise ValueError(f"نوع محتوای پشتیبانی‌نشده: {info.media_type}")
            return find_media(target)

        return self.client.with_client(_do)

    # ---------- روش ۲: yt-dlp (محتوای عمومی؛ با کوکی نشست ربات) ----------
    def _via_ytdlp(self, url: str, shortcode: str, dest_dir: Path, kind: str = "post") -> list[Path]:
        args = [
            sys.executable, "-m", "yt_dlp",
            "--no-playlist", "--no-warnings",
            "-f", "bv*[vcodec^=avc1]+ba[ext=m4a]/b[ext=mp4]/b",
            "--merge-output-format", "mp4",
            "-o", str(dest_dir / "%(id)s.%(ext)s"),
        ]
        # کوکی نشست ربات (در صورت وجود) → رفع «empty media response» و 429
        cookie_file = dest_dir / "cookies.txt"
        if self.client.export_cookies(cookie_file):
            args += ["--cookies", str(cookie_file)]
            log.info("yt-dlp با کوکی نشست ربات اجرا می‌شود")
        if self.proxy:
            args += ["--proxy", self.proxy]
        args.append(url)
        try:
            proc = subprocess.run(args, capture_output=True, timeout=600)
        finally:
            # کوکی حساس (sessionid) هرگز روی دیسک باقی نمی‌ماند
            cookie_file.unlink(missing_ok=True)
        if proc.returncode != 0:
            stderr = proc.stderr.decode("utf-8", "replace")
            raise RuntimeError(f"yt-dlp شکست خورد: {_ytdlp_reason(stderr)}")
        return find_media(Path(dest_dir))
