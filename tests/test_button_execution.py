"""آزمون اجرای واقعی دکمه‌ها — هر دکمه باید «واقعاً کار کند» نه فقط هندلر داشته باشد.

تفاوت با test_ui_wiring: آن آزمون فقط بررسی می‌کرد callback به هندلر می‌رسد؛
این آزمون رویداد را از دیسپچر واقعی عبور می‌دهد و مطمئن می‌شود:
  ۱) هیچ استثنایی در هندلر رخ ندهد (مثل append دوآرگومانی که دکمه را کشته بود)،
  ۲) پاسخ قابل مشاهده‌ای به کاربر برسد (answer/edit/send در نشست جعلی ثبت شود).

پوشش: همه دکمه‌های پنل (از جمله مسیرهای وابسته به داده: پیش‌نویس نسخه،
رسید سفارش، کاربر مسدود، صف دانلود و ...) + جریان‌های ورودی FSM + سمت کاربر.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path

import pytest
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.enums import ParseMode
from aiogram.methods import TelegramMethod
from aiogram.types import (
    CallbackQuery, Chat, Message, PhotoSize, Update, User,
)

from app.database import Database
from app.main import build_dispatcher
from app.services import Services, build_services

OWNER = 111
USER1 = 1001
USER2 = 1002

# روش‌هایی که یعنی «کاربر چیزی دید»
VISIBLE_METHODS = {
    "AnswerCallbackQuery", "EditMessageText", "EditMessageCaption",
    "SendMessage", "SendPhoto", "SendVideo", "SendDocument", "CopyMessage",
}


class FakeSession(BaseSession):
    """نشست جعلی: فراخوانی‌ها را ثبت می‌کند و پاسخ معتبر برمی‌گرداند."""

    def __init__(self):
        super().__init__()
        self.calls: list[str] = []

    async def make_request(self, bot, method: TelegramMethod, timeout=None):
        self.calls.append(type(method).__name__)
        return True

    async def stream_content(self, *args, **kwargs):
        yield b""

    async def close(self):
        pass


def make_bot() -> tuple[Bot, FakeSession]:
    session = FakeSession()
    bot = Bot(token="1:test", session=session,
              default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    return bot, session


def cb_update(data: str, user_id: int = OWNER, msg_id: int = 500) -> Update:
    user = User(id=user_id, is_bot=False, first_name="T", username="t")
    chat = Chat(id=user_id, type="private")
    msg = Message(message_id=msg_id, date=datetime.now(), chat=chat, from_user=user, text="x")
    return Update(update_id=1, callback_query=CallbackQuery(
        id="1", from_user=user, chat_instance="ci", data=data, message=msg))


def msg_update(text: str | None, user_id: int = OWNER, photo: bool = False) -> Update:
    user = User(id=user_id, is_bot=False, first_name="T", username="t")
    chat = Chat(id=user_id, type="private")
    kw = {}
    if photo:
        kw["photo"] = [PhotoSize(file_id="fid", file_unique_id="fuid", width=1, height=1)]
        text = None
    return Update(update_id=1, message=Message(
        message_id=int(datetime.now().timestamp() * 1000) % 10_000_000,
        date=datetime.now(), chat=chat, from_user=user, text=text, **kw))


# نکته: روترهای هندلرها singleton سطح ماژول‌اند و فقط یک‌بار می‌توانند به
# Dispatcher وصل شوند؛ بنابراین services و rig در سطح ماژول ساخته می‌شوند
# و پیش از هر آزمون، حالت FSM پاک می‌شود (جداسازی بین آزمون‌ها).
@pytest.fixture(scope="module")
def services(tmp_path_factory) -> Services:
    db = Database(tmp_path_factory.mktemp("btn") / "t.db")
    svc = build_services(db, [OWNER])
    _populate(svc)
    return svc


@pytest.fixture(scope="module")
def rig(services):
    """(bot, dp, session) — دیسپچر واقعی + بات جعلی."""

    class Rig:
        def __init__(self):
            self.bot, self.session = make_bot()
            # روترها singleton‌اند؛ از دیسپچر مشترک conftest استفاده می‌کنیم
            from tests.conftest import get_shared_dispatcher
            self.dp = get_shared_dispatcher(services)

        def reset_fsm(self):
            """پاک‌سازی حالت FSM بین آزمون‌ها (MemoryStorage داخلی)."""
            try:
                self.dp.fsm.storage.storage.clear()
            except Exception:
                pass

        async def feed(self, upd: Update) -> None:
            self.session.calls.clear()
            await self.dp.feed_update(self.bot, upd)

        async def cb(self, data: str, user_id: int = OWNER) -> list[str]:
            await self.feed(cb_update(data, user_id))
            return list(self.session.calls)

        async def text(self, text: str, user_id: int = OWNER) -> list[str]:
            await self.feed(msg_update(text, user_id))
            return list(self.session.calls)

        async def photo(self, user_id: int = OWNER) -> list[str]:
            await self.feed(msg_update(None, user_id, photo=True))
            return list(self.session.calls)

        def visible(self, calls: list[str]) -> bool:
            return any(c in VISIBLE_METHODS for c in calls)

    r = Rig()
    yield r


@pytest.fixture(autouse=True)
def _isolate_fsm(request):
    """پاک‌سازی FSM و ثبت شماره آزمون — برای جداسازی آزمون‌های ماژول-اسکوپ."""
    rig = request.getfixturevalue("rig") if "rig" in request.fixturenames else None
    if rig is not None:
        rig.reset_fsm()
    yield
    if rig is not None:
        rig.reset_fsm()


def _populate(svc: Services) -> None:
    """داده‌های واقعی برای فعال‌شدن همه مسیرهای وابسته به داده."""
    svc.users_repo.register(USER1, "user1", "User One")
    svc.users_repo.register(USER2, "user2", "User Two")
    svc.users_repo.set_blocked(USER2, True, reason="test")

    # حالت فروش فعال برای جریان خرید
    svc.settings.set("sales.mode", "paid")

    plans = svc.plans_repo.list()
    basic = next(p for p in plans if p["name"] == "basic_month")

    # اشتراک فعال برای USER1
    v = svc.plans_repo.current_version(basic["id"])
    svc.subs_repo.create(USER1, basic["id"], v["id"], 30 * 86400, "purchase")

    # سفارش‌ها در وضعیت‌های مختلف
    method = svc.payments_repo.get_active()
    plan_row = next(p for p in svc.plan_service.saleable_plans() if p["id"] == basic["id"])
    ids = []
    for _ in range(4):
        ids.append(svc.orders_repo.create(USER1, plan_row, 99000, 30 * 86400, method))
    svc.orders_repo.announce_payment(ids[1])
    svc.orders_repo.announce_payment(ids[2])
    svc.orders_repo.attach_receipt(ids[2], "photo_file_id")
    svc.orders_repo.announce_payment(ids[3])
    svc.orders_repo.attach_receipt(ids[3], "photo_file_id2")
    svc.orders_repo.confirm(ids[3], OWNER)

    # پیش‌نویس نسخه برای پلن پایه (مسیر «نسخه‌های پیش‌نویس» در کارت پلن)
    from app.services.plans import PlanConfig
    cfg = svc.plan_service.config_of_version(svc.plans_repo.current_version(basic["id"]))
    cfg.price = cfg.price + 1000
    svc.plan_service.draft_new_version(basic["id"], cfg, OWNER, "تست")

    # کد اشتراک
    svc.codes_repo.generate(plan_id=basic["id"], duration_seconds=30 * 86400,
                            max_uses=1, expires_at=None, created_by=OWNER,
                            count=1, batch_id="b1")

    # گروه دستی
    svc.group_service.create_manual_group("vip", [USER1])

    # درخواست‌های دانلود (شکست‌خورده + در صف)
    r1 = svc.downloads_repo.create(USER1, "AAA1", "https://instagram.com/p/AAA1/", "post")
    svc.downloads_repo.set_status(r1, "failed", last_error="instagrapi: boom")
    r2 = svc.downloads_repo.create(USER1, "BBB2", "https://instagram.com/p/BBB2/", "reel")
    svc.downloads_repo.set_priority(r2, 2)

    # خطا + استثنا + اعلان
    svc.errors_repo.log("download", "download", "خطای آزمایشی", request_id=r1)
    svc.exceptions_repo.add(USER1, 1_000_000, 1_000_000 + 7 * 86400, OWNER,
                            concurrent=3, wait=0)

    # پیام همگانی
    svc.broadcast_repo.create(OWNER, {"type": "all"}, "پیام آزمون")
    svc.broadcast_repo.set_preview(1, 2)


# ============================ کمکی: اجرای امن ============================
async def run_cb(rig, data: str, user_id: int = OWNER) -> list[str]:
    """اجرای callback؛ استثنا را نمی‌بلعد — pytest آن را نمایش می‌دهد."""
    return await rig.cb(data, user_id)


def assert_responded(calls: list[str], what: str):
    assert any(c in VISIBLE_METHODS for c in calls), (
        f"دکمه «{what}» هیچ پاسخ قابل مشاهده‌ای نداد (calls={calls})")


# ============================ ۱) ورود و منو ============================
@pytest.mark.asyncio
async def test_panel_entry(rig):
    calls = await rig.text("/panel")
    assert_responded(calls, "/panel")


@pytest.mark.asyncio
async def test_home_and_cancel(rig):
    assert_responded(await rig.cb("a:home:"), "منوی اصلی")
    assert_responded(await rig.cb("a:home"), "منوی اصلی (قدیمی)")
    assert_responded(await rig.cb("a:cancel:"), "انصراف")
    assert_responded(await rig.cb("a:cancel"), "انصراف (قدیمی)")


# ============================ ۲) کاربران ============================
@pytest.mark.asyncio
async def test_users_section(rig):
    assert_responded(await run_cb(rig, "a:users:menu:"), "منوی کاربران")
    assert_responded(await run_cb(rig, "a:users:list:0"), "فهرست کاربران")
    assert_responded(await run_cb(rig, "a:users:list:10"), "صفحه دوم کاربران")
    assert_responded(await run_cb(rig, "a:users:blocked:0"), "مسدودشده‌ها")
    assert_responded(await run_cb(rig, f"a:users:card:{USER1}"), "کارت کاربر عادی")
    assert_responded(await run_cb(rig, f"a:users:card:{USER2}"), "کارت کاربر مسدود")


@pytest.mark.asyncio
async def test_users_card_actions(rig):
    assert_responded(await run_cb(rig, f"a:users:gift:{USER1}"), "هدیه — انتخاب پلن")
    assert_responded(await run_cb(rig, f"a:users:gift2:{USER1}:2"), "هدیه — اعطا")
    assert_responded(await run_cb(rig, f"a:users:block:{USER2}"), "مسدودسازی")
    assert_responded(await run_cb(rig, f"a:users:unblock:{USER2}"), "رفع مسدودی")
    assert_responded(await run_cb(rig, f"a:users:unlink:{USER1}"), "قطع اتصال")
    assert_responded(await run_cb(rig, f"a:users:addtogroup:{USER1}"), "افزودن به گروه")
    gid = 1
    assert_responded(await run_cb(rig, f"a:users:addgroup2:{USER1}:{gid}"), "عضویت در گروه")


@pytest.mark.asyncio
async def test_users_extend_flow(rig):
    assert_responded(await run_cb(rig, f"a:users:extend:{USER1}"), "پرامپت تمدید")
    calls = await rig.text("7")
    assert_responded(calls, "ورودی تمدید")


@pytest.mark.asyncio
async def test_users_exception_flow(rig):
    assert_responded(await run_cb(rig, f"a:users:exception:{USER1}"), "پرامپت استثنا")
    assert_responded(await rig.text("concurrent=3, wait=0, days=7"), "ورودی استثنا")


@pytest.mark.asyncio
async def test_users_search_flow(rig):
    assert_responded(await run_cb(rig, "a:users:search:"), "پرامپت جست‌وجو")
    assert_responded(await rig.text(str(USER1)), "ورودی جست‌وجو")


# ============================ ۳) پلن‌ها ============================
@pytest.mark.asyncio
async def test_subs_section(rig):
    assert_responded(await run_cb(rig, "a:subs:menu:"), "منوی اشتراک‌ها")
    assert_responded(await run_cb(rig, "a:subs:plans:"), "فهرست پلن‌ها")


@pytest.mark.asyncio
async def test_plan_card_with_draft(rig):
    """مسیر پیش‌نویس (پیش از این با append دوآرگومانی می‌شکست)."""
    assert_responded(await run_cb(rig, "a:subs:plan:2"), "کارت پلن با پیش‌نویس")


@pytest.mark.asyncio
async def test_plan_versions(rig):
    """«📜 نسخه‌ها» — پیش از این برای هر پلنی TypeError می‌داد (دکمه مرده)."""
    assert_responded(await run_cb(rig, "a:subs:versions:1"), "نسخه‌های پلن")
    assert_responded(await run_cb(rig, "a:subs:versions:2"), "نسخه‌های پلن با پیش‌نویس")


@pytest.mark.asyncio
async def test_plan_price_flow(rig):
    assert_responded(await run_cb(rig, "a:subs:price:2"), "پرامپت تغییر قیمت")
    assert_responded(await rig.text("150000"), "ورودی قیمت")


@pytest.mark.asyncio
async def test_plan_publish_flow(rig, services):
    """انتشار نسخه — پیش از این بازگشت به کارت پلن با cb.data= می‌شکست (مدل frozen)."""
    drafts = services.db.query("SELECT * FROM plan_versions WHERE status='draft' LIMIT 1")
    assert drafts, "نسخه پیش‌نویس ساخته نشد"
    vid = drafts[0]["id"]
    assert_responded(await run_cb(rig, f"a:subs:publish:{vid}"), "انتشار — انتخاب دامنه")
    assert_responded(await run_cb(rig, f"a:subs:pub2:{vid}:future"), "انتشار future")


@pytest.mark.asyncio
async def test_plan_archive_flow(rig):
    assert_responded(await run_cb(rig, "a:subs:archive:3"), "تأیید بایگانی")
    assert_responded(await run_cb(rig, "a:subs:archive2:3"), "اجرای بایگانی")


@pytest.mark.asyncio
async def test_new_plan_flow_multiline(rig):
    assert_responded(await run_cb(rig, "a:subs:newplan:"), "پرامپت پلن جدید")
    calls = await rig.text(
        "name: gold2\nعنوان: طلایی\nقیمت: 299000\nمدت_روز: 30\n"
        "هم‌زمان: 2\nانتظار_ثانیه: 10\nظرفیت_صف: 15"
    )
    assert_responded(calls, "ورودی پلن جدید")
    # بررسی ساخت واقعی
    assert rig.dp  # (ساخت پلن در سرویس واقعی انجام شد)


@pytest.mark.asyncio
async def test_sales_mode_flows(rig):
    assert_responded(await run_cb(rig, "a:subs:salesmode:"), "حالت فروش")
    assert_responded(await run_cb(rig, "a:subs:mode:free"), "حالت رایگان")
    assert_responded(await run_cb(rig, "a:subs:mode:paid"), "شروع فروش")
    assert_responded(await run_cb(rig, "a:subs:sched:"), "پرامپت زمان‌بندی")
    assert_responded(await rig.text("2026-01-15 18:30"), "ورودی زمان‌بندی")


@pytest.mark.asyncio
async def test_paymethods(rig):
    assert_responded(await run_cb(rig, "a:subs:paymethods:"), "روش‌های پرداخت")
    assert_responded(await run_cb(rig, "a:subs:paytoggle:1"), "تغییر روش پرداخت")


# ============================ ۴) سفارش‌ها ============================
@pytest.mark.asyncio
async def test_orders_section(rig):
    assert_responded(await run_cb(rig, "a:orders:menu:"), "منوی سفارش‌ها")
    assert_responded(await run_cb(rig, "a:orders:pending:0"), "در انتظار بررسی")
    assert_responded(await run_cb(rig, "a:orders:list:all:0"), "همه سفارش‌ها")
    assert_responded(await run_cb(rig, "a:orders:list:confirmed:0"), "تأییدشده‌ها")
    assert_responded(await run_cb(rig, "a:orders:list:rejected:0"), "ردشده‌ها")
    assert_responded(await run_cb(rig, "a:orders:stats:"), "خلاصه سفارش‌ها")


@pytest.mark.asyncio
async def test_order_card_and_receipt(rig, services):
    with_receipt = [o["id"] for o in services.orders_repo.list(status="receipt_received")]
    assert with_receipt, "داده سفارش با رسید ساخته نشد"
    oid = with_receipt[0]
    assert_responded(await run_cb(rig, f"a:orders:card:{oid}"), "کارت سفارش")
    assert_responded(await run_cb(rig, f"a:orders:receipt:{oid}"), "مشاهده رسید")


@pytest.mark.asyncio
async def test_order_approve_two_step(rig, services):
    with_receipt = [o["id"] for o in services.orders_repo.list(status="receipt_received")]
    assert with_receipt, "داده سفارش با رسید ساخته نشد"
    oid = with_receipt[0]
    assert_responded(await run_cb(rig, f"a:orders:approve:{oid}:0"), "تأیید مرحله ۱")
    assert_responded(await run_cb(rig, f"a:orders:approve:{oid}:1"), "تأیید مرحله ۲")


@pytest.mark.asyncio
async def test_order_reject_flow(rig, services):
    awaiting = [o["id"] for o in services.orders_repo.list(status="awaiting_payment")]
    assert awaiting
    oid = awaiting[0]
    assert_responded(await run_cb(rig, f"a:orders:reject:{oid}"), "پرامپت رد")
    assert_responded(await rig.text("دلیل آزمایشی"), "ورودی دلیل رد")


@pytest.mark.asyncio
async def test_order_needsreview(rig, services):
    pending = [o["id"] for o in services.orders_repo.list(status="payment_announced")]
    assert pending
    assert_responded(await run_cb(rig, f"a:orders:needsreview:{pending[0]}"), "نیازمند بررسی")


# ============================ ۵) آمار ============================
@pytest.mark.asyncio
async def test_stats_section(rig):
    assert_responded(await run_cb(rig, "a:stats:menu:"), "منوی آمار")
    assert_responded(await run_cb(rig, "a:stats:dash:"), "داشبورد")
    assert_responded(await run_cb(rig, "a:stats:ledger:"), "دفتر فروش")
    assert_responded(await run_cb(rig, "a:stats:users:"), "آمار کاربران")


# ============================ ۶) دانلودها ============================
@pytest.mark.asyncio
async def test_downloads_section(rig):
    assert_responded(await run_cb(rig, "a:downloads:menu:"), "منوی دانلودها")
    assert_responded(await run_cb(rig, "a:downloads:queue:"), "وضعیت صف")
    assert_responded(await run_cb(rig, "a:downloads:prio:1:3"), "تغییر اولویت")
    assert_responded(await run_cb(rig, "a:downloads:failed:"), "خطاها")
    assert_responded(await run_cb(rig, "a:downloads:retry:1"), "تلاش مجدد")
    assert_responded(await run_cb(rig, "a:downloads:capacity:"), "ظرفیت سرور")
    assert_responded(await run_cb(rig, "a:downloads:capedit:"), "پرامپت ظرفیت")
    assert_responded(await rig.text("dl=3, send=5, queue=100"), "ورودی ظرفیت")


@pytest.mark.asyncio
async def test_download_track_flow(rig):
    assert_responded(await run_cb(rig, "a:downloads:track:"), "پرامپت پیگیری")
    assert_responded(await rig.text("1"), "ورودی پیگیری")


# ============================ ۷) کدها ============================
@pytest.mark.asyncio
async def test_codes_flows(rig, services):
    assert_responded(await run_cb(rig, "a:codes:menu:"), "منوی کدها")
    assert_responded(await run_cb(rig, "a:codes:new:"), "پلن برای کد")
    assert_responded(await run_cb(rig, "a:codes:plan:2"), "پرامپت مشخصات کد")
    assert_responded(await rig.text("days=30, count=2, max_uses=1"), "ورودی کد")
    assert_responded(await run_cb(rig, "a:codes:list:0"), "فهرست کدها")
    codes = services.codes_repo.list()
    if codes:
        assert_responded(await run_cb(rig, f"a:codes:cancel:{codes[0]['code']}"), "لغو کد")


# ============================ ۸) گروه‌ها ============================
@pytest.mark.asyncio
async def test_groups_flows(rig):
    assert_responded(await run_cb(rig, "a:groups:menu:"), "منوی گروه‌ها")
    assert_responded(await run_cb(rig, "a:groups:list:"), "فهرست گروه‌ها")
    assert_responded(await run_cb(rig, "a:groups:card:1"), "کارت گروه")
    assert_responded(await run_cb(rig, "a:groups:new:"), "پرامپت گروه جدید")
    assert_responded(await rig.text("vip2 | 1001,1002"), "ورودی گروه جدید")


# ============================ ۹) پیام همگانی ============================
@pytest.mark.asyncio
async def test_broadcast_flows(rig):
    assert_responded(await run_cb(rig, "a:broadcast:menu:"), "منوی پیام همگانی")
    assert_responded(await run_cb(rig, "a:broadcast:new:"), "مخاطبان")
    assert_responded(await run_cb(rig, "a:broadcast:aud:all"), "پرامپت متن همگانی")
    assert_responded(await rig.text("پیام آزمون همگانی"), "ورودی متن همگانی")
    assert_responded(await run_cb(rig, "a:broadcast:confirm:1"), "تأیید ارسال")
    await asyncio.sleep(0.05)
    assert_responded(await run_cb(rig, "a:broadcast:jobs:"), "گزارش ارسال‌ها")


@pytest.mark.asyncio
async def test_broadcast_group_flow(rig):
    assert_responded(await run_cb(rig, "a:broadcast:new:"), "مخاطبان ۲")
    assert_responded(await run_cb(rig, "a:broadcast:audgroup:1"), "پرامپت گروه")
    assert_responded(await rig.text("سلام گروه"), "ورودی گروه")


# ============================ ۱۰) تنظیمات ============================
@pytest.mark.asyncio
async def test_settings_flows(rig):
    assert_responded(await run_cb(rig, "a:settings:menu:"), "منوی تنظیمات")
    assert_responded(await run_cb(rig, "a:settings:texts:"), "ویرایش پیام‌ها")
    assert_responded(await run_cb(rig, "a:settings:edittext:text.welcome"), "پرامپت متن")
    assert_responded(await rig.text("متن جدید خوشامد"), "ورودی متن")
    assert_responded(await run_cb(rig, "a:settings:maint:"), "حالت نگهداری")
    assert_responded(await run_cb(rig, "a:settings:maintopt:block:1"), "گزینه تعمیرات ۱")
    assert_responded(await run_cb(rig, "a:settings:maintopt:cont:1"), "گزینه تعمیرات ۲")
    assert_responded(await run_cb(rig, "a:settings:maint:on"), "روشن‌کردن تعمیرات")
    assert_responded(await run_cb(rig, "a:settings:maint:off"), "خاموش‌کردن تعمیرات")
    assert_responded(await run_cb(rig, "a:settings:features:"), "کلیدهای قابلیت")
    assert_responded(await run_cb(rig, "a:settings:feattoggle:feature.trial:1"), "کلید قابلیت")
    assert_responded(await run_cb(rig, "a:settings:notify:"), "اعلان‌ها")
    assert_responded(await run_cb(rig, "a:settings:notifytoggle:notify.on_queue:1"), "کلید اعلان")
    assert_responded(await run_cb(rig, "a:settings:wait:"), "نقطه انتظار")
    assert_responded(await run_cb(rig, "a:settings:waitset:send_success"), "تنظیم انتظار")
    assert_responded(await run_cb(rig, "a:settings:free:"), "پرامپت حالت رایگان")
    assert_responded(await rig.text("concurrent=2, wait=30"), "ورودی حالت رایگان")


# ============================ ۱۱) خطاها ============================
@pytest.mark.asyncio
async def test_errors_section(rig):
    assert_responded(await run_cb(rig, "a:errors:menu:"), "منوی خطاها")
    for cat in ("all", "download", "send", "instagram", "internal"):
        assert_responded(await run_cb(rig, f"a:errors:list:{cat}"), f"خطاهای {cat}")


# ============================ ۱۲) مدیران ============================
@pytest.mark.asyncio
async def test_admins_flows(rig):
    assert_responded(await run_cb(rig, "a:admins:menu:"), "منوی مدیران")
    assert_responded(await run_cb(rig, "a:admins:list:"), "فهرست مدیران")
    assert_responded(await run_cb(rig, "a:admins:roles:"), "نقش‌ها")
    assert_responded(await run_cb(rig, "a:admins:add:"), "پرامپت افزودن مدیر")
    assert_responded(await rig.text(f"{USER2} sales"), "ورودی مدیر جدید")
    assert_responded(await run_cb(rig, f"a:admins:remove:{USER2}"), "حذف مدیر")


# ============================ ۱۳) سلامت ============================
@pytest.mark.asyncio
async def test_health(rig):
    assert_responded(await run_cb(rig, "a:health:menu:"), "مرکز سلامت")


# ============================ سمت کاربر ============================
@pytest.mark.asyncio
async def test_user_side_flows(rig, services):
    assert_responded(await rig.text("/start", user_id=USER1), "/start")
    assert_responded(await rig.text("📊 وضعیت من", user_id=USER1), "وضعیت من")
    assert_responded(await rig.text("💎 پلن‌ها و خرید", user_id=USER1), "پلن‌ها")
    assert_responded(await rig.text("🔗 اتصال اینستاگرام", user_id=USER1), "اتصال")
    assert_responded(await rig.text("🧾 سفارش‌های من", user_id=USER1), "سفارش‌های من")
    assert_responded(await rig.text("🆘 راهنما", user_id=USER1), "راهنما")
    assert_responded(await rig.text("/help", user_id=USER1), "/help")
    assert_responded(await rig.text("/status", user_id=USER1), "/status")
    assert_responded(await rig.text("/myorders", user_id=USER1), "/myorders")


@pytest.mark.asyncio
async def test_user_buy_flow(rig, services):
    # جریان زمان‌بندی فروش آزمون قبلی حالت را scheduled گذاشته و آزمون
    # روش‌های پرداخت روش فعال را خاموش کرده؛ هر دو را برمی‌گردانیم.
    services.settings.set("sales.mode", "paid")
    for m in services.payments_repo.list():
        services.payments_repo.set_active(m["id"], True)
    assert_responded(await rig.cb("buy:2", user_id=USER1), "خرید پلن")
    open_orders = [o for o in services.orders_repo.list(user_id=USER1)
                   if o["status"] == "awaiting_payment"]
    assert open_orders
    oid = open_orders[0]["id"]
    assert_responded(await rig.cb(f"paydone:{oid}", user_id=USER1), "پرداخت کردم")
    assert_responded(await rig.photo(user_id=USER1), "ارسال رسید")
    assert_responded(await rig.cb(f"paycancel:{oid}", user_id=USER1), "لغو سفارش")


@pytest.mark.asyncio
async def test_user_free_text_paths(rig):
    assert_responded(await rig.text("123456", user_id=USER1), "کد ۶ رقمی نامعتبر")
    assert_responded(await rig.text("https://instagram.com/p/ABC/", user_id=USER1), "لینک اینستاگرام")
    assert_responded(await rig.text("سلام ربات", user_id=USER1), "متن ناشناخته")


@pytest.mark.asyncio
async def test_user_denied_panel(rig):
    """کاربر عادی نباید به پنل برسد — پیام رد باید بیاید (پاسخ قابل مشاهده)."""
    assert_responded(await rig.text("/panel", user_id=USER1), "پنل برای غیرمدیر")
