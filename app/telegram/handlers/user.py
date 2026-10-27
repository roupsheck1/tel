"""هندلرهای کاربر — جریان اصلی محصول از سمت تلگرام (بخش ۲ سند).

نکته معماری (بخش ۲.۱): دریافت محتوا فقط از طریق دایرکت اینستاگرام است؛
ارسال لینک در تلگرام جزو جریان اصلی نیست و کاربر راهنمایی می‌شود.
"""
from __future__ import annotations

import logging
import re
import time

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from ...services import Services
from ...utils import normalize_digits

log = logging.getLogger("handlers.user")
router = Router()
# روتر پس‌زمینه (fallback): این router باید «بعد از» روترهای ادمین ثبت شود تا
# هندلر همه‌گیر free_text، دستورات (مثل /panel) و ورودی‌های متنی ادمین را
# نمی‌بلعد. در main.py ترتیب ثبت را ببینید.
fallback = Router(name="fallback")
CODE_RE = re.compile(r"^[0-9]{6}$")
PROMO_RE = re.compile(r"^(?:/code\s+|کد\s+)?([A-Za-z0-9-]{4,20})$")


class ReceiptStates(StatesGroup):
    awaiting_receipt = State()


# ============================ /start و ثبت‌نام ============================
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, services: Services):
    await state.clear()
    is_new = services.user_service.register(
        message.from_user.id, message.from_user.username, message.from_user.full_name
    )
    ig_user = services.settings.get("bot.ig_username") or services.links_repo and "ربات"
    text = services.settings.get("text.welcome").format(
        name=message.from_user.first_name or "دوست", ig_user=ig_user,
    )
    from ..keyboards.user import MAIN_KEYBOARD
    await message.reply(text + ("\n\n👤 حساب شما ثبت شد ✅" if is_new else ""),
                        reply_markup=MAIN_KEYBOARD)


@router.message(Command("help"))
async def cmd_help(message: Message, services: Services):
    await message.reply(services.settings.get("text.help"))


@router.message(Command("connect"))
async def cmd_connect(message: Message, services: Services):
    link = services.links_repo.get_active(message.from_user.id)
    if link:
        await message.reply(
            f"✅ حسابت از قبل به @{link['ig_username']} وصل است.\n"
            "برای قطع اتصال /unlink را بفرست."
        )
        return
    await message.reply(
        "🔹 مراحل اتصال:\n"
        "۱. در اینستاگرام به پیج ربات یک پیام بفرست (مثلاً «سلام»).\n"
        "۲. یک کد ۶ رقمی در دایرکت می‌گیری.\n"
        "۳. همان کد را همین‌جا بفرست تا حسابت وصل شود."
    )


@router.message(Command("unlink"))
async def cmd_unlink(message: Message, services: Services):
    ig = services.links_repo.unlink_by_admin(message.from_user.id)
    if ig:
        await message.reply(f"اتصال به @{ig} قطع شد. برای اتصال مجدد /connect را بفرست.")
    else:
        await message.reply("حسابی وصل نیست.")


# ============================ وضعیت ============================
@router.message(Command("status"))
@router.message(F.text == "📊 وضعیت من")
async def cmd_status(message: Message, services: Services):
    summary = services.user_service.status_summary(message.from_user.id)
    lines = ["📊 وضعیت شما:", ""]
    link = summary["link"]
    lines.append("🔗 اینستاگرام: " + (f"متصل به @{link['ig_username']}" if link else "متصل نیست"))
    mode = summary["sales_mode"]
    if mode == "free":
        lines.append("💎 اشتراک: سرویس در حالت رایگان است 🎁")
    else:
        sub = summary["subscription"]
        if sub:
            days = max(0, (sub["expires_at"] - int(time.time())) // 86400)
            plan = services.plans_repo.get(sub["plan_id"])
            lines.append(f"💎 اشتراک فعال: {plan['title'] if plan else '-'} ({days} روز مانده)")
        else:
            lines.append("💎 اشتراک: فعال نیست — با /plans پلن‌ها را ببین.")
    # مصرف امروز (در صورت وجود موتور)
    if services.engine:
        used = services.downloads_repo.quota_used(message.from_user.id)
        lines.append(f"📥 دانلود موفق امروز: {used['daily']} | این ساعت: {used['hourly']}")
    await message.reply("\n".join(lines))


# ============================ پلن‌ها و خرید ============================
@router.message(Command("plans"))
@router.message(F.text == "💎 پلن‌ها و خرید")
async def cmd_plans(message: Message, services: Services):
    mode = services.settings.sales_mode
    if mode == "free":
        await message.reply("🎁 سرویس فعلاً رایگان است! بدون نیاز به خرید از سرویس استفاده کن.")
        return
    plans = services.plan_service.saleable_plans()
    if not plans:
        await message.reply("فعلاً پلنی برای فروش فعال نیست.")
        return
    from ..keyboards.user import plans_keyboard, trial_keyboard
    rows = []
    from aiogram.types import InlineKeyboardMarkup
    for p in plans:
        cfg = p["config"]
        title = f"{p['title']} — {cfg.price:,} تومان ({cfg.duration_label})"
        if cfg.is_trial:
            rows.append([trial_keyboard(p["id"]).inline_keyboard[0][0]])
        else:
            from aiogram.types import InlineKeyboardButton
            rows.append([InlineKeyboardButton(text=title, callback_data=f"buy:{p['id']}")])
    await message.reply("💎 پلن‌های فعال:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("buy:"))
async def cb_buy(callback: CallbackQuery, services: Services):
    plan_id = int(callback.data.split(":")[1])
    ok, result = services.order_service.create_order(callback.from_user.id, plan_id)
    if not ok:
        await callback.answer(result, show_alert=True)
        return
    order = services.orders_repo.get(result)
    text = services.order_service.payment_text(order)
    from ..keyboards.user import order_keyboard
    await callback.message.answer(text, reply_markup=order_keyboard(order["id"]))
    await callback.answer("سفارش ایجاد شد")


@router.callback_query(F.data.startswith("trial:"))
async def cb_trial(callback: CallbackQuery, services: Services):
    plan_id = int(callback.data.split(":")[1])
    ok, msg = services.sub_service.grant_trial(callback.from_user.id, plan_id, "user")
    await callback.answer(msg, show_alert=not ok)
    if ok:
        await callback.message.answer("🎁 " + msg)


@router.callback_query(F.data.startswith("paydone:"))
async def cb_paydone(callback: CallbackQuery, state: FSMContext, services: Services):
    order_id = callback.data.split(":")[1]
    ok, msg = services.order_service.announce_payment(order_id, callback.from_user.id)
    if ok:
        await state.set_state(ReceiptStates.awaiting_receipt)
        await state.update_data(order_id=order_id)
        await callback.message.answer("لطفاً 📷 رسید پرداخت را همین‌جا بفرست.")
    await callback.answer(msg)


@router.callback_query(F.data.startswith("paycancel:"))
async def cb_paycancel(callback: CallbackQuery, state: FSMContext, services: Services):
    order_id = callback.data.split(":")[1]
    ok, msg = services.order_service.cancel(order_id, callback.from_user.id)
    if ok:
        await state.clear()
    await callback.answer(msg, show_alert=not ok)


@router.message(ReceiptStates.awaiting_receipt, F.photo)
async def receipt_photo(message: Message, state: FSMContext, services: Services):
    data = await state.get_data()
    order_id = data.get("order_id")
    file_id = message.photo[-1].file_id
    ok, msg = services.order_service.attach_receipt(order_id, message.from_user.id, file_id)
    await message.reply(msg)
    if ok:
        await state.clear()


# ============================ سفارش‌های من ============================
@router.message(Command("myorders"))
@router.message(F.text == "🧾 سفارش‌های من")
async def cmd_myorders(message: Message, services: Services):
    orders = services.orders_repo.list(user_id=message.from_user.id, limit=10)
    if not orders:
        await message.reply("سفارشی ثبت نشده است.")
        return
    LABELS = {
        "awaiting_payment": "منتظر پرداخت", "payment_announced": "پرداخت اعلام‌شده",
        "receipt_received": "رسید دریافت‌شده", "under_review": "در حال بررسی",
        "confirmed": "تأییدشده ✅", "rejected": "ردشده ❌", "expired": "منقضی",
        "canceled": "لغوشده", "needs_review": "نیازمند بررسی بیشتر",
    }
    lines = ["🧾 آخرین سفارش‌های شما:", ""]
    for o in orders:
        lines.append(
            f"• {o['plan_title']} — {o['amount']:,} تومان — {LABELS.get(o['status'], o['status'])}"
        )
    await message.reply("\n".join(lines))


# ============================ کدها ============================
@router.message(Command("code"))
async def cmd_code(message: Message, services: Services):
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.reply("شکل صحیح: /code IG-XXXX")
        return
    # ارقام فارسی/عربی هم پذیرفته می‌شود (تجربه کاربر فارسی‌زبان)
    ok, msg = services.sub_service.redeem_code(normalize_digits(parts[1]).strip(),
                                                message.from_user.id)
    await message.reply(msg)


# ============================ پیام آزاد کاربر ============================
# نکته مهم: این هندلر روی روتر «fallback» است نه روتر اصلی؛ در main.py بعد از
# روترهای ادمین ثبت می‌شود تا دستورات ادمین (/panel و ...) و ورودی‌های متنی
# حالت‌های FSM ادمین (پیام همگانی، جستجو و ...) به هندلر درست برسند.
@fallback.message(F.text)
async def free_text(message: Message, state: FSMContext, services: Services):
    # نرمال‌سازی ارقام فارسی/عربی به انگلیسی (کد اتصال با ارقام فارسی هم کار کند)
    text = normalize_digits((message.text or "").strip())

    # ۱) کد ۶ رقمی اتصال (بخش ۵)
    if CODE_RE.fullmatch(text):
        ok, result = services.links_repo.link(text, message.from_user.id)
        if ok:
            await message.reply(
                f"✅ حسابت به @{result} وصل شد.\n"
                "حالا پست یا ریلز را در دایرکت اینستاگرام برای پیج ربات بفرست."
            )
        else:
            await message.reply(
                f"❌ {result}\n\n"
                "راهنما: فقط «آخرین کد» که در دایرکت اینستاگرام گرفته‌ای معتبر است "
                "(۲۰ دقیقه اعتبار دارد). اگر کدت منقضی شده، در اینستاگرام دوباره به "
                "پیج ربات پیام بفرست تا کد تازه بگیری و همین‌جا بفرست."
            )
        return

    # ۲) لینک اینستاگرام در تلگرام — جزو جریان اصلی نیست (بخش ۲.۱)
    if "instagram.com" in text:
        await message.reply(
            "📥 دریافت محتوا از طریق دایرکت اینستاگرام انجام می‌شود.\n"
            "پست یا ریلز را در اینستاگرام برای پیج ربات بفرست (Share) تا همین‌جا تحویل بگیری."
        )
        return

    # ۳) کد اشتراک به شکل «کد IG-XXX»
    m = PROMO_RE.match(text)
    if m and m.group(1).upper().startswith("IG-"):
        ok, msg = services.sub_service.redeem_code(m.group(1), message.from_user.id)
        await message.reply(msg)
        return

    # ۴) دکمه راهنما
    if text == "🆘 راهنما":
        await message.reply(services.settings.get("text.help"))
        return
    if text == "🔗 اتصال اینستاگرام":
        await cmd_connect(message, services)
        return

    await message.reply("دستور را متوجه نشدم. /help را ببین.")
