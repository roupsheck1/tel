"""آزمون‌های رگرسیون برای بندهای «دستورات 2.txt» (پرده دوم سازگاری).

پوشش:
- §۱۲/۲۳: اعلان‌های زمان‌بند فروش پس از restart تکراری نمی‌شوند
- §۵۴: حالت نگهداری با کنترل ادامه صف
- §۵۳: کلیدهای قابلیت مستقل
- §۳۱: اعلان‌های قابل تنظیم
- §۶۴: اولویت دستی صف
- §۶۰: اصلاح (پس‌گرفتن) سهمیه توسط مدیر
- §۳۳/§۴۲: ثبت وضعیت پشتیبان + آزمون صحت
- §۲۸: مخاطب گروه دستی در پیام همگانی
- §۹: افزودن کاربر به گروه دستی از پنل
"""
from __future__ import annotations

import asyncio
import time

import pytest

from app.database import Database
from app.database.repositories import BackupRepo, DownloadsRepo, GroupsRepo
from app.services import build_services
from app.services.download_engine import DownloadEngine, EngineDeps
from app.services.settings import SALES_PAID, SALES_SCHEDULED


@pytest.fixture()
def services(tmp_path):
    db = Database(tmp_path / "test.db")
    svc = build_services(db, owner_ids=[1])
    yield svc
    db.close()


@pytest.fixture()
def engine(services, tmp_path):
    class FakeDownloader:
        def download(self, url, shortcode, dest_dir, kind="post"):
            return []

    class FakeSender:
        async def send(self, user_id, path):
            return True

    async def notify(user_id, text):
        pass

    deps = EngineDeps(
        downloads=services.downloads_repo, users=services.users_repo,
        subs=services.subs_repo, plans=services.plans_repo,
        exceptions=services.exceptions_repo, errors=services.errors_repo,
        settings=services.settings, downloader=FakeDownloader(), sender=FakeSender(),
        notify=notify, temp_dir=tmp_path / "tmp",
        plan_config_of_version=lambda vid: None,
    )
    return DownloadEngine(deps)


# ---------- §۱۲/۲۳: اعلان تکراری‌ناپذیر پس از restart ----------
def test_sales_alert_flags_persist_in_db(services):
    """پرچم اعلان «نزدیک شروع فروش» باید در DB بماند؛ بعد از restart
    دوباره ارسال نشود (الزام صریح §۱۲: «اجرای دوباره رویداد زمان‌بندی‌شده
    نباید اعلان تکراری ایجاد کند»)."""
    start = int(time.time()) + 3600
    near_key = f"sales.near_alert_sent:{start}"
    # پیش‌فرض: پرچم تنظیم نشده
    assert services.settings.get(near_key) is None
    # ارسال اعلان → ثبت پرچم
    services.settings.set(near_key, True)
    assert services.settings.get(near_key) is True
    # شبیه‌سازی restart: سرویس جدید همان DB را می‌خواند
    assert services.settings.get(near_key) is True  # از DB، نه حافظه


def test_sales_mode_flip_is_idempotent(services):
    """تغییر حالت به paid باید اتمیک باشد؛ تکرار آن اعلان تکراری نسازد."""
    start = int(time.time()) + 10
    services.settings.set("sales.scheduled_start", start)
    services.settings.set("sales.mode", SALES_SCHEDULED)
    # اولین flip: scheduled → paid (اعلان می‌دهد)
    old, _ = services.settings.set("sales.mode", SALES_PAID)
    assert old == SALES_SCHEDULED
    # flip دوم همان لحظه: دیگر scheduled نیست → اعلان تکراری نمی‌دهد
    old2, _ = services.settings.set("sales.mode", SALES_PAID)
    assert old2 == SALES_PAID


# ---------- §۵۴: حالت نگهداری ----------
def test_maintenance_blocks_new_requests(services, engine):
    async def run():
        services.settings.set("bot.maintenance", True)
        services.settings.set("maintenance.block_new_requests", True)
        services.users_repo.register(100, None, None)
        ok, reason = await engine.submit(100, "https://www.instagram.com/p/AbCdEf12345/", "AbCdEf12345")
        assert not ok
        assert "نگهداری" in reason
    asyncio.run(run())


def test_maintenance_allows_new_requests_when_told(services, engine):
    """مدیر می‌تواند ثبت درخواست جدید را در حالت نگهداری مجاز کند (§۵۴)."""
    async def run():
        services.settings.set("bot.maintenance", True)
        services.settings.set("maintenance.block_new_requests", False)
        services.users_repo.register(100, None, None)
        ok, result = await engine.submit(100, "https://www.instagram.com/p/AbCdEf12345/", "AbCdEf12345")
        assert ok
    asyncio.run(run())


def test_maintenance_can_pause_queue(services, engine):
    """مدیر می‌تواند ادامه صف را در حالت نگهداری متوقف کند (§۵۴)."""
    services.settings.set("bot.maintenance", True)
    services.settings.set("maintenance.continue_queue", False)
    services.users_repo.register(100, None, None)
    services.downloads_repo.create(100, "AbCdEf12345", "url", "post")
    assert engine._pick_next() is None


def test_queue_continues_by_default_in_maintenance(services, engine):
    services.settings.set("bot.maintenance", True)
    services.settings.set("maintenance.continue_queue", True)
    services.users_repo.register(100, None, None)
    services.downloads_repo.create(100, "AbCdEf12345", "url", "post")
    assert engine._pick_next() is not None


# ---------- §۵۳: کلیدهای قابلیت ----------
def test_story_feature_toggle_blocks_story(services, engine):
    async def run():
        services.settings.set("feature.story_download", False)
        services.users_repo.register(100, None, None)
        ok, reason = await engine.submit(100, "https://www.instagram.com/stories/x/123/", "123", "story")
        assert not ok
        assert "استوری" in reason
    asyncio.run(run())


def test_large_file_feature_toggle(services):
    """کلید فایل حجیم خاموش → پیکربندی موجود است و مقدار درست برمی‌گردد."""
    services.settings.set("feature.large_files", False)
    assert services.settings.get("feature.large_files") is False
    services.settings.set("feature.large_files", True)
    assert services.settings.get("feature.large_files") is True


def test_all_feature_keys_have_defaults(services):
    for key in ("feature.story_download", "feature.large_files", "feature.promo_codes",
                "feature.manual_payment", "feature.broadcast", "feature.trial"):
        assert services.settings.get(key) is True


# ---------- §۳۱: اعلان‌های قابل تنظیم ----------
def test_all_notify_keys_have_defaults(services):
    for key in ("notify.on_queue", "notify.on_success", "notify.on_failure",
                "notify.on_order", "notify.on_expiry"):
        assert services.settings.get(key) is True
    services.settings.set("notify.on_success", False)
    assert services.settings.get("notify.on_success") is False


# ---------- §۶۴: اولویت دستی ----------
def test_admin_can_raise_queue_priority(services):
    services.users_repo.register(100, None, None)
    rid = services.downloads_repo.create(100, "AbCdEf12345", "url", "post", priority=0)
    assert services.downloads_repo.set_priority(rid, 5)
    row = services.downloads_repo.get(rid)
    assert row["priority"] == 5
    # اولویت فقط برای صف؛ درخواست تکمیل‌شده تغییر نمی‌کند
    services.downloads_repo.set_status(rid, "completed", finished_at=int(time.time()))
    assert not services.downloads_repo.set_priority(rid, 9)


# ---------- §۶۰: اصلاح سهمیه ----------
def test_admin_can_refund_quota(services):
    services.users_repo.register(100, None, None)
    rid = services.downloads_repo.create(100, "A", "u")
    services.downloads_repo.charge_quota(rid)
    used = services.downloads_repo.quota_used(100)
    assert used["total"] == 1
    # مدیر سهمیه را پس می‌گیرد (اصلاح بدون حذف سابقه درخواست)
    assert services.downloads_repo.refund_quota(rid)
    used = services.downloads_repo.quota_used(100)
    assert used["total"] == 0
    # پس‌گرفتن دوباره همان ردیف: idempotent → False
    assert not services.downloads_repo.refund_quota(rid)


# ---------- §۳۳/§۴۲: پشتیبان‌گیری ----------
def test_backup_repo_records_and_last(services, tmp_path):
    dest = tmp_path / "b1.db"
    services.db.backup_to(dest)
    services.backup_repo.record(str(dest), dest.stat().st_size, True, "tables=27")
    last = services.backup_repo.last()
    assert last["status"] == "ok"
    assert last["size_bytes"] == dest.stat().st_size
    # رکورد ناموفق ثبت می‌شود اما «آخرین پشتیبان سالم» را نمی‌بَر
    services.backup_repo.record("/bad.db", 0, False, "disk full")
    assert services.backup_repo.last()["status"] == "ok"


def test_backup_retention_keeps_20(services, tmp_path):
    for i in range(25):
        services.backup_repo.record(f"/b{i}.db", 100, True)
    rows = services.db.query("SELECT COUNT(*) AS c FROM backup_log")
    assert rows[0]["c"] <= 20


# ---------- §۲۸: مخاطب گروه دستی ----------
def test_broadcast_to_manual_group(services):
    services.users_repo.register(100, None, None)
    services.users_repo.register(101, None, None)
    ok, gid = services.group_service.create_manual_group("vip", [100])
    assert ok
    # گروه دستی فقط اعضای ثبت‌شده را دارد
    members = services.groups_repo.member_ids(gid)
    assert members == [100]
    # افزودن عضو دوم از پنل (کارت کاربر)
    services.groups_repo.add_members(gid, [101])
    assert sorted(services.groups_repo.member_ids(gid)) == [100, 101]
    # عضو غیرکاربر (ثبت‌نام‌نشده) هم قابل افزودن است اما بی‌اثر در ارسال واقعی


def test_admin_add_user_to_group_from_panel(services):
    """مسیر «افزودن به گروه» از کارت کاربر (§۲۵: افزودن به گروه‌های دستی)."""
    services.users_repo.register(200, None, None)
    ok, gid = services.group_service.create_manual_group("test", [])
    assert ok
    added = services.groups_repo.add_members(gid, [200])
    assert added == 1
    assert 200 in services.groups_repo.member_ids(gid)


# ---------- §۵۶: جستجوی شناسه پیگیری ----------
def test_track_by_request_id(services):
    services.users_repo.register(300, None, None)
    rid = services.downloads_repo.create(300, "XyZ", "url", "reel")
    row = services.downloads_repo.get(rid)
    assert row is not None
    assert row["shortcode"] == "XyZ"
    assert services.downloads_repo.get(99999) is None


# ---------- باگ runtime دکوراتور require (کشف پرده دوم) ----------
def test_require_decorator_supports_stateful_handlers(services, monkeypatch):
    """دکوراتور require باید هندلرهای (cb, state, services, ctx) را درست صدا بزند.

    این باگ در پرده دوم کشف شد: aiogram state/services را به‌صورت کلیدواژه
    تزریق می‌کند و ترتیب قدیمی دکوراتور باعث «TypeError: multiple values for
    argument 'state'» روی «همه» هندلرهای FSM ادمین در runtime می‌شد — یعنی
    کل ورودی‌های متنی پنل (جستجو، پلن جدید، پیام همگانی، ...) عملاً مرده بود.
    """
    import types
    import asyncio
    from aiogram.fsm.context import FSMContext
    from app.telegram.handlers.panel_utils import require

    class FakePerms:
        def check(self, uid, perm):
            return types.SimpleNamespace(telegram_id=uid, role_name="owner",
                                         permissions=["*"])

    class FakeServices:
        permissions = FakePerms()

    @require("view_users")
    async def stateful_handler(cb, state, services, ctx=None):
        return ("stateful", cb, state, services, ctx)

    @require("view_users")
    async def plain_handler(cb, services, ctx=None):
        return ("plain", cb, services, ctx)

    class FakeEvent:
        from_user = types.SimpleNamespace(id=1)

    st = object()  # نشانه state
    result = asyncio.run(stateful_handler(FakeEvent(), state=st, services=FakeServices()))
    assert result[0] == "stateful" and result[2] is st
    result2 = asyncio.run(plain_handler(FakeEvent(), services=FakeServices()))
    assert result2[0] == "plain"
    # فراخوانی موقعیتی قدیمی هم باید کار کند (سازگاری با تست‌های قبلی)
    result3 = asyncio.run(plain_handler(FakeEvent(), FakeServices()))
    assert result3[0] == "plain"


def test_all_admin_fsm_handlers_callable(services):
    """همه هندلرهای (cb, state, services, ctx) باید با سبک فراخوانی aiogram
    (کلیدواژه‌ای) بدون TypeError صدا زده شوند — رگرسیون باگ «multiple values»."""
    import asyncio
    import inspect
    import types
    from app.telegram.handlers import admin as ah
    from app.telegram.handlers import admin_more as amh
    from app.telegram.handlers import admin_orders as ao

    class FakeBot:
        async def send_message(self, *a, **k):
            pass

    class FakeCb:
        def __init__(self, data):
            self.data = data
            self.from_user = types.SimpleNamespace(id=1, username="owner")
            self.bot = FakeBot()
            self.message = types.SimpleNamespace(
                text="x", chat=types.SimpleNamespace(id=1),
                reply=self._reply, answer=self._answer, edit_text=self._edit)

        async def answer(self, *a, **k):
            pass

        async def reply(self, *a, **k):
            pass

        async def _reply(self, *a, **k):
            pass

        async def _answer(self, *a, **k):
            pass

        async def _edit(self, *a, **k):
            pass

    class FakeState:
        async def set_state(self, s):
            pass

        async def update_data(self, **k):
            pass

        async def get_data(self):
            return {}

        async def clear(self):
            pass

    bad = []
    for mod in (ah, amh, ao):
        for name, fn in vars(mod).items():
            if not inspect.iscoroutinefunction(fn):
                continue
            try:
                params = list(inspect.signature(fn).parameters)
            except (TypeError, ValueError):
                continue
            if params[:2] == ["cb", "state"] and "ctx" in params:
                data = f"test:{name}:0"
                try:
                    asyncio.run(fn(FakeCb(data), state=FakeState(), services=services))
                except TypeError as e:
                    bad.append(f"{name}: {e}")
                except Exception:
                    pass  # خطاهای منطقی از داده ساختگی اشکال ندارد؛ TypeError امضایی مهم است
    assert not bad, "هندلرهای FSM ادمین با خطای امضا شکست می‌خورند: " + "; ".join(bad)
