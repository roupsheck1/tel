"""آزمون‌های رگرسیون رابط کاربری: سیم‌کشی دکمه‌ها و پارسرهای ورودی.

پوشش:
- دکمه‌های «🏠 منوی اصلی» و «❌ انصراف» باید واقعاً به هندلر برسند
  (پیش از این فیکس، کیبورد «a:home:» می‌فرستاد ولی هندلر «a:home» را
  می‌پذیرفت؛ در نتیجه دکمه در تلگرام بی‌پاسخ می‌ماند).
- پارسر پلن جدید: هر دو شکل «کلید: مقدار» چندخطی (نمونه پنل) و
  «gold | طلایی | ...» تک‌خطی، با کلیدهای فارسی/انگلیسی و ارقام فارسی.
- parse_kv با جداکننده‌های «=»، «:» و ارقام فارسی.
"""
from __future__ import annotations

import asyncio

from aiogram.types import CallbackQuery, Chat, Message, User

from app.telegram.handlers.panel_utils import parse_kv, parse_plan_fields


# ---------------- ابزار ساخت روتر واقعی ----------------
# روترهای هندلرها singleton سطح ماژول‌اند و فقط یک‌بار می‌توانند به
# Dispatcher وصل شوند (آزمون‌های دیگر ممکن است قبلاً وصلشان کرده باشند)؛
# بنابراین به‌جای ساخت Dispatcher جدید، مستقیماً روی همان ۵ روتر اصلی
# walk می‌کنیم — فیلترها همان فیلترهای زمان اجرای واقعی‌اند.
_handlers = None


def _get_handlers():
    global _handlers
    if _handlers is None:
        from app.telegram.handlers import admin as ah
        from app.telegram.handlers import admin_more as amh
        from app.telegram.handlers import admin_orders as aoh
        from app.telegram.handlers import user as uh

        _handlers = []

        def walk(router):
            obs = getattr(router, "callback_query", None)
            if obs is not None:
                for h in obs.handlers:
                    _handlers.append(h)
            for sub in router.sub_routers:
                walk(sub)

        # ترتیب ثبت در build_dispatcher (main.py) رعایت می‌شود
        for r in (uh.router, ah.router, amh.router, aoh.router, uh.fallback):
            walk(r)
    return _handlers


def _make_event(data: str) -> CallbackQuery:
    user = User(id=1, is_bot=False, first_name="T", username="t")
    chat = Chat(id=1, type="private")
    msg = Message(message_id=1, date=0, chat=chat, from_user=user, text="x")
    return CallbackQuery(id="1", from_user=user, chat_instance="ci",
                         data=data, message=msg)


async def _routed(data: str) -> bool:
    """آیا هیچ هندلری این callback را می‌پذیرد؟"""
    handlers = _get_handlers()
    ev = _make_event(data)
    for h in handlers:
        try:
            res = await h.check(ev)
            if res and res[0] is True:
                return True
        except Exception:
            continue
    return False


# ---------------- دکمه‌های حیاتی ----------------
def test_main_menu_button_is_routed():
    """«🏠 منوی اصلی» با داده «a:home:» باید هندلر پیدا کند (رگرسیون باگ)."""
    assert asyncio.run(_routed("a:home:")) is True


def test_main_menu_button_legacy_form_is_routed():
    assert asyncio.run(_routed("a:home")) is True


def test_cancel_button_is_routed():
    """«❌ انصراف» با داده «a:cancel:» باید هندلر پیدا کند (رگرسیون باگ)."""
    assert asyncio.run(_routed("a:cancel:")) is True


def test_cancel_button_legacy_form_is_routed():
    assert asyncio.run(_routed("a:cancel")) is True


def test_every_static_panel_callback_is_routed():
    """همه دکمه‌های ثابت پنل (هر بخش) باید هندلر داشته باشند."""
    buttons = [
        "a:users:menu:", "a:users:search:", "a:users:list:0", "a:users:blocked:0",
        "a:users:card:5", "a:users:block:5", "a:users:unblock:5", "a:users:gift:5",
        "a:users:gift2:5:3", "a:users:extend:5", "a:users:exception:5",
        "a:users:unlink:5", "a:users:addtogroup:5", "a:users:addgroup2:5:3",
        "a:subs:menu:", "a:subs:plans:", "a:subs:plan:5", "a:subs:price:5",
        "a:subs:publish:5", "a:subs:pub2:5:future", "a:subs:versions:5",
        "a:subs:archive:5", "a:subs:archive2:5", "a:subs:newplan:",
        "a:subs:salesmode:", "a:subs:mode:free", "a:subs:mode:paid",
        "a:subs:sched:", "a:subs:paymethods:", "a:subs:paytoggle:2",
        "a:stats:menu:", "a:stats:dash:", "a:stats:ledger:", "a:stats:users:",
        "a:downloads:menu:", "a:downloads:queue:", "a:downloads:prio:7:2",
        "a:downloads:track:", "a:downloads:failed:", "a:downloads:retry:7",
        "a:downloads:capacity:", "a:downloads:capedit:", "a:downloads:refund:7",
        "a:codes:menu:", "a:codes:new:", "a:codes:plan:3", "a:codes:list:0",
        "a:codes:cancel:IG-TEST1",
        "a:groups:menu:", "a:groups:list:", "a:groups:card:3", "a:groups:new:",
        "a:broadcast:menu:", "a:broadcast:new:", "a:broadcast:aud:all",
        "a:broadcast:audgroup:3", "a:broadcast:confirm:9", "a:broadcast:jobs:",
        "a:broadcast:stop:9",
        "a:settings:menu:", "a:settings:texts:", "a:settings:edittext:text.welcome",
        "a:settings:maint:", "a:settings:maint:on", "a:settings:maint:off",
        "a:settings:maintopt:block:1", "a:settings:maintopt:cont:1",
        "a:settings:features:", "a:settings:feattoggle:feature.trial:1",
        "a:settings:notify:", "a:settings:notifytoggle:notify.on_queue:1",
        "a:settings:wait:", "a:settings:waitset:send_success", "a:settings:free:",
        "a:errors:menu:", "a:errors:list:all", "a:errors:list:download",
        "a:errors:list:send", "a:errors:list:instagram", "a:errors:list:internal",
        "a:admins:menu:", "a:admins:list:", "a:admins:remove:5", "a:admins:add:",
        "a:admins:roles:",
        "a:health:menu:",
        "a:orders:menu:", "a:orders:pending:0", "a:orders:list:all:0",
        "a:orders:list:confirmed:0", "a:orders:list:rejected:0", "a:orders:stats:",
        "a:orders:card:ORD1", "a:orders:receipt:ORD1", "a:orders:approve:ORD1:0",
        "a:orders:approve:ORD1:1", "a:orders:reject:ORD1", "a:orders:needsreview:ORD1",
    ]
    handlers = _get_handlers()

    async def check_all():
        broken = []
        for b in buttons:
            ev = _make_event(b)
            found = False
            for h in handlers:
                try:
                    res = await h.check(ev)
                    if res and res[0] is True:
                        found = True
                        break
                except Exception:
                    continue
            if not found:
                broken.append(b)
        return broken

    broken = asyncio.run(check_all())
    assert broken == [], f"دکمه‌های بدون هندلر: {broken}"


def test_user_side_buttons_are_routed():
    for data in ("buy:1", "trial:1", "paydone:ORD1", "paycancel:ORD1"):
        assert asyncio.run(_routed(data)) is True, data


# ---------------- پارسر پلن جدید ----------------
def test_parse_plan_fields_kv_multiline_exact_panel_example():
    """نمونه دقیق متن پنل باید بدون خطا پارس شود."""
    text = (
        "name: gold\n"
        "عنوان: طلایی\n"
        "قیمت: 299000\n"
        "مدت_روز: 30\n"
        "هم‌زمان: 2\n"
        "انتظار_ثانیه: 10\n"
        "ظرفیت_صف: 15"
    )
    assert parse_plan_fields(text) == ["gold", "طلایی", "299000", "30", "2", "10", "15"]


def test_parse_plan_fields_pipe_single_line():
    """شکل قدیمی تک‌خطی همچنان پذیرفته می‌شود."""
    assert parse_plan_fields("gold | طلایی | 299000 | 30 | 2 | 10 | 15") == [
        "gold", "طلایی", "299000", "30", "2", "10", "15",
    ]


def test_parse_plan_fields_english_keys_and_equals():
    assert parse_plan_fields(
        "name=silver\nprice=199000\ntitle=نقره‌ای\ndays=15\n"
        "concurrent=1\nwait=5\nqueue=10"
    ) == ["silver", "نقره‌ای", "199000", "15", "1", "5", "10"]


def test_parse_plan_fields_persian_digits():
    assert parse_plan_fields(
        "name: gold\nعنوان: طلایی\nقیمت: ۲۹۹۰۰۰\nمدت: ۳۰\n"
        "همزمان: ۲\nانتظار: ۱۰\nصف: ۱۵"
    ) == ["gold", "طلایی", "299000", "30", "2", "10", "15"]


def test_parse_plan_fields_rejects_incomplete():
    assert parse_plan_fields("name: gold\nعنوان: طلایی") is None
    assert parse_plan_fields("gold | طلایی | 299000") is None
    assert parse_plan_fields("") is None
    assert parse_plan_fields("سلام") is None


# ---------------- parse_kv ----------------
def test_parse_kv_accepts_colon_separator():
    assert parse_kv("days: 30, count: 5") == {"days": 30, "count": 5}


def test_parse_kv_accepts_equals_separator():
    assert parse_kv("days=30, count=5") == {"days": 30, "count": 5}


def test_parse_kv_accepts_multiline():
    assert parse_kv("days=30\ncount=5") == {"days": 30, "count": 5}


def test_parse_kv_persian_digits():
    assert parse_kv("days=۳۰, count=۵") == {"days": 30, "count": 5}


def test_parse_kv_ignores_garbage():
    assert parse_kv("سلام") == {}
    assert parse_kv("") == {}
