"""سازنده صفحه‌کلیدهای پنل مدیریت (Inline Keyboard — بخش ۱۶.۱ و ۱۶.۲ سند)."""
from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def kb(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    """ساخت سریع کیبورد از [(متن، callback_data)]."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t, callback_data=c) for t, c in row] for row in rows
    ])


# ---------- منوی اصلی (بخش ۱۶.۲ — ۱۲ بخش طبق سند ۲) ----------
MAIN_MENU = kb([
    [("👥 کاربران", "a:users:menu:")],
    [("💎 مدیریت اشتراک‌ها", "a:subs:menu:")],
    [("🛍 پلن‌ها و قیمت‌گذاری", "a:subs:plans:")],
    [("🧾 سفارش‌ها و پرداخت‌ها", "a:orders:menu:")],
    [("📊 آمار و گزارش‌ها", "a:stats:menu:")],
    [("📥 مدیریت دانلودها", "a:downloads:menu:")],
    [("🎟 کدهای اشتراک", "a:codes:menu:")],
    [("👥‍👥 گروه‌های کاربری", "a:groups:menu:")],
    [("📢 پیام همگانی", "a:broadcast:menu:")],
    [("🛠 تنظیمات ربات", "a:settings:menu:")],
    [("📋 گزارش خطاها", "a:errors:menu:")],
    [("🔐 مدیریت مدیران", "a:admins:menu:")],
    [("🩺 مرکز سلامت", "a:health:menu:")],
])

BACK_HOME_ROW = [("🏠 منوی اصلی", "a:home:")]
BACK_HOME = kb([BACK_HOME_ROW])


def nav_rows(parent: str = "a:home:") -> list[list[tuple[str, str]]]:
    """ردیف‌های پایین هر صفحهٔ پنل:
    1️⃣ «🔙 برگشت» → منوی قبلی (parent)
    2️⃣ «🏠 منوی اصلی» → ریشهٔ پنل (میان‌بر)
    """
    return [[("🔙 برگشت", parent)], list(BACK_HOME_ROW)]


def with_back(rows: list[list[tuple[str, str]]], parent: str = "a:home:") -> InlineKeyboardMarkup:
    """دکمه‌های صفحه + «برگشت به منوی قبلی» (parent) + میان‌بر «منوی اصلی»."""
    return kb([*rows, *nav_rows(parent)])


# ---------- منوهای اختصاصی هر بخش ----------
def menu_of(section: str) -> InlineKeyboardMarkup:
    menus: dict[str, list[list[tuple[str, str]]]] = {
        "users": [
            [("🔍 جست‌وجوی کاربر", "a:users:search:")],
            [("📃 آخرین کاربران", "a:users:list:0")],
            [("🚫 مسدودشده‌ها", "a:users:blocked:0")],
        ],
        "subs": [
            [("📃 فهرست پلن‌ها", "a:subs:plans:")],
            [("➕ پلن جدید", "a:subs:newplan:")],
            [("💰 حالت فروش", "a:subs:salesmode:")],
            [("💳 روش‌های پرداخت", "a:subs:paymethods:")],
        ],
        "stats": [
            [("📈 داشبورد کلی", "a:stats:dash:")],
            [("🧾 دفتر فروش", "a:stats:ledger:")],
            [("👥 آمار کاربران", "a:stats:users:")],
        ],
        "downloads": [
            [("📋 وضعیت صف", "a:downloads:queue:")],
            [("❌ خطاها و تلاش مجدد", "a:downloads:failed:")],
            [("⚙️ ظرفیت سرور", "a:downloads:capacity:")],
        ],
        "orders": [
            [("🔔 در انتظار بررسی", "a:orders:pending:0")],
            [("🧾 همه سفارش‌ها", "a:orders:list:all:0")],
            [("✅ تأییدشده‌ها", "a:orders:list:confirmed:0")],
            [("❌ ردشده‌ها", "a:orders:list:rejected:0")],
            [("📊 خلاصه وضعیت سفارش‌ها", "a:orders:stats:")],
        ],
        "codes": [
            [("➕ ساخت کد", "a:codes:new:")],
            [("📃 فهرست کدها", "a:codes:list:0")],
        ],
        "groups": [
            [("📃 فهرست گروه‌ها", "a:groups:list:")],
            [("➕ گروه دستی جدید", "a:groups:new:")],
        ],
        "broadcast": [
            [("📢 پیام جدید", "a:broadcast:new:")],
            [("📃 گزارش ارسال‌ها", "a:broadcast:jobs:")],
        ],
        "settings": [
            [("📝 ویرایش پیام‌ها", "a:settings:texts:")],
            [("🔧 حالت نگهداری", "a:settings:maint:")],
            [("🔑 کلیدهای قابلیت", "a:settings:features:")],
            [("🔔 اعلان‌ها", "a:settings:notify:")],
            [("⏱ تنظیمات انتظار", "a:settings:wait:")],
            [("📥 پیکربندی حالت رایگان", "a:settings:free:")],
        ],
        "errors": [
            [("🕘 خطاهای اخیر", "a:errors:list:all")],
            [("📥 خطای دانلود", "a:errors:list:download")],
            [("📤 خطای ارسال", "a:errors:list:send")],
            [("📸 خطای اینستاگرام", "a:errors:list:instagram")],
            [("⚙️ خطای داخلی", "a:errors:list:internal")],
        ],
        "admins": [
            [("📃 مدیران فعال", "a:admins:list:")],
            [("➕ افزودن مدیر", "a:admins:add:")],
            [("🎭 نقش‌ها", "a:admins:roles:")],
        ],
        "health": [
            [("🔄 بازخوانی وضعیت", "a:health:menu:")],
        ],
    }
    return with_back(menus.get(section, []))


def confirm_kb(yes_cb: str, no_cb: str = "a:home:") -> InlineKeyboardMarkup:
    return kb([[("✅ تأیید و اجرا", yes_cb), ("❌ انصراف", no_cb)]])


def pager(base_cb: str, offset: int, total: int, page_size: int = 10) -> list[list[tuple[str, str]]]:
    """دکمه‌های صفحه‌بندی."""
    rows = []
    if offset >= page_size:
        rows.append(("⏮ قبلی", f"{base_cb}{max(0, offset - page_size)}"))
    if offset + page_size < total:
        rows.append(("بعدی ⏭", f"{base_cb}{offset + page_size}"))
    return [rows] if rows else []
