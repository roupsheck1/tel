"""ارسال فایل به تلگرام با مدیریت فایل حجیم (بخش ۳.۳ سند).

محدودیت واقعی Bot API رسمی برای آپلود بات: ۵۰ مگابایت. هدف ۲ گیگابایت
فقط با Telegram Local Bot API Server (نیازمند api_id/api_hash معتبر)
قابل دستیابی است؛ در صورت تنظیم TELEGRAM_BASE_URL از آن استفاده می‌شود
و در غیر این صورت فایل‌های بزرگ‌تر از سقف با ffmpeg فشرده می‌شوند.
"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from aiogram import Bot
from aiogram.types import FSInputFile

log = logging.getLogger("telegram.delivery")

BOT_API_LIMIT = 50 * 1024 * 1024     # سقف آپلود Bot API رسمی
LOCAL_API_LIMIT = 2 * 1024 * 1024 * 1024  # سقف Local Bot API Server
UPLOAD_TIMEOUT = 900  # ثانیه — آپلود فایل بزرگ ممکن است بیشتر از ۶۰ ثانیه پیش‌فرض طول بکشد


class TelegramSender:
    """پیاده‌سازی DeliverySender برای موتور دانلود."""

    def __init__(self, bot: Bot, use_local_api: bool = False):
        self.bot = bot
        self.use_local_api = use_local_api
        self.limit = LOCAL_API_LIMIT if use_local_api else BOT_API_LIMIT

    async def send(self, user_id: int, path: Path) -> bool:
        """ارسال فایل؛ در صورت لزوم فشرده‌سازی و در نهایت fallback به document."""
        path = Path(path)
        if not path.exists() or path.stat().st_size == 0:
            log.error("فایل %s موجود نیست یا خالی است", path)
            return False
        try:
            size = path.stat().st_size
            if size > self.limit:
                path = await self._compress(path)
                if path is None:
                    await self.bot.send_message(
                        user_id, "حجم فایل بیش از حد مجاز ارسال است."
                    )
                    return False
            if path.suffix.lower() in (".mp4", ".m4v", ".mov", ".webm"):
                await self._send_video(user_id, path)
            else:
                # نکته: aiogram 3 فقط str (آدرس/فایل‌آیدی) یا InputFile می‌پذیرد؛
                # Path خام باعث خطای اعتبارسنجی SendPhoto می‌شود.
                await self.bot.send_photo(
                    user_id, photo=FSInputFile(path), request_timeout=UPLOAD_TIMEOUT,
                )
            return True
        except Exception:
            try:  # تلاش دوم به‌صورت سند (fallback — بخش ۱۵.۱۱ خطای ارسال)
                await self.bot.send_document(
                    user_id, document=FSInputFile(path), request_timeout=UPLOAD_TIMEOUT,
                )
                return True
            except Exception as e:
                log.exception("ارسال فایل %s ناموفق: %s", path, e)
                raise

    async def _send_video(self, user_id: int, path: Path) -> None:
        width = height = duration = None
        try:
            out = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=width,height,duration", "-of", "csv=p=0", str(path)],
                capture_output=True, text=True, timeout=30,
            ).stdout.strip()
            parts = out.split(",")
            width, height = int(parts[0]), int(parts[1])
            duration = int(float(parts[2]))
        except Exception:
            pass
        await self.bot.send_video(
            user_id, video=FSInputFile(path), width=width, height=height,
            duration=duration, supports_streaming=True,
            request_timeout=UPLOAD_TIMEOUT,
        )

    async def _compress(self, path: Path) -> Path | None:
        """فشرده‌سازی ffmpeg برای جا شدن زیر سقف؛ در صورت شکست None."""
        out = path.with_name(path.stem + "_small.mp4")
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-i", str(path),
                 "-vf", "scale=-2:720", "-c:v", "libx264", "-crf", "30",
                 "-preset", "veryfast", "-c:a", "aac", "-b:a", "96k",
                 "-movflags", "+faststart", str(out)],
                capture_output=True, timeout=900,
            )
        except subprocess.TimeoutExpired:
            return None
        if out.exists() and out.stat().st_size <= self.limit:
            return out
        return None
