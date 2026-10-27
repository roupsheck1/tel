"""دریافت محتوای اینستاگرام — چندروشی با جایگزین (بخش ۳.۲ و ۱۵ سند).

روش‌ها به‌ترتیب:
1. instagrapi با اکانت ربات (پشتیبانی از محتوای خصوصی در دسترس اکانت)
2. yt-dlp برای محتوای عمومی (بدون نیاز به حساب)
در صورت شکست روش اول، روش دوم امتحان می‌شود و خطاهای موقت مدیریت می‌شوند.
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


class InstagramContentDownloader:
    """پیاده‌سازی ContentDownloader برای اینستاگرام."""

    def __init__(self, client: InstagramClient, proxy: str | None = None):
        self.client = client
        self.proxy = proxy or None

    def download(self, url: str, shortcode: str, dest_dir: Path, kind: str = "post") -> list[Path]:
        dest_dir.mkdir(parents=True, exist_ok=True)
        last_error: Exception | None = None
        for method in (self._via_instagrapi, self._via_ytdlp):
            try:
                files = method(url, shortcode, dest_dir, kind)
                if files:
                    return files
            except Exception as e:
                last_error = e
                log.warning("روش %s ناموفق: %s", method.__name__, e)
        if last_error:
            raise RuntimeError(f"دریافت محتوا ناموفق: {last_error}")
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

    # ---------- روش ۲: yt-dlp (فقط محتوای عمومی) ----------
    def _via_ytdlp(self, url: str, shortcode: str, dest_dir: Path, kind: str = "post") -> list[Path]:
        args = [
            sys.executable, "-m", "yt_dlp",
            "--no-playlist", "--no-warnings",
            "-f", "bv*[vcodec^=avc1]+ba[ext=m4a]/b[ext=mp4]/b",
            "--merge-output-format", "mp4",
            "-o", str(dest_dir / "%(id)s.%(ext)s"),
        ]
        if self.proxy:
            args += ["--proxy", self.proxy]
        args.append(url)
        proc = subprocess.run(args, capture_output=True, timeout=300)
        if proc.returncode != 0:
            tail = proc.stderr.decode("utf-8", "replace")[-300:]
            raise RuntimeError(f"yt-dlp شکست خورد: {tail}")
        return find_media(Path(dest_dir))
