"""آزمون «برگشت به منوی قبل» — ساختار کیبورد، دکمه‌های 🔙 و مکانیزم a:backgo.

پوشش:
1) nav_rows / with_back / menu_of — دکمه «🔙 برگشت» با هدف درست در همه صفحات
2) پرامپت‌های ورودی — دکمه برگشت با callback منوی قبلی
3) cb_backgo — پاک‌سازی FSM + رندر منوی قبلی (با آرگومان و بدون آرگومان)
4) پیام خطای فارسی برای ورودی زمان نامعتبر (subs:sched)
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from aiogram.types import CallbackQuery, Chat, Message, User
from datetime import datetime

from app.database import Database
from app.services import build_services
from app.telegram.handlers import admin as ah
from app.telegram.handlers import admin_more as am
from app.telegram.keyboards import admin as ak

OWNER = 111
USER1 = 1001


# ---------------- فیک‌ها ----------------
class FakeState:
    def __init__(self, flow="", arg=""):
        self.data = {"flow": flow, "arg": arg} if flow else {}
        self.cleared = False
        self.set_calls = []

    async def set_state(self, s):
        self.set_calls.append(s)

    async def update_data(self, **kw):
        self.data.update(kw)

    async def get_data(self):
        return dict(self.data)

    async def clear(self):
        self.cleared = True
        self.data = {}


class FakeCbMessage:
    def __init__(self):
        self.edits = []
        self.answers = []

    async def edit_text(self, text, reply_markup=None, **_):
        self.edits.append((text, reply_markup))
        return True

    async def answer(self, text=None, reply_markup=None, **_):
        self.answers.append((text, reply_markup))
        return True


class FakeCb:
    def __init__(self, data: str, user_id: int = OWNER):
        self.data = data
        self.from_user = SimpleNamespace(id=user_id)
        self.message = FakeCbMessage()
        self.answers = []

    async def answer(self, text=None, show_alert=False, **_):
        self.answers.append(text)
        return True

    async def reply(self, text, reply_markup=None, **_):
        # مسیر پیام (نه callback) — برای ask_input وقتی فیک، CallbackQuery واقعی نیست
        self.message.edits.append((text, reply_markup))
        return True


class FakeMsg:
    def __init__(self, text: str, user_id: int = OWNER):
        self.text = text
        self.from_user = SimpleNamespace(id=user_id)
        self.replies = []
        self.bot = SimpleNamespace(send_message=self._send)

    async def _send(self, *_a, **_k):
        return True

    async def reply(self, text, reply_markup=None, **_):
        self.replies.append((text, reply_markup))
        return True


def buttons(markup):
    return [(b.text, b.callback_data) for row in markup.inline_keyboard for b in row]


@pytest.fixture()
def services(tmp_path):
    db = Database(tmp_path / "back.db")
    svc = build_services(db, [OWNER])
    svc.users_repo.register(USER1, "u1001", "User One")
    return svc


# ---------------- ۱) ساختار کیبورد ----------------
def test_nav_rows_structure():
    rows = ak.nav_rows("a:x:y:")
    assert rows[0] == [("🔙 برگشت", "a:x:y:")]
    assert rows[1] == [("🏠 منوی اصلی", "a:home:")]
    # پیش‌فرض بدون والد → ریشه پنل
    assert ak.nav_rows()[0][0][1] == "a:home:"


def test_menu_of_every_section_has_back():
    sections = ["users", "subs", "stats", "downloads", "orders", "codes",
                "groups", "broadcast", "settings", "errors", "admins", "health"]
    for sec in sections:
        btns = buttons(ak.menu_of(sec))
        texts = [t for t, _ in btns]
        assert "🔙 برگشت" in texts, f"بخش {sec} دکمه برگشت ندارد"
        assert "🏠 منوی اصلی" in texts, f"بخش {sec} دکمه منوی اصلی ندارد"
        back_target = dict(btns)["🔙 برگشت"]
        assert back_target == "a:home:", f"برگشت بخش {sec} باید به ریشه برود"


def test_with_back_parent():
    btns = dict(buttons(ak.with_back([], parent="a:stats:menu:")))
    assert btns["🔙 برگشت"] == "a:stats:menu:"
    assert btns["🏠 منوی اصلی"] == "a:home:"


# ---------------- ۲) پرامپت‌ها ----------------
@pytest.mark.asyncio
async def test_prompt_has_back_button(services):
    cb = FakeCb(f"a:users:extend:{USER1}")
    st = FakeState()
    await ah.users_extend(cb, state=st, services=services)
    markup = cb.message.edits[0][1]
    btns = dict(buttons(markup))
    assert btns.get("❌ انصراف") == "a:cancel:"
    assert btns.get("🔙 برگشت") == f"a:backgo:a:users:card:{USER1}", \
        "پرامپت تمدید باید دکمه برگشت به کارت همان کاربر داشته باشد"


# ---------------- ۳) cb_backgo ----------------
@pytest.mark.asyncio
async def test_backgo_clears_fsm_and_renders_card(services):
    cb = FakeCb(f"a:backgo:a:users:card:{USER1}")
    st = FakeState(flow="users:extend", arg=str(USER1))
    await ah.cb_backgo(cb, st, services)
    assert st.cleared, "برگشت از پرامپت باید FSM را پاک کند"
    assert cb.message.edits, "کارت باید رندر شود"
    text = cb.message.edits[0][0]
    assert "کاربر" in text
    btns = dict(buttons(cb.message.edits[0][1]))
    assert btns["🔙 برگشت"] == "a:users:list:0", "برگشتِ کارت → فهرست کاربران"


@pytest.mark.asyncio
async def test_backgo_menu(services):
    cb = FakeCb("a:backgo:a:users:menu:")
    await ah.cb_backgo(cb, FakeState(), services)
    assert "مدیریت کاربران" in cb.message.edits[0][0]


@pytest.mark.asyncio
async def test_backgo_plan_card(services):
    plan = services.plans_repo.list()[0]
    cb = FakeCb(f"a:backgo:a:subs:plan:{plan['id']}")
    await ah.cb_backgo(cb, FakeState(), services)
    assert "نام داخلی" in cb.message.edits[0][0], "کارت پلن باید رندر شود"
    btns = dict(buttons(cb.message.edits[0][1]))
    assert btns["🔙 برگشت"] == "a:subs:plans:"


@pytest.mark.asyncio
async def test_backgo_unknown_falls_to_root(services):
    cb = FakeCb("a:backgo:a:nope:1")
    await ah.cb_backgo(cb, FakeState(), services)
    assert "پنل مدیریت" in cb.message.edits[0][0]
    assert "بخش موردنظر" in cb.message.edits[0][0]


@pytest.mark.asyncio
async def test_backgo_codes_new_chooser(services):
    cb = FakeCb("a:backgo:a:codes:new:")
    await ah.cb_backgo(cb, FakeState(), services)
    assert "پلن کد را انتخاب کن" in cb.message.edits[0][0]


@pytest.mark.asyncio
async def test_backgo_orders_card(services):
    method = services.payments_repo.get_active()
    plan_row = services.plan_service.saleable_plans()[0]
    # ۱) سفارش در وضعیت بررسی (رسید/اعلام) → برگشت به «در انتظار بررسی»
    oid1 = services.orders_repo.create(USER1, plan_row, 99000, 30 * 86400, method)
    services.orders_repo.announce_payment(oid1)
    cb = FakeCb(f"a:backgo:a:orders:card:{oid1}")
    await ah.cb_backgo(cb, FakeState(), services)
    assert f"سفارش <code>{oid1}</code>" in cb.message.edits[0][0]
    btns = dict(buttons(cb.message.edits[0][1]))
    assert btns["🔙 برگشت"] == "a:orders:pending:0", "سفارش در بررسی → برگشت به در انتظار بررسی"
    # ۲) سفارش تازه (منتظر پرداخت) → برگشت به «همه سفارش‌ها»
    oid2 = services.orders_repo.create(USER1, plan_row, 99000, 30 * 86400, method)
    cb2 = FakeCb(f"a:backgo:a:orders:card:{oid2}")
    await ah.cb_backgo(cb2, FakeState(), services)
    btns2 = dict(buttons(cb2.message.edits[0][1]))
    assert btns2["🔙 برگشت"] == "a:orders:list:all:0", "سفارش عادی → برگشت به همه سفارش‌ها"


# ---------------- ۴) پیام خطای فارسی ----------------
@pytest.mark.asyncio
async def test_sched_bad_input_friendly_error(services):
    msg = FakeMsg("abc")
    st = FakeState(flow="subs:sched", arg="")
    await am.admin_input(msg, st, services)
    assert msg.replies, "باید پاسخ بیاید"
    text = msg.replies[0][0]
    assert "قالب زمان درست نیست" in text, f"پیام باید فارسی و راهنما باشد: {text[:80]}"
    assert "2026-01-15 18:30" in text, "نمونه قالب باید در پیام باشد"
