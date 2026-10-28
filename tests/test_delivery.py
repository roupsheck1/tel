"""آزمون‌های رگرسیون ارسال فایل به تلگرام — با Bot «واقعی» aiogram.

پس از باگ تولیدی (پاس‌دادن Path خام به send_video/send_photo/send_document
و kwargs نامعتبر read_timeout/write_timeout که در aiogram 3 وجود ندارند)،
این آزمون‌ها با یک نشست ضبط‌کننده و Bot واقعی اجرا می‌شوند تا اعتبارسنجی
واقعی pydantic و امضای واقعی متدها اجرا شود — فیک‌های قبلی این باگ را
نمی‌دیدند چون خودشان جای Bot را می‌گرفتند.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncGenerator, Optional

import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import SendMessage, SendDocument, SendPhoto, SendVideo, TelegramMethod
from aiogram.types import Chat, FSInputFile, Message

from app.telegram.delivery import TelegramSender


class RecordingSession(BaseSession):
    """نشست آزمایشی: درخواست را اعتبارسنجی‌شده ثبت می‌کند و پاسخ جعلی می‌دهد."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[TelegramMethod] = []
        self.fail_on: set[type] = set()

    async def make_request(
        self, bot: Bot, method: TelegramMethod, timeout: Optional[int] = None
    ) -> Any:
        self.calls.append(method)
        if type(method) in self.fail_on:
            raise RuntimeError("simulated telegram failure")
        return Message(
            message_id=len(self.calls),
            date=datetime.now(timezone.utc),
            chat=Chat(id=method.chat_id, type="private"),  # type: ignore[arg-type]
        )

    async def stream_content(
        self, url: str, headers: Optional[dict] = None, timeout: int = 30,
        chunk_size: int = 65536, raise_for_status: bool = True,
    ) -> AsyncGenerator[bytes, None]:
        yield b""

    async def close(self) -> None:
        return None


@pytest.fixture()
def session() -> RecordingSession:
    return RecordingSession()


@pytest.fixture()
def bot(session: RecordingSession) -> Bot:
    return Bot(token="1:test", session=session)


@pytest.fixture()
def sender(bot: Bot) -> TelegramSender:
    return TelegramSender(bot)


def _write(tmp_path: Path, name: str, size: int = 1024) -> Path:
    f = tmp_path / name
    f.write_bytes(b"x" * size)
    return f


async def test_send_video_wraps_path_in_fsinputfile(
    sender: TelegramSender, session: RecordingSession, tmp_path: Path
) -> None:
    """بازتولید مستقیم باگ کاربر: ویدئوی mp4 باید با SendVideo+FSInputFile برود."""
    path = _write(tmp_path, "reel.mp4")
    ok = await sender.send(1001, path)
    assert ok is True
    # فقط SendVideo — بدون سقوط به fallback سند
    assert [type(c) for c in session.calls] == [SendVideo]
    video = session.calls[0].video
    assert isinstance(video, FSInputFile)
    assert Path(video.path) == path
    assert video.filename == "reel.mp4"


async def test_send_photo_wraps_path_in_fsinputfile(
    sender: TelegramSender, session: RecordingSession, tmp_path: Path
) -> None:
    """تصویر باید با SendPhoto+FSInputFile ارسال شود نه Path خام."""
    path = _write(tmp_path, "post.jpg")
    ok = await sender.send(1001, path)
    assert ok is True
    assert [type(c) for c in session.calls] == [SendPhoto]
    photo = session.calls[0].photo
    assert isinstance(photo, FSInputFile)
    assert Path(photo.path) == path


async def test_video_failure_falls_back_to_document_with_fsinputfile(
    sender: TelegramSender, session: RecordingSession, tmp_path: Path
) -> None:
    """اگر SendVideo شکست بخورد، fallback به SendDocument هم باید معتبر باشد."""
    session.fail_on = {SendVideo}
    path = _write(tmp_path, "reel.mp4")
    ok = await sender.send(1001, path)
    assert ok is True
    assert [type(c) for c in session.calls] == [SendVideo, SendDocument]
    document = session.calls[1].document
    assert isinstance(document, FSInputFile)
    assert Path(document.path) == path


async def test_oversize_returns_false_and_notifies(
    bot: Bot, session: RecordingSession, tmp_path: Path
) -> None:
    """فایل بزرگ‌تر از سقف: فشرده‌سازی ناموفق → پیام خطا → False."""
    s = TelegramSender(bot, use_local_api=False)
    s.limit = 512  # فایل ۱KB آزمونی «بزرگ» می‌شود
    path = _write(tmp_path, "big.mp4", size=1024)
    ok = await s.send(1001, path)
    assert ok is False
    assert [type(c) for c in session.calls] == [SendMessage]
    assert "حجم فایل" in str(session.calls[0].text)
