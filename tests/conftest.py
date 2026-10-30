"""ابزارهای مشترک آزمون — پایگاه داده موقت، سرویس‌ها و فیک‌ها.

آزمون‌ها کاملاً مستقل از تلگرام/اینستاگرام واقعی اجرا می‌شوند؛ دانلودر و
ارسال‌کننده جعلی جایگزین می‌شوند (بخش ۲۴ سند).
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.database import Database
from app.services import Services, build_engine, build_services
from app.services.download_engine import DownloadEngine

OWNER = 111
USER1 = 1001
USER2 = 1002


@pytest.fixture()
def db(tmp_path) -> Database:
    database = Database(tmp_path / "test.db")
    yield database
    database.close()


@pytest.fixture()
def services(db) -> Services:
    svc = build_services(db, [OWNER])
    # حالت فروش فعال برای آزمون‌های تجاری (پیش‌فرض seed، رایگان است)
    svc.settings.set("sales.mode", "paid")
    return svc


class FakeDownloader:
    """دانلودر جعلی: یک فایل نمونه می‌سازد (یا خطا می‌دهد)."""

    def __init__(self):
        self.fail = False
        self.calls: list[str] = []

    def download(self, url: str, shortcode: str, dest_dir: Path,
                 kind: str = "post") -> list[Path]:
        self.calls.append(shortcode)
        if self.fail:
            raise RuntimeError("fake download failure")
        f = dest_dir / f"{shortcode}.mp4"
        f.write_bytes(b"x" * 1024)
        return [f]


class FakeSender:
    """ارسال‌کننده جعلی با شمارش و قابلیت شکست."""

    def __init__(self):
        self.fail = False
        self.sent: list[tuple[int, Path]] = []

    async def send(self, user_id: int, path: Path) -> bool:
        if self.fail:
            return False
        self.sent.append((user_id, path))
        return True


class FakeNotifier:
    def __init__(self):
        self.messages: list[tuple[int, str]] = []

    async def __call__(self, user_id: int, text: str) -> None:
        self.messages.append((user_id, text))


@pytest.fixture()
def fake_downloader():
    return FakeDownloader()


@pytest.fixture()
def fake_sender():
    return FakeSender()


@pytest.fixture()
def notifier():
    return FakeNotifier()


@pytest.fixture()
def engine(services, fake_downloader, fake_sender, notifier, tmp_path) -> DownloadEngine:
    eng = build_engine(
        services, fake_downloader, fake_sender, notifier, tmp_path / "downloads"
    )
    # آزمون‌ها بدون حلقه پس‌زمینه، به‌صورت قطعی (deterministic) اجرا می‌شوند
    return eng


def make_paid_sub(services: Services, user_id: int, plan_name: str = "basic_month") -> int:
    """ساخت اشتراک فعال برای کاربر (با ثبت‌نام خودکار)."""
    services.user_service.register(user_id, f"u{user_id}", None)
    plan = services.plans_repo.get_by_name(plan_name)
    version = services.plans_repo.current_version(plan["id"])
    return services.subs_repo.create(user_id, plan["id"], version["id"], 86400 * 30, "gift")


# ---------------- دیسپچر مشترک (روترهای singleton) ----------------
# روترهای هندلرها singleton سطح ماژول‌اند و در هر فرآیند فقط یک‌بار می‌توانند
# به یک Dispatcher وصل شوند. آزمون‌هایی که دیسپچر واقعی می‌خواهند باید از
# همین نمونه مشترک استفاده کنند (مثلاً test_button_execution و test_routing).
_SHARED_DP = None


def get_shared_dispatcher(services=None):
    """ساخت/بازیابی دیسپچر مشترک؛ اگر قبلاً بدون services ساخته شده باشد و
    الان services واقعی داده شود، میدل‌ور کاربر به‌روز می‌شود."""
    global _SHARED_DP
    from app.main import build_dispatcher
    if _SHARED_DP is None:
        _SHARED_DP = build_dispatcher(services)
        return _SHARED_DP
    if services is not None:
        from app.telegram.middlewares import UserMiddleware
        for obs in (_SHARED_DP.message, _SHARED_DP.callback_query):
            for mw in getattr(obs.middleware, "_middlewares", []):
                if isinstance(mw, UserMiddleware) and mw.services is None:
                    mw.services = services
    return _SHARED_DP
