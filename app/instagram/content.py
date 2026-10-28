"""دریافت محتوای اینستاگرام — چندروشی با جایگزین (بخش ۳.۲ و ۱۵ سند).

روش‌ها به‌ترتیب:
1. instagrapi با اکانت ربات از مسیر «API خام» (پشتیبانی از محتوای خصوصی در دسترس اکانت)
2. yt-dlp برای محتوای عمومی ویدیویی (بدون نیاز به حساب)
در صورت شکست روش اول، روش دوم امتحان می‌شود و خطاهای موقت مدیریت می‌شوند.

نکات مهم (کشف‌شده با آزمون واقعی، ۲۰۲۶-۱۰):
- yt-dlp ذاتاً «فقط ویدیو» دانلود می‌کند؛ پست‌های عکس اینستاگرام همیشه شکست
  می‌خورند (بی‌کوکی: «There is no video in this post»، با کوکی از API خصوصی:
  «No video formats found!»). عکس‌ها فقط از مسیر instagrapi دریافت می‌شوند.
- yt-dlp بدون کوکی برای بسیاری از پست‌ها «empty media response» یا 429 می‌گیرد؛
  به همین دلیل کوکی‌های نشست ربات در قالب Netscape به yt-dlp داده می‌شود.
- پاسخ API اینستاگرام برای برخی رسانه‌ها (مثل آیتم‌های کاروسل) «like_count: null»
  دارد که مدل pydantic کتابخانه را می‌شکند (ValidationError). به همین دلیل مسیر
  instagrapi به‌جای media_info/photo_download/album_download از «API خام»
  (private_request) استفاده می‌کند و خودش فیلدهای استاندارد را می‌خواند:
  carousel_media / video_versions / image_versions2.
- آیتم کاروسل گاهی media_type=8 با carousel_media خالی برمی‌گرداند؛
  album_download قدیمی در آن حالت «بدون خطا و بدون فایل» برمی‌گشت و روش
  بدون شکست به yt-dlp می‌افتاد (سکوت کامل). حالت جدید خودِ آیتم را دانلود
  می‌کند و هیچ روشی دیگر بدون گزارش، خالی برنمی‌گردد.
- خطای معنادار yt-dlp در خط «ERROR:» ابتدای stderr است؛ دنبالهٔ پیام
  فقط بویلرپلیت «گزارش مشکل» است و نباید به کاربر نشان داده شود.
"""
from __future__ import annotations

import glob
import logging
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

import requests

from .client import InstagramClient

log = logging.getLogger("instagram.content")

MEDIA_EXTS = ("mp4", "jpg", "jpeg", "png", "webp")
_DOWNLOAD_TIMEOUT = 120  # ثانیه؛ فایل‌های بزرگ CDN

# برندهای ftyp که یعنی فایل HEIC/HEIF/AVIF است (تلگرام عکس قبول نمی‌کند)
_HEIC_BRANDS = (b"heic", b"heix", b"hevc", b"heim", b"heis", b"mif1", b"msf1",
                b"avif", b"avis")


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
    ("no video formats found", "این محتوا ویدیو نیست (عکس‌ها از مسیر اصلی دریافت می‌شوند)"),
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


# ============================ دانلود از API خام ============================

def _best_url(versions) -> str | None:
    """بالاترین کیفیت از «video_versions» یا «image_versions2.candidates»."""
    if not versions:
        return None

    def _area(v):
        try:
            return int(v.get("width") or 0) * int(v.get("height") or 0)
        except (TypeError, ValueError):
            return 0

    try:
        best = max(versions, key=_area)
    except (TypeError, ValueError):
        best = versions[0]
    return str((best or {}).get("url") or "") or None


def _ext_of(url: str, default: str) -> str:
    """پسوند فایل از مسیر URL؛ در نبودِ پسوند معتبر، پیش‌فرض."""
    tail = urlparse(str(url)).path.rsplit("/", 1)[-1]
    if "." in tail:
        ext = tail.rsplit(".", 1)[-1].lower()
        if ext.isalnum() and len(ext) <= 5:
            return ext
    return default


def _sniff_ext(head: bytes, default: str) -> str:
    """تشخیص پسوند واقعی از بایت‌های ابتدایی فایل (magic bytes).

    اهمیت واقعی (کشف‌شده با آزمون روی پست کاربر): اینستاگرام اکنون URLهای
    تصویر را با پسوند «.heic» می‌دهد، اما CDN به کلاینت معمولی «JPEG» سرو
    می‌کند؛ اگر نام فایل را از URL بگیریم، فایلِ JPEG با پسوند heic ذخیره
    می‌شود که هم find_media آن را نمی‌بیند و هم تلگرام آن را عکس قبول
    نمی‌کند. پس پسوند را از «محتوای واقعی» تعیین می‌کنیم.
    """
    if head[:2] == b"\xff\xd8":
        return "jpg"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if head[4:8] == b"ftyp":
        return "heic" if head[8:12] in _HEIC_BRANDS else "mp4"
    return default


def _read_head(path: Path, size: int = 16) -> bytes:
    try:
        with open(path, "rb") as f:
            return f.read(size)
    except OSError:
        return b""


def _fetch_to_file(url: str, dest_dir: Path, base_name: str, default_ext: str) -> Path:
    """دانلود یک فایل از CDN اینستاگرام با صحت‌سنجی حجم و پسوند واقعی.

    فایل ابتدا به صورت «.part» دریافت می‌شود، حجم آن با Content-Length
    سنجیده می‌شود و پسوند نهایی از محتوای واقعی (magic bytes) تعیین
    می‌شود؛ در نتیجه JPEGِ سروشده از URLهای heic با پسوند jpg ذخیره
    می‌شود و برای تلگرام قابل ارسال است (اتم یا هیچ — بدون فایل ناقص).
    """
    accept = "video/mp4,*/*" if default_ext == "mp4" else "image/jpeg,image/png,image/webp,*/*"
    resp = requests.get(url, stream=True, timeout=_DOWNLOAD_TIMEOUT,
                        headers={"Accept": accept})
    resp.raise_for_status()
    expected = resp.headers.get("Content-Length")
    tmp = dest_dir / f"{base_name}.part"
    try:
        with open(tmp, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1 << 20):
                if chunk:
                    f.write(chunk)
        actual = tmp.stat().st_size
        if actual == 0:
            raise RuntimeError("پاسخ CDN خالی بود")
        if expected is not None and actual != int(expected):
            raise RuntimeError(f"دانلود ناقص (انتظار {expected} بایت، دریافت {actual})")
        ext = _sniff_ext(_read_head(tmp), default_ext)
        dest = dest_dir / f"{base_name}.{ext}"
        tmp.replace(dest)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    return dest


def _collect_downloaded(folder: Path) -> list[Path]:
    """گردآوری فایل‌های دانلودشدهٔ یک پوشه با نرمال‌سازی پسوند واقعی.

    برای مسیرهایی که فایل را خودِ کتابخانه ذخیره کرده (مثل story_download):
    اگر پسوند فایل با محتوایش نخواند (مثلاً JPEG با نام heic)، به پسوند
    واقعی تغییر می‌یابد تا هم قابل یافتن باشد و هم تلگرام آن را عکس
    قبول کند. فایل‌های ناقص (.part) و کوکی نادیده گرفته می‌شوند.
    """
    out: list[Path] = []
    for p in sorted(Path(folder).iterdir()):
        if not p.is_file() or p.name.startswith(".") or p.name.endswith(".part") \
                or p.name == "cookies.txt":
            continue
        real = _sniff_ext(_read_head(p), p.suffix.lower().lstrip("."))
        if real and real != p.suffix.lower().lstrip("."):
            new = p.with_suffix(f".{real}")
            if new.exists():
                new = p  # تصادم نام؛ نام قبلی حفظ می‌شود
            else:
                p.rename(new)
            p = new
        out.append(p)
    return out


def download_from_media_dict(media: dict, dest_dir: Path) -> list[Path]:
    """دانلود رسانه از دیکتهٔ «خام» پاسخ API اینستاگرام.

    با هر دو شکل پاسخ کار می‌کند (API خصوصیِ media/{pk}/info/ و API وب) و
    به‌جای اتکا به مدل pydantic کتابخانه — که با فیلدهای null مثل
    like_count می‌شکند — مستقیماً فیلدهای استاندارد را می‌خواند:
    - carousel_media غیرخالی → همهٔ آیتم‌های کاروسل (عکس + ویدیو)
    - وگرنه خودِ رسانه: video_versions (ویدیو) و در نبودش image_versions2 (عکس)

    این تابع پوش‌دهندهٔ هر سه شکل مشکل‌ساز است:
    عکس تکی، ویدیو/ریل، کاروسل، و «آیتم کاروسل» (media_type=8 با
    carousel_media خالی) که قبلاً باعث سکوت کامل می‌شد.
    خروجی: فهرست فایل‌های دانلودشده؛ اگر هیچ فایلی قابل دریافت نباشد
    خطا می‌دهد (هرگز ساکت خالی برنمی‌گردد).
    """
    media = dict(media or {})
    dest_dir.mkdir(parents=True, exist_ok=True)
    username = str((media.get("user") or {}).get("username") or "instagram")
    safe_user = "".join(c if c.isalnum() or c in "._-" else "_" for c in username)[:40]
    pk = str(media.get("pk") or "media")

    carousel = media.get("carousel_media") or []
    singles = list(carousel) if carousel else [media]

    files: list[Path] = []
    for idx, item in enumerate(singles, start=1):
        item = item or {}
        video_url = _best_url(item.get("video_versions"))
        image_url = _best_url((item.get("image_versions2") or {}).get("candidates"))
        url, default_ext = (video_url, "mp4") if video_url else (image_url, "jpg")
        if not url:
            log.warning("آیتم %s از رسانه %s فایل قابل دریافت ندارد (video/image versions خالی)", idx, pk)
            continue
        base = f"{safe_user}_{pk}{f'_{idx}' if len(singles) > 1 else ''}"
        try:
            files.append(_fetch_to_file(url, dest_dir, base, default_ext))
        except Exception as e:
            log.warning("دانلود آیتم %s از رسانه %s ناموفق: %s", idx, pk, e)
    if not files:
        raise RuntimeError("این محتوا فایل قابل دریافت‌ای ندارد (عکس/ویدیو در پاسخ نیست)")
    return files


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
                # «موفق اما بدون فایل» دیگر سکوت نمی‌کند؛ قبلاً همین حالت
                # سبب می‌شد شکستِ روش اول کاملاً بی‌صدا قورت داده شود.
                label = labels.get(method.__name__, method.__name__)
                errors.append(f"{label}: بدون فایل برگشت")
                log.warning("روش %s بدون هیچ فایلی برگشت", label)
            except Exception as e:
                label = labels.get(method.__name__, method.__name__)
                errors.append(f"{label}: {_first_line(str(e), 140)}")
                log.warning("روش %s ناموفق: %s", label, e)
        if errors:
            # هر دو علت در یک خط تا در پیام کاربر (خط اول) دیده شوند
            raise RuntimeError("دریافت محتوا ناموفق — " + " | ".join(errors))
        return []

    # ---------- روش ۱: instagrapi با API خام (محتوای در دسترس اکانت) ----------
    def _via_instagrapi(self, url: str, shortcode: str, dest_dir: Path, kind: str = "post") -> list[Path]:
        def _do(cl):
            if kind == "story":
                # استوری: shortcode همان media_id کامل (owner_pk_media_pk) یا pk خام است
                cl.story_download(shortcode, folder=dest_dir)
                # نرمال‌سازی پسوند واقعی: اینستاگرام URLهای heic با محتوای JPEG
                # می‌دهد؛ story_download نام فایل را از URL می‌سازد و بدون این
                # اصلاح، عکس استوری «گم» می‌شد.
                files = _collect_downloaded(dest_dir)
                if not files:
                    raise RuntimeError("استوری دریافت شد اما فایلی ذخیره نشد")
                return files
            pk = cl.media_pk_from_url(url)
            # مسیر «API خام»: مقاوم در برابر like_count=null، کاروسل بی‌آیتم و
            # آیتم کاروسل؛ مستقیماً از items[0] فایل‌ها را دریافت می‌کنیم.
            result = cl.private_request(f"media/{pk}/info/")
            items = result.get("items") if isinstance(result, dict) else None
            if not items:
                raise RuntimeError(
                    "اینستاگرام اطلاعات این محتوا را نداد (خصوصی/حذف‌شده یا خارج از دسترس اکانت)")
            return download_from_media_dict(items[0], dest_dir)

        return self.client.with_client(_do)

    # ---------- روش ۲: yt-dlp (محتوای عمومی ویدیویی؛ با کوکی نشست ربات) ----------
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
