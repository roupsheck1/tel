"""آزمون دسترسی ادمین به پنل — دکمه پنل در منوی اصلی + پیام راهنمای شناسه.

پس از گزارش کاربر («من ادمین هستم ولی فقط منوی کاربر عادی را می‌بینم»):
- مدیر باید بدون دانستن /panel هم راه ورود به پنل را در منوی اصلی ببیند؛
- اگر ADMIN_IDS سرویس با شناسه واقعی مدیر فرق داشته باشد، پیام رد شدن
  باید شناسه عددی واقعی کاربر را نشان دهد تا تنظیم ADMIN_IDS ممکن شود.
"""
from __future__ import annotations

from app.telegram.handlers import admin as ah
from app.telegram.keyboards.user import ADMIN_PANEL_BUTTON, MAIN_KEYBOARD, main_keyboard


class FakeUser:
    def __init__(self, user_id: int, username: str | None = None):
        self.id = user_id
        self.username = username


class FakeMessage:
    def __init__(self, user_id: int, username: str | None = None):
        self.from_user = FakeUser(user_id, username)
        self.replies: list[tuple[str, object]] = []

    async def reply(self, text: str, reply_markup=None, **_):
        self.replies.append((text, reply_markup))


def _texts(kb) -> list[str]:
    return [b.text for row in kb.keyboard for b in row]


# ---------- صفحه‌کلید ----------

def test_main_keyboard_has_admin_row_only_for_admins():
    admin_texts = _texts(main_keyboard(is_admin=True))
    plain_texts = _texts(main_keyboard(is_admin=False))
    assert ADMIN_PANEL_BUTTON in admin_texts
    assert ADMIN_PANEL_BUTTON not in plain_texts
    # منوی همیشگی کاربر بدون تغییر مانده است
    assert ADMIN_PANEL_BUTTON not in _texts(MAIN_KEYBOARD)
    assert _texts(MAIN_KEYBOARD) == plain_texts


def test_panel_button_handler_on_admin_router_not_fallback():
    from app.telegram.handlers import user as uh

    admin_names = {h.callback.__name__ for h in ah.router.message.handlers}
    assert "msg_open_panel" in admin_names
    assert "cmd_panel" in admin_names
    # دکمه پنل نباید روی روترهای دیگر (کاربر/fallback) هم باشد تا ترتیب مهم نباشد
    fallback_names = {h.callback.__name__ for h in uh.fallback.message.handlers}
    user_names = {h.callback.__name__ for h in uh.router.message.handlers}
    assert "msg_open_panel" not in fallback_names
    assert "msg_open_panel" not in user_names


# ---------- ورود به پنل ----------

async def test_panel_opens_for_owner(services):  # noqa: ARG001
    msg = FakeMessage(111)  # OWNER در conftest مدیر اصلی است
    await ah.cmd_panel(msg, services)
    assert len(msg.replies) == 1
    text, markup = msg.replies[0]
    assert "پنل مدیریت" in text
    assert "مدیر اصلی" in text
    assert markup is not None


async def test_panel_denial_shows_numeric_id(services):  # noqa: ARG001
    msg = FakeMessage(99999, username="somebody")
    await ah.cmd_panel(msg, services)
    text, _ = msg.replies[0]
    # شناسه عددی واقعی کاربر باید در پیام باشد تا ADMIN_IDS قابل تنظیم شود
    assert "99999" in text
    assert "ADMIN_IDS" in text


async def test_panel_button_opens_panel_for_admin(services):  # noqa: ARG001
    msg = FakeMessage(111)
    await ah.msg_open_panel(msg, services)
    text, markup = msg.replies[0]
    assert "پنل مدیریت" in text
    assert markup is not None


async def test_panel_button_denied_for_normal_user(services):  # noqa: ARG001
    msg = FakeMessage(1001)
    await ah.msg_open_panel(msg, services)
    text, _ = msg.replies[0]
    assert "1001" in text
    assert "دسترسی ندارید" in text
