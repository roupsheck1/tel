"""صفحه‌کلیدهای کاربر (فارسی و راست‌چین — بخش ۱۶.۱ سند)."""
from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
from aiogram.types import KeyboardButton

# دکمه ورود به پنل که فقط برای مدیران به منوی اصلی اضافه می‌شود (§۲۶:
# دسترسی پنل بر اساس شناسه عددی مدیران؛ دکمه صرفاً نقطه ورود است و
# مجوز همچنان سمت سرور بررسی می‌شود)
ADMIN_PANEL_BUTTON = "🛛 پنل مدیریت"

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📊 وضعیت من"), KeyboardButton(text="💎 پلن‌ها و خرید")],
        [KeyboardButton(text="🔗 اتصال اینستاگرام"), KeyboardButton(text="🧾 سفارش‌های من")],
        [KeyboardButton(text="🆘 راهنما")],
    ],
    resize_keyboard=True,
    # is_persistent=False: منو «همیشه باز» نمی‌ماند؛ دکمه برگشت گوشی آن را
    # جمع می‌کند تا کاربر پیام‌های ربات را کامل ببیند (درخواست کاربر).
    is_persistent=False,
)


def main_keyboard(is_admin: bool = False) -> ReplyKeyboardMarkup:
    """صفحه‌کلید اصلی؛ برای مدیران یک ردیف «پنل مدیریت» اضافه می‌شود.

    (کاربر عادی همان منوی همیشگی را می‌بیند؛ مدیر بدون /panel هم راه
    ورود به پنل را داخل خود منو می‌بیند.)
    """
    rows = [
        [KeyboardButton(text="📊 وضعیت من"), KeyboardButton(text="💎 پلن‌ها و خرید")],
        [KeyboardButton(text="🔗 اتصال اینستاگرام"), KeyboardButton(text="🧾 سفارش‌های من")],
        [KeyboardButton(text="🆘 راهنما")],
    ]
    if is_admin:
        rows.append([KeyboardButton(text=ADMIN_PANEL_BUTTON)])
    # is_persistent=False → منو با دکمهٔ برگشت گوشی جمع می‌شود (نه فقط با chevron)
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, is_persistent=False)


def plans_keyboard(plans: list[dict]) -> InlineKeyboardMarkup:
    rows = []
    for p in plans:
        rows.append([InlineKeyboardButton(
            text=f"{p['title']} — {p['config'].price:,} تومان",
            callback_data=f"buy:{p['id']}",
        )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def order_keyboard(order_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ پرداخت کردم", callback_data=f"paydone:{order_id}"),
        InlineKeyboardButton(text="❌ لغو سفارش", callback_data=f"paycancel:{order_id}"),
    ]])


def trial_keyboard(plan_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🎁 دریافت دوره آزمایشی", callback_data=f"trial:{plan_id}"),
    ]])
