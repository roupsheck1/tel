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

پست‌های «عکس + موزیک» (درخواست کاربر ۲۰۲۶-۱۰-۳۱):
- اگر پاسخ API video_versions داشته باشد همان ویدیو (عکس+موسیقی یکجا)
  دانلود می‌شود — این مسیر از قبل درست بود.
- اگر فقط عکس + فرادادهٔ موزیک باشد (بدون video_versions)، قطعهٔ صوتی از
  خود پاسخ (مثلاً music_metadata.audio_asset_info.progressive_download_url)
  گرفته می‌شود و با ffmpeg یک ویدیوی یکپارچه (عکس + موزیک) ساخته می‌شود؛
  برای کاروسل موزیک‌دار، اسلایدشوی همهٔ عکس‌ها با همان موزیک ساخته می‌شود.
  اگر ساخت ویدیو ممکن نشد، مثل قبل عکس ارسال می‌شود (رفتار امن).
"""
from __future__ import annotations

import glob
import logging
import re
import subprocess
import sys
import threading
from pathlib import Path
from urllib.parse import urlparse

import requests

from .client import InstagramClient

log = logging.getLogger("instagram.content")

MEDIA_EXTS = ("mp4", "jpg", "jpeg", "png", "webp")
_DOWNLOAD_TIMEOUT = 120  # ثانیه؛ فایل‌های بزرگ CDN
_FFMPEG_TIMEOUT = 300    # ثانیه؛ ساخت ویدیوی عکس+موزیک

# برندهای ftyp که یعنی فایل HEIC/HEIF/AVIF است (تلگرام عکس قبول نمی‌کند)
_HEIC_BRANDS = (b"heic", b"heix", b"hevc", b"heim", b"heis", b"mif1", b"msf1",
                b"avif", b"avis")

# پسوندهای صوتی برای تشخیص URL قطعهٔ موزیک
_AUDIO_URL_RE = re.compile(r"\.(mp3|m4a|aac|mp4a)(?:\?|$)", re.IGNORECASE)
# نام فایل‌های کمکی داخل پوشهٔ موقت که رسانه نیستند و نباید ارسال شوند
_AUX_FILES = {"cookies.txt", "uploader.txt"}


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
    قبول کند. فایل‌های ناقص (.part) و کمکی (کوکی/آپلودر) نادیده گرفته می‌شوند.
    """
    out: list[Path] = []
    for p in sorted(Path(folder).iterdir()):
        if not p.is_file() or p.name.startswith(".") or p.name.endswith(".part") \
                or p.name in _AUX_FILES:
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


# ============================ پست‌های «عکس + موزیک" ============================

def _find_music_audio(node) -> str | None:
    """یافتن URL قطعهٔ موزیک در پاسخ API (ساختار بین پست‌ها فرق دارد).

    جست‌وجوی بازگشتی؛ فقط URLهایی پذیرفته می‌شوند که «زیر کلید موزیک/صدا»
    آمده باشند (مثل music_metadata.audio_asset_info.progressive_download_url)
    یا پسوند صوتی (mp3/m4a/aac) دارند — تا URL ویدیو/عکس اشتباهی گرفته نشود.
    """
    hits: list[str] = []

    def walk(n, in_media_ctx: bool) -> None:
        if len(hits) > 1:
            return
        if isinstance(n, dict):
            for k, v in n.items():
                kl = str(k).lower()
                ctx = in_media_ctx or ("music" in kl or "audio" in kl)
                if isinstance(v, str) and v.startswith("http") and ctx and (
                        kl in ("progressive_download_url", "download_url", "audio_url", "url")
                        or _AUDIO_URL_RE.search(v)):
                    hits.append(v)
                elif isinstance(v, (dict, list)):
                    walk(v, ctx)
        elif isinstance(n, list):
            for x in n:
                walk(x, in_media_ctx)

    walk(node, False)
    return hits[0] if hits else None


def _ffprobe(path: Path, entries: str) -> str | None:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", entries, "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=30,
        ).stdout.strip()
        return out or None
    except Exception:
        return None


def _audio_duration(path: Path) -> float | None:
    """طول قطعهٔ صوتی به ثانیه (برای تقسیم زمان اسلایدشو)."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=30,
        ).stdout.strip()
        return float(out) if out else None
    except Exception:
        return None


def _media_dims(path: Path) -> tuple[int, int] | None:
    out = _ffprobe(path, "stream=width,height")
    if not out:
        return None
    try:
        w, h = out.split(",")[:2]
        return int(w), int(h)
    except (ValueError, IndexError):
        return None


def _render_music_video(photo: Path, audio: Path, dest_dir: Path, base: str) -> Path | None:
    """ساخت ویدیوی «عکس + موزیک» — عکس به‌مدت کل آهنگ + صدا (ffmpeg)."""
    out = dest_dir / f"{base}_music.mp4"
    cmd = [
        "ffmpeg", "-y",
        "-loop", "1", "-i", str(photo),
        "-i", str(audio),
        "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2,format=yuv420p",
        "-c:v", "libx264", "-tune", "stillimage",
        "-c:a", "aac", "-b:a", "192k",
        "-shortest", "-movflags", "+faststart",
        str(out),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=_FFMPEG_TIMEOUT)
    except (subprocess.TimeoutExpired, OSError):
        log.warning("ساخت ویدیوی عکس+موزیک ناموفق (ffmpeg)")
        return None
    if proc.returncode != 0 or not out.exists() or out.stat().st_size == 0:
        log.warning("ffmpeg ویدیوی عکس+موزیک را نساخت: %s",
                    (proc.stderr or b"")[:200])
        out.unlink(missing_ok=True)
        return None
    return out


def _render_slideshow_video(photos: list[Path], audio: Path, dest_dir: Path,
                            base: str) -> Path | None:
    """ساخت اسلایدشو (کاروسل موزیک‌دار): همهٔ عکس‌ها + موزیک در یک ویدیو."""
    dims = [d for d in (_media_dims(p) for p in photos) if d]
    if not dims:
        return None
    w = max(d[0] for d in dims)
    h = max(d[1] for d in dims)
    w, h = w + (w % 2), h + (h % 2)  # ابعاد زوج برای x264
    total = _audio_duration(audio) or (5.0 * len(photos))
    per = max(2.0, min(15.0, total / len(photos)))

    vf = (f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
          f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black,format=yuv420p")
    segs: list[Path] = []
    try:
        for i, p in enumerate(photos):
            seg = dest_dir / f"{base}_seg{i}.mp4"
            proc = subprocess.run(
                ["ffmpeg", "-y", "-loop", "1", "-t", f"{per:.2f}", "-i", str(p),
                 "-vf", vf, "-c:v", "libx264", "-tune", "stillimage",
                 "-an", str(seg)],
                capture_output=True, timeout=_FFMPEG_TIMEOUT,
            )
            if proc.returncode != 0 or not seg.exists():
                return None
            segs.append(seg)
        lst = dest_dir / f"{base}_list.txt"
        lst.write_text("\n".join(f"file '{s.name}'" for s in segs), encoding="utf-8")
        out = dest_dir / f"{base}_music.mp4"
        proc = subprocess.run(
            ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
             "-i", str(audio), "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
             "-shortest", "-movflags", "+faststart", str(out)],
            capture_output=True, timeout=_FFMPEG_TIMEOUT,
        )
        if proc.returncode != 0 or not out.exists() or out.stat().st_size == 0:
            return None
        return out
    except (subprocess.TimeoutExpired, OSError):
        log.warning("ساخت اسلایدشوی موزیک‌دار ناموفق (ffmpeg)")
        return None
    finally:
        for s in segs:
            s.unlink(missing_ok=True)
        (dest_dir / f"{base}_list.txt").unlink(missing_ok=True)


def _render_music_item(singles: list[dict], music_url: str, dest_dir: Path,
                       base: str) -> list[Path] | None:
    """دانلود عکس(ها)+موزیک و ساخت یک ویدیوی یکپارچه.

    خروجی: [mp4] در صورت موفقیت؛ None یعنی «انجام نشد» و مسیر عادی عکس ادامه می‌یابد.
    فایل‌های میانی (عکس/صدا) در پایان حذف می‌شوند تا فقط ویدیو ارسال شود.
    """
    photo_files: list[Path] = []
    audio_file: Path | None = None
    try:
        audio_file = _fetch_to_file(music_url, dest_dir, f"{base}_audio", "mp3")
        for idx, item in enumerate(singles, start=1):
            item = item or {}
            image_url = _best_url((item.get("image_versions2") or {}).get("candidates"))
            if not image_url:
                continue
            suffix = f"{idx}" if len(singles) > 1 else ""
            photo_files.append(
                _fetch_to_file(image_url, dest_dir, f"{base}_img{suffix}", "jpg"))
        if not audio_file or not photo_files:
            return None
        if len(photo_files) == 1:
            rendered = _render_music_video(photo_files[0], audio_file, dest_dir, base)
        else:
            rendered = _render_slideshow_video(photo_files, audio_file, dest_dir, base)
        if rendered is None:
            return None
        log.info("ویدیوی عکس+موزیک ساخته شد: %s (%s عکس)", rendered.name, len(photo_files))
        return [rendered]
    except Exception as e:
        log.warning("ساخت ویدیوی عکس+موزیک ناموفق: %s", e)
        return None
    finally:
        # فایل‌های میانی حذف می‌شوند (فقط ویدیوی نهایی می‌ماند)
        for p in photo_files:
            p.unlink(missing_ok=True)
        if audio_file:
            audio_file.unlink(missing_ok=True)


def download_from_media_dict(media: dict, dest_dir: Path,
                             meta_out: dict | None = None) -> list[Path]:
    """دانلود رسانه از دیکتهٔ «خام» پاسخ API اینستاگرام.

    با هر دو شکل پاسخ کار می‌کند (API خصوصیِ media/{pk}/info/ و API وب) و
    به‌جای اتکا به مدل pydantic کتابخانه — که با فیلدهای null مثل
    like_count می‌شکند — مستقیماً فیلدهای استاندارد را می‌خواند:
    - carousel_media غیرخالی → همهٔ آیتم‌های کاروسل (عکس + ویدیو)
    - وگرنه خودِ رسانه: video_versions (ویدیو) و در نبودش image_versions2 (عکس)

    این تابع پوش‌دهندهٔ هر سه شکل مشکل‌ساز است:
    عکس تکی، ویدیو/ریل، کاروسل، و «آیتم کاروسل» (media_type=8 با
    carousel_media خالی) که قبلاً باعث سکوت کامل می‌شد.

    پست‌های «عکس + موزیک» (بدون video_versions): یک ویدیوی یکپارچه از
    عکس و موزیک ساخته می‌شود؛ در صورت شکست، عکس خالص ارسال می‌شود.

    meta_out (اختیاری): فرادادهٔ محتوا (username/code سازنده) برای پیام
    لینک‌های اینستاگرام بعد از تحویل (درخواست کاربر ۲۰۲۶-۱۰-۳۱).
    خروجی: فهرست فایل‌های دانلودشده؛ اگر هیچ فایلی قابل دریافت نباشد
    خطا می‌دهد (هرگز ساکت خالی برنمی‌گردد).
    """
    media = dict(media or {})
    dest_dir.mkdir(parents=True, exist_ok=True)
    username = str((media.get("user") or {}).get("username") or "instagram")
    safe_user = "".join(c if c.isalnum() or c in "._-" else "_" for c in username)[:40]
    pk = str(media.get("pk") or "media")
    if meta_out is not None:
        meta_out["username"] = username if username != "instagram" else None
        meta_out["code"] = media.get("code")

    carousel = media.get("carousel_media") or []
    singles = list(carousel) if carousel else [media]

    # ---------- پست «عکس + موزیک»: ساخت ویدیوی یکپارچه ----------
    # فقط وقتی هیچ آیتمی video_versions ندارد (ویدیو نداریم) و قطعهٔ موزیک
    # در پاسخ هست، ویدیوی عکس+موزیک ساخته می‌شود؛ در شکست، مسیر عادی ادامه می‌یابد.
    any_video = bool(_best_url(media.get("video_versions"))) or any(
        _best_url((it or {}).get("video_versions")) for it in singles
    )
    music_url = None if any_video else _find_music_audio(media)
    if music_url:
        rendered = _render_music_item(singles, music_url, dest_dir, f"{safe_user}_{pk}")
        if rendered:
            return rendered
        log.warning("ساخت ویدیوی عکس+موزیک ممکن نشد؛ عکس(ها) ساده ارسال می‌شود")

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
    """پیاده‌سازی ContentDownloader برای اینستاگرام.

    pop_meta(dest_dir): فرادادهٔ آخرین دانلودِ همان پوشه (نام کاربری سازنده)
    را برمی‌گرداند و پاک می‌کند؛ موتور دانلود از آن برای ارسال پیام
    «بازکردن پست در اینستاگرام» و لینک صفحهٔ سازنده استفاده می‌کند.
    """

    def __init__(self, client: InstagramClient, proxy: str | None = None):
        self.client = client
        self.proxy = proxy or None
        self._meta: dict[str, dict] = {}
        self._meta_lock = threading.Lock()

    def pop_meta(self, dest_dir) -> dict | None:
        """فرادادهٔ دانلودِ این پوشه را برگردان و حذف کن (یک‌بارمصرف)."""
        with self._meta_lock:
            return self._meta.pop(str(dest_dir), None)

    def _store_meta(self, dest_dir, meta: dict) -> None:
        with self._meta_lock:
            self._meta[str(dest_dir)] = meta

    def download(self, url: str, shortcode: str, dest_dir: Path, kind: str = "post") -> list[Path]:
        dest_dir.mkdir(parents=True, exist_ok=True)
        errors: list[str] = []
        labels = {"_via_instagrapi": "instagrapi", "_via_ytdlp": "yt-dlp"}
        for method in (self._via_instagrapi, self._via_ytdlp):
            meta: dict = {}
            try:
                files = method(url, shortcode, dest_dir, kind, meta)
                if files:
                    self._store_meta(dest_dir, meta)
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
    def _via_instagrapi(self, url: str, shortcode: str, dest_dir: Path, kind: str = "post",
                         meta: dict | None = None) -> list[Path]:
        meta = meta if meta is not None else {}

        def _do(cl):
            if kind == "story":
                # استوری: shortcode همان media_id کامل (owner_pk_media_pk) یا pk خام است
                cl.story_download(shortcode, folder=dest_dir)
                # نام کاربری سازنده از خود URL استوری (برای پیام لینک‌ها)
                mt = re.search(r"/stories/([^/]+)/", str(url))
                if mt:
                    meta["username"] = mt.group(1)
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
            return download_from_media_dict(items[0], dest_dir, meta_out=meta)

        return self.client.with_client(_do)

    # ---------- روش ۲: yt-dlp (محتوای عمومی ویدیویی؛ با کوکی نشست ربات) ----------
    def _via_ytdlp(self, url: str, shortcode: str, dest_dir: Path, kind: str = "post",
                    meta: dict | None = None) -> list[Path]:
        meta = meta if meta is not None else {}
        uploader_file = dest_dir / "uploader.txt"
        args = [
            sys.executable, "-m", "yt_dlp",
            "--no-playlist", "--no-warnings",
            "-f", "bv*[vcodec^=avc1]+ba[ext=m4a]/b[ext=mp4]/b",
            "--merge-output-format", "mp4",
            "-o", str(dest_dir / "%(id)s.%(ext)s"),
            # نام کاربری سازنده برای پیام لینک‌ها (yt-dlp >= 2023.03)
            "--print-to-file", "after_move:%(uploader)s", str(uploader_file),
        ]
        if kind == "story":
            # برای استوری، نام کاربری از خود URL استوری برداشته می‌شود
            mt = re.search(r"/stories/([^/]+)/", str(url))
            if mt:
                meta["username"] = mt.group(1)
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
        # نام سازنده (اگر yt-dlp نوشته باشد) برای پیام لینک‌ها
        try:
            if uploader_file.exists():
                up = uploader_file.read_text(encoding="utf-8").strip()
                if up and up.upper() != "NA":
                    meta["username"] = up
        except OSError:
            pass
        finally:
            uploader_file.unlink(missing_ok=True)
        return find_media(Path(dest_dir))
