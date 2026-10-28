"""پنل مدیریت — بخش ۴: سفارش‌ها و پرداخت‌ها (بخش‌های ۱۳ و ۱۶.۲ سند ۲).

حلقه کامل پرداخت دستی (گام‌های ۸ تا ۱۱ بخش ۱۳ سند):
۸. مدیر سفارش را تأیید یا رد می‌کند.
۹. در صورت تأیید، اشتراک فعال می‌شود (تراکنش اتمیک در OrdersRepo.confirm).
۱۰. کاربر و مدیر نتیجه را دریافت می‌کنند (اطلاع‌رسانی دوطرفه).
۱۱. سوابق تصمیم مدیر در audit_log ثبت می‌شود.

هر عملیات مجوز خود را سمت سرور بررسی می‌کند (بخش ۱۹.۴):
- مشاهده و رسید → PERM_VIEW_ORDERS
- تأیید → PERM_APPROVE_PAYMENT
- رد → PERM_REJECT_PAYMENT
"""
from __future__ import annotations

import json
import logging

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from ...admin.permissions import (
    PERM_APPROVE_PAYMENT, PERM_REJECT_PAYMENT, PERM_VIEW_ORDERS,
)
from ...services import Services
from ..keyboards import admin as ak
from .panel_utils import ask_input, edit_or_answer, mention, require, ts_fa

log = logging.getLogger("handlers.admin_orders")
router = Router(name="admin_orders")

# وضعیت‌های «در جریان بررسی» — سفارش‌هایی که منتظر تصمیم مدیرند (بخش ۱۳.۶)
REVIEW_STATUSES = ("receipt_received", "under_review", "needs_review", "payment_announced")

STATUS_LABELS: dict[str, str] = {
    "awaiting_payment": "منتظر پرداخت",
    "payment_announced": "پرداخت اعلام‌شده",
    "receipt_received": "رسید دریافت‌شده 🧾",
    "under_review": "در حال بررسی",
    "needs_review": "نیازمند بررسی دستی ⚠️",
    "confirmed": "تأییدشده ✅",
    "rejected": "ردشده ❌",
    "expired": "منقضی‌شده",
    "canceled": "لغوشده",
}


def _order_line(o) -> str:
    status = STATUS_LABELS.get(o["status"], o["status"])
    return (f"• <code>{o['id']}</code> — {o['plan_title']} — {o['amount']:,} تومان — "
            f"{mention(o['user_id'], o['username'])} — {status} — {ts_fa(o['created_at'])}")


def order_card(order, services: Services) -> str:
    """کارت کامل سفارش با مشخصات کاربر و پلن (بخش ۱۳.۷: مدیر قبل از تأیید
    مشخصات سفارش و کاربر را مشاهده می‌کند)."""
    try:
        snap = json.loads(order["payment_snapshot"] or "{}")
    except (TypeError, ValueError):
        snap = {}
    user = services.users_repo.get(order["user_id"])
    keys = order.keys()
    username = order["username"] if "username" in keys else None
    days = max(1, order["duration_seconds"] // 86400)
    lines = [
        f"🧾 سفارش <code>{order['id']}</code>",
        "",
        f"👤 کاربر: {mention(order['user_id'], username)}"
        + (f" ({user['display_name']})" if user and user["display_name"] else ""),
        f"📦 پلن: {order['plan_title']} ({days} روز)",
        f"💰 مبلغ: {order['amount']:,} تومان",
        f"💳 روش پرداخت: {snap.get('title') or '-'}"
        + (f" — {snap.get('card_number')}" if snap.get("card_number") else ""),
        f"📌 وضعیت: {STATUS_LABELS.get(order['status'], order['status'])}",
        f"🕰 ایجاد: {ts_fa(order['created_at'])} | انقضا: {ts_fa(order['expires_at'])}",
    ]
    if order["receipt_file_id"]:
        lines.append(f"🧾 رسید: دریافت‌شده ({ts_fa(order['receipt_received_at'])}) — دکمه «مشاهده رسید»")
    else:
        lines.append("🧾 رسید: ارسال‌نشده")
    if order["status"] == "confirmed":
        lines.append(f"✅ تأیید: {ts_fa(order['confirmed_at'])} | اشتراک #{order['subscription_id']}")
    if order["status"] == "rejected":
        lines.append(f"❌ رد: {ts_fa(order['rejected_at'])} | دلیل: {order['rejection_reason'] or '-'}")
    return "\n".join(lines)


def order_actions(order) -> InlineKeyboardMarkup:
    """دکمه‌های متناسب با وضعیت سفارش — عملیات فقط در وضعیت مجاز (۱۳.۶)."""
    rows: list[list[tuple[str, str]]] = []
    if order["status"] in REVIEW_STATUSES:
        rows.append([("✅ تأیید پرداخت", f"a:orders:approve:{order['id']}:0"),
                     ("❌ رد سفارش", f"a:orders:reject:{order['id']}")])
        rows.append([("🔁 علامت‌گذاری «نیازمند بررسی»", f"a:orders:needsreview:{order['id']}")])
    if order["receipt_file_id"]:
        rows.append([("🖼 مشاهده رسید", f"a:orders:receipt:{order['id']}")])
    rows.append([("🏠 منوی اصلی", "a:home:")])
    return ak.kb(rows)


async def notify_admins_new_receipt(bot: Bot, services: Services, order) -> None:
    """اعلان رسید جدید به مدیران دارای مجوز تأیید (بخش ۲۴ سند ۲: «اعلام پرداخت»).

    دکمه‌های سریع تأیید/رد فقط میان‌بُرند؛ بررسی مجوز نهایی هنگام اجرای خودِ
    عملیات انجام می‌شود (مخفی‌بودن دکمه کنترل دسترسی نیست — ۱۹.۴).
    """
    admins = services.admins_repo.ids_with_permission(PERM_APPROVE_PAYMENT)
    if not admins:
        return
    text = (f"🔔 سفارش در انتظار بررسی:\n\n{order_card(order, services)}")
    markup = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ تأیید", callback_data=f"a:orders:approve:{order['id']}:0"),
        InlineKeyboardButton(text="❌ رد", callback_data=f"a:orders:reject:{order['id']}"),
        InlineKeyboardButton(text="🧾 کارت کامل", callback_data=f"a:orders:card:{order['id']}"),
    ]])
    for admin_id in admins:
        try:
            await bot.send_message(admin_id, text, reply_markup=markup)
        except Exception as e:
            log.warning("اعلان رسید به مدیر %s ناموفق: %s", admin_id, e)


# ============================ منو و فهرست‌ها ============================
@router.callback_query(F.data == "a:orders:menu:")
@require(PERM_VIEW_ORDERS)
async def orders_menu(cb: CallbackQuery, services: Services, ctx=None):
    stats = services.orders_repo.stats()
    lines = [
        "🧾 سفارش‌ها و پرداخت‌ها:",
        "",
        f"• مجموع: {stats['total']}",
        f"• 🔔 در انتظار تصمیم: {stats['pending']}",
        f"• ✅ تأییدشده: {stats['confirmed']}",
        f"• ❌ ردشده: {stats['rejected']}",
        f"• 🚫 لغوشده: {stats['canceled']} | منقضی: {stats['expired']}",
        "",
        f"💰 فروش تأییدشده: {stats['revenue']:,} تومان",
    ]
    if stats.get("avg_review_seconds"):
        lines.append(f"⏱ میانگین زمان بررسی: {stats['avg_review_seconds'] // 60} دقیقه")
    await edit_or_answer(cb, "\n".join(lines), ak.menu_of("orders"))


@router.callback_query(F.data.startswith("a:orders:pending:"))
@require(PERM_VIEW_ORDERS)
async def orders_pending(cb: CallbackQuery, services: Services, ctx=None):
    """فهرست سفارش‌های در انتظار بررسی (همه وضعیت‌های بررسی — ۱۳.۶)."""
    offset = int(cb.data.split(":")[3] or 0)
    rows = []
    for status in REVIEW_STATUSES:
        rows += services.orders_repo.list(status=status, limit=25)
    rows.sort(key=lambda o: o["created_at"], reverse=True)
    total = len(rows)
    rows = rows[offset:offset + 10]
    if not rows:
        await edit_or_answer(cb, "🔔 سفارشی در انتظار بررسی نیست ✅", ak.menu_of("orders"))
        return
    lines = [f"🔔 در انتظار بررسی ({total}):", ""] + [_order_line(o) for o in rows]
    pages = ak.pager("a:orders:pending:", offset, total)
    await edit_or_answer(cb, "\n".join(lines), ak.with_back(pages))


@router.callback_query(F.data.startswith("a:orders:list:"))
@require(PERM_VIEW_ORDERS)
async def orders_list(cb: CallbackQuery, services: Services, ctx=None):
    """فهرست سفارش‌ها با فیلتر وضعیت و صفحه‌بندی."""
    parts = cb.data.split(":")
    status = parts[3] if parts[3] != "all" else None
    offset = int(parts[4] or 0)
    rows = services.orders_repo.list(status=status, limit=10, offset=offset)
    total = services.orders_repo.count(status)
    if not rows:
        await edit_or_answer(cb, "سفارشی با این فیلتر نیست.", ak.menu_of("orders"))
        return
    label = STATUS_LABELS.get(status, "همه") if status else "همه"
    lines = [f"🧾 سفارش‌ها — {label} ({total}):", ""] + [_order_line(o) for o in rows]
    pages = ak.pager(f"a:orders:list:{parts[3]}:", offset, total)
    await edit_or_answer(cb, "\n".join(lines), ak.with_back(pages))


@router.callback_query(F.data.startswith("a:orders:card:"))
@require(PERM_VIEW_ORDERS)
async def orders_card(cb: CallbackQuery, services: Services, ctx=None):
    order_id = cb.data.split(":")[3]
    order = services.orders_repo.get(order_id)
    if not order:
        await cb.answer("سفارش پیدا نشد.", show_alert=True)
        return
    await edit_or_answer(cb, order_card(order, services), order_actions(order))


@router.callback_query(F.data.startswith("a:orders:receipt:"))
@require(PERM_VIEW_ORDERS)
async def orders_receipt(cb: CallbackQuery, services: Services, ctx=None):
    """ارسال عکس رسید به مدیر — دسترسی محدود به مدیران دارای مجوز (بخش ۲۵.۶ سند ۲)."""
    order_id = cb.data.split(":")[3]
    order = services.orders_repo.get(order_id)
    if not order or not order["receipt_file_id"]:
        await cb.answer("رسیدی برای این سفارش ثبت نشده است.", show_alert=True)
        return
    await cb.answer("رسید ارسال می‌شود...")
    try:
        await cb.bot.send_photo(cb.from_user.id, order["receipt_file_id"],
                                caption=f"🧾 رسید سفارش <code>{order_id}</code>\n"
                                        + order_card(order, services),
                                reply_markup=order_actions(order))
    except Exception as e:
        log.warning("ارسال رسید %s ناموفق: %s", order_id, e)
        await cb.bot.send_message(cb.from_user.id,
                                  f"ارسال عکس رسید ناموفق بود: {e}",
                                  reply_markup=order_actions(order))


# ============================ تأیید و رد (گام ۸ و ۱۰ بخش ۱۳) ============================
@router.callback_query(F.data.startswith("a:orders:approve:"))
@require(PERM_APPROVE_PAYMENT)
async def orders_approve(cb: CallbackQuery, services: Services, ctx=None):
    """تأیید با تأییدیه دومرحله‌ای (بخش ۱۶.۸: عملیات حساس تأیید جداگانه دارد)."""
    parts = cb.data.split(":")
    order_id = parts[3]
    confirmed = len(parts) > 4 and parts[4] == "1"
    order = services.orders_repo.get(order_id)
    if not order:
        await cb.answer("سفارش پیدا نشد.", show_alert=True)
        return
    if order["status"] == "confirmed":
        await cb.answer("این سفارش قبلاً تأیید شده است.", show_alert=True)
        return
    if order["status"] not in REVIEW_STATUSES:
        await cb.answer("این سفارش در وضعیت قابل تأیید نیست.", show_alert=True)
        return
    if not confirmed:
        markup = ak.confirm_kb(f"a:orders:approve:{order_id}:1", f"a:orders:card:{order_id}")
        await edit_or_answer(
            cb,
            f"⚠️ تأیید پرداخت سفارش <code>{order_id}</code>؟\n\n{order_card(order, services)}",
            markup,
        )
        return
    ok, result = services.order_service.approve(order_id, cb.from_user.id)
    if not ok:
        await cb.answer(result, show_alert=True)
        return
    services.audit_repo.log(cb.from_user.id, "approve_order", "order", order_id,
                            new_value={"subscription_id": result})
    await edit_or_answer(cb, f"✅ پرداخت تأیید شد — اشتراک #{result} فعال/تمدید شد.",
                         order_actions(services.orders_repo.get(order_id)))
    # گام ۱۰: کاربر نتیجه را دریافت می‌کند
    sub = services.subs_repo.get(result)
    plan = services.plans_repo.get(order["plan_id"]) if order else None
    days = max(1, order["duration_seconds"] // 86400)
    expires = sub["expires_at"] if sub else None
    text = (f"✅ پرداخت سفارش <code>{order_id}</code> تأیید شد.\n"
            f"💎 پلن «{plan['title'] if plan else '-'}» ({days} روز) برایت فعال شد.")
    if expires:
        text += f"\n🗓 اعتبار تا: {ts_fa(expires)}"
    try:
        await cb.bot.send_message(order["user_id"], text)
    except Exception as e:
        log.warning("اطلاع تأیید به کاربر %s ناموفق: %s", order["user_id"], e)


@router.callback_query(F.data.startswith("a:orders:reject:"))
@require(PERM_REJECT_PAYMENT)
async def orders_reject(cb: CallbackQuery, state: FSMContext, services: Services, ctx=None):
    """رد با دلیل الزامی — سیاست رد مستند (بخش ۱۳.۷)."""
    order_id = cb.data.split(":")[3]
    order = services.orders_repo.get(order_id)
    if not order:
        await cb.answer("سفارش پیدا نشد.", show_alert=True)
        return
    if order["status"] not in REVIEW_STATUSES + ("awaiting_payment",):
        await cb.answer("این سفارش نهایی شده و قابل رد نیست.", show_alert=True)
        return
    await ask_input(state, cb,
                    f"❌ دلیل رد سفارش <code>{order_id}</code> را بنویس:\n"
                    "(این دلیل برای کاربر ارسال می‌شود)",
                    "orders:reject", order_id)


@router.callback_query(F.data.startswith("a:orders:needsreview:"))
@require(PERM_APPROVE_PAYMENT)
async def orders_needsreview(cb: CallbackQuery, services: Services, ctx=None):
    """علامت‌گذاری «نیازمند بررسی دستی» (وضعیت مستقل از صف عادی — ۱۵.۱۴)."""
    order_id = cb.data.split(":")[3]
    if services.orders_repo.needs_review(order_id):
        services.audit_repo.log(cb.from_user.id, "order_needs_review", "order", order_id)
        await edit_or_answer(cb, f"⚠️ سفارش <code>{order_id}</code> علامت‌خورد.",
                             order_actions(services.orders_repo.get(order_id)))
    else:
        await cb.answer("این سفارش در وضعیت قابل علامت‌گذاری نیست.", show_alert=True)


@router.callback_query(F.data == "a:orders:stats:")
@require(PERM_VIEW_ORDERS)
async def orders_stats(cb: CallbackQuery, services: Services, ctx=None):
    stats = services.orders_repo.stats()
    lines = [
        "📊 خلاصه وضعیت سفارش‌ها:",
        "",
        f"• مجموع: {stats['total']}",
        f"• 🔔 در انتظار تصمیم: {stats['pending']}",
        f"• ✅ تأییدشده: {stats['confirmed']}",
        f"• ❌ ردشده: {stats['rejected']}",
        f"• 🚫 لغوشده: {stats['canceled']} | منقضی: {stats['expired']}",
        "",
        f"💰 فروش تأییدشده: {stats['revenue']:,} تومان",
    ]
    if stats.get("avg_review_seconds"):
        lines.append(f"⏱ میانگین زمان بررسی: {stats['avg_review_seconds'] // 60} دقیقه")
    by_plan = services.orders_repo.sales_by_plan()
    if by_plan:
        lines.append("")
        lines.append("📈 فروش به تفکیک پلن:")
        for r in by_plan:
            lines.append(f"• {r['title']}: {r['cnt']} سفارش — {r['total']:,} تومان")
    await edit_or_answer(cb, "\n".join(lines), ak.menu_of("orders"))
