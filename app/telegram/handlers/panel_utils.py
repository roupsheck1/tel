"""ابزارهای مشترک پنل مدیریت — کنترل مجوز سمت سرور (بخش ۱۶.۳ و ۱۹.۴ سند)."""
from __future__ import annotations

import functools
import time

from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from ...admin.permissions import AccessDenied
from ...services import Services

PERSIAN_DIGITS = "۰۱۲۳۴۵۶۷۸۹"


class AdminInput(StatesGroup):
    """حالت FSM ورودی مدیر."""
    awaiting = State()


def fa_num(value) -> str:
    """تبدیل عدد به ارقام فارسی برای نمایش."""
    return "".join(PERSIAN_DIGITS[int(ch)] if ch.isdigit() else ch for ch in str(value))


def ts_fa(ts: int | None) -> str:
    """نمایش زمان یونیکس به تاریخ خوانا."""
    if not ts:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M", time.gmtime(ts))


def mention(user_id: int, username: str | None) -> str:
    if username:
        return f"@{username}"
    return f"<code>{user_id}</code>"


async def edit_or_answer(cb: CallbackQuery, text: str, reply_markup=None) -> None:
    """ویرایش پیام پنل با fallback به ارسال جدید."""
    try:
        await cb.message.edit_text(text, reply_markup=reply_markup)
    except Exception:
        await cb.message.answer(text, reply_markup=reply_markup)


def require(permission: str):
    """دکوراتور بررسی مجوز برای هر عملیات پنل (سمت سرور).

    سازگار با هر دو ترتیب امضای هندلر:
    - (cb, services, ctx=None)
    - (cb, state, services, ctx=None)   ← هندلرهای دارای ورودی FSM
    aiogram سرویس‌ها و state را به‌صورت کلیدواژه تزریق می‌کند؛ این
    دکوراتور پارامترها را موقعیتی می‌سازد تا fn با ترتیب امضایش درست
    فراخوانی شود (پیش‌تر ترتیب state-first خطای «multiple values» می‌داد
    و همه هندلرهای FSM ادمین را در runtime می‌شکست).
    """

    def decorator(fn):
        import inspect

        sig = inspect.signature(fn)
        param_names = list(sig.parameters)
        # آیا fn پارامتر state دارد؟ (cb, state, services, ctx)
        has_state = len(param_names) >= 3 and param_names[1] == "state"

        @functools.wraps(fn)
        async def wrapped(event, services: Services, *args, **kwargs):
            user_id = event.from_user.id
            try:
                ctx = services.permissions.check(user_id, permission)
            except AccessDenied as e:
                if isinstance(event, CallbackQuery):
                    await event.answer(str(e), show_alert=True)
                elif isinstance(event, Message):
                    await event.reply(str(e))
                return None
            state = kwargs.pop("state", None)
            if has_state:
                if state is None and args:
                    state = args[0]
                    args = args[1:]
                return await fn(event, state, services, *args, ctx=ctx, **kwargs)
            return await fn(event, services, *args, ctx=ctx, **kwargs)

        return wrapped

    return decorator


async def ask_input(state: FSMContext, cb_or_msg, prompt: str, flow: str, arg: str = "") -> None:
    """درخواست ورودی متنی از مدیر با ذخیره جریان در FSM."""
    from ..keyboards.admin import kb
    await state.set_state(AdminInput.awaiting)
    await state.update_data(flow=flow, arg=arg)
    markup = kb([[("❌ انصراف", "a:cancel:")]])
    if isinstance(cb_or_msg, CallbackQuery):
        try:
            await cb_or_msg.message.edit_text(prompt, reply_markup=markup)
        except Exception:
            await cb_or_msg.message.answer(prompt, reply_markup=markup)
    else:
        await cb_or_msg.reply(prompt, reply_markup=markup)


def parse_kv(text: str) -> dict:
    """پارس ورودی به شکل key=value با کاما."""
    out = {}
    for part in text.replace("،", ",").split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            k = k.strip().lower()
            v = v.strip()
            if v.lstrip("-").isdigit():
                v = int(v)
            out[k] = v
    return out
