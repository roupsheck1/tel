"""پنل مدیریت تلگرام — بخش‌های ۱ تا ۴ (بخش ۱۶ سند).

گردش کار callback: «a:<section>:<op>:<arg>». هر عملیات مجوز خود را سمت
سرور بررسی می‌کند (بخش ۱۹.۴) و رویدادهای حساس در audit_log ثبت می‌شوند.
"""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from ...admin.permissions import (
    PERM_APPROVE_PAYMENT, PERM_BLOCK_USERS, PERM_CANCEL_SUBSCRIPTION, PERM_CHANGE_PLAN,
    PERM_CHANGE_PRICE, PERM_EDIT_USERS, PERM_REPORTS, PERM_TECH_SETTINGS, PERM_VIEW_ORDERS,
    PERM_VIEW_USERS, ROLE_LABELS,
)
from ...services import Services
from ..keyboards import admin as ak
from ..keyboards.user import ADMIN_PANEL_BUTTON
from .panel_utils import (
    AdminInput, ask_input, edit_or_answer, mention, parse_kv, require, ts_fa,
)

log = logging.getLogger("handlers.admin")
router = Router(name="admin")


# ============================ ورود به پنل ============================
async def _open_panel(message: Message, services: Services) -> None:
    """ورود به پنل — مجوز سمت سرور (§۲۶: شناسه عددی تلگرام مدیران مجاز)."""
    ctx = services.permissions.context_of(message.from_user.id)
    if not ctx:
        # تشخیص راه دور: شناسه ملاک واقعی است؛ اگر مدیر فکر می‌کند ادمین است
        # ولی این پیام را می‌گیرد، یعنی ADMIN_IDS سرویس با شناسه واقعی‌اش فرق دارد.
        log.warning(
            "ورود ناموفق به پنل: tg_id=%s username=%s — این شناسه در جدول مدیران نیست "
            "(در صورت ادمین بودن، ADMIN_IDS در لوسیتی باید همین عدد باشد)",
            message.from_user.id, getattr(message.from_user, "username", None),
        )
        await message.reply(
            "⛔ شما به پنل مدیریت دسترسی ندارید.\n"
            f"🆔 شناسه عددی تلگرام شما: <code>{message.from_user.id}</code>\n"
            "برای دسترسی مدیر، همین عدد باید در متغیر ADMIN_IDS سرویس تنظیم شود "
            "(لوسیتی → پروژه tel → سرویس tel → Variables). پس از ذخیره، سرویس "
            "خودکار دوباره اجرا و شما به‌عنوان مدیر شناخته می‌شوید."
        )
        return
    await message.reply(
        f"🛛 پنل مدیریت\nنقش شما: {ROLE_LABELS.get(ctx.role_name, ctx.role_name)}",
        reply_markup=ak.MAIN_MENU,
    )


@router.message(Command("panel"))
async def cmd_panel(message: Message, services: Services):
    await _open_panel(message, services)


@router.message(F.text == ADMIN_PANEL_BUTTON)
async def msg_open_panel(message: Message, services: Services):
    """دکمه «🛛 پنل مدیریت» در منوی اصلی (فقط برای مدیران نمایش داده می‌شود)."""
    await _open_panel(message, services)


# نکته: دکمه‌های منو «a:home:» می‌فرستند (قالب عمومی callback های پنل)؛
# هر دو شکل (با و بدون دونقطه پایانی) پذیرفته می‌شود تا هیچ دکمه‌ای بی‌پاسخ نماند.
@router.callback_query(F.data.in_({"a:home", "a:home:"}))
async def cb_home(cb: CallbackQuery, state: FSMContext, services: Services):
    ctx = services.permissions.context_of(cb.from_user.id)
    if not ctx:
        await cb.answer("دسترسی ندارید.", show_alert=True)
        return
    # پاک‌سازی هر جریان ورودی نیمه‌کاره (FSM) هنگام بازگشت به منوی اصلی
    await state.clear()
    await edit_or_answer(cb, "🛛 پنل مدیریت — بخش موردنظر را انتخاب کن:", ak.MAIN_MENU)


@router.callback_query(F.data.in_({"a:cancel", "a:cancel:"}))
async def cb_cancel(cb: CallbackQuery, state: FSMContext, services: Services):
    await state.clear()
    await edit_or_answer(cb, "✅ عملیات لغو شد.", ak.MAIN_MENU)


@router.callback_query(F.data == "a:back")
async def cb_back(cb: CallbackQuery, services: Services):
    await edit_or_answer(cb, "بازگشت.", ak.MAIN_MENU)


# ============================ ۱) مدیریت کاربران ============================
@router.callback_query(F.data == "a:users:menu:")
@require(PERM_VIEW_USERS)
async def users_menu(cb: CallbackQuery, services: Services, ctx=None):
    await edit_or_answer(cb, "👥 مدیریت کاربران:", ak.menu_of("users"))


@router.callback_query(F.data == "a:users:search:")
@require(PERM_VIEW_USERS)
async def users_search(cb: CallbackQuery, state: FSMContext, services: Services, ctx=None):
    await ask_input(
        state, cb,
        "🔍 جست‌وجوی کاربر\n"
        "شناسه عددی تلگرام یا یوزرنیم کاربر را بفرست:\n\n"
        "📌 نمونه:\n"
        "123456789 یا @user_name",
        "users:search",
    )


@router.callback_query(F.data.startswith("a:users:list:"))
@require(PERM_VIEW_USERS)
async def users_list(cb: CallbackQuery, services: Services, ctx=None):
    offset = int(cb.data.split(":")[3] or 0)
    rows = services.users_repo.search(limit=10, offset=offset)
    total = services.users_repo.count()
    if not rows:
        await edit_or_answer(cb, "کاربری یافت نشد.", ak.menu_of("users"))
        return
    lines = [f"👥 کاربران (مجموع {total}):", ""]
    kbrows = []
    for u in rows:
        status = "🚫" if u["status"] == "blocked" else "✅"
        lines.append(
            f"{status} {mention(u['telegram_id'], u['username'])} — {ts_fa(u['registered_at'])}"
        )
        kbrows.append([(f"{u['telegram_id']}", f"a:users:card:{u['telegram_id']}")])
    kbrows += ak.pager("a:users:list:", offset, total)
    kbrows.append(ak.BACK_HOME_ROW)
    await edit_or_answer(cb, "\n".join(lines), ak.kb(kbrows))


@router.callback_query(F.data == "a:users:blocked:0")
@require(PERM_VIEW_USERS)
async def users_blocked(cb: CallbackQuery, services: Services, ctx=None):
    rows = services.db.query("SELECT * FROM users WHERE status='blocked' LIMIT 20")
    if not rows:
        await edit_or_answer(cb, "کاربر مسدودی وجود ندارد ✅", ak.menu_of("users"))
        return
    lines = ["🚫 کاربران مسدود:", ""]
    kbrows = []
    for u in rows:
        lines.append(f"• {mention(u['telegram_id'], u['username'])} — {u['blocked_reason'] or '-'}")
        kbrows.append([(str(u["telegram_id"]), f"a:users:card:{u['telegram_id']}")])
    kbrows.append(ak.BACK_HOME_ROW)
    await edit_or_answer(cb, "\n".join(lines), ak.kb(kbrows))


@router.callback_query(F.data.startswith("a:users:card:"))
@require(PERM_VIEW_USERS)
async def users_card(cb: CallbackQuery, services: Services, ctx=None):
    uid = int(cb.data.split(":")[3])
    u = services.users_repo.get(uid)
    if not u:
        await cb.answer("کاربر پیدا نشد.")
        return
    sub = services.subs_repo.active_of(uid)
    link = services.links_repo.get_active(uid)
    orders = services.orders_repo.list(user_id=uid, limit=5)
    used = services.downloads_repo.quota_used(uid)
    lines = [
        f"👤 کاربر {mention(uid, u['username'])}",
        f"وضعیت: {'مسدود 🚫' if u['status'] == 'blocked' else 'فعال ✅'}",
        f"ثبت‌نام: {ts_fa(u['registered_at'])}",
        f"اینستاگرام: {'@' + link['ig_username'] if link else 'متصل نیست'}",
        f"اشتراک فعال: "
        + (f"{services.plans_repo.get(sub['plan_id'])['title']} تا {ts_fa(sub['expires_at'])}" if sub else "ندارد"),
        f"دانلود موفق: امروز {used['daily']} / کل {used['total']}",
        f"سفارش‌ها: {len(orders)}",
    ]
    kbrows = [
        [("🚫 مسدود", f"a:users:block:{uid}"), ("✅ رفع مسدودی", f"a:users:unblock:{uid}")]
        if u["status"] != "blocked" else [("✅ رفع مسدودی", f"a:users:unblock:{uid}")],
        [("🎁 هدیه اشتراک", f"a:users:gift:{uid}")],
        [("➕ تمدید (روز)", f"a:users:extend:{uid}")],
        [("⚡️ استثنای فردی", f"a:users:exception:{uid}")],
        [("👥 افزودن به گروه", f"a:users:addtogroup:{uid}")],
        [("🔗 قطع اتصال اینستاگرام", f"a:users:unlink:{uid}")],
    ]
    kbrows.append(ak.BACK_HOME_ROW)
    await edit_or_answer(cb, "\n".join(lines), ak.kb(kbrows))


@router.callback_query(F.data.startswith("a:users:block:"))
@require(PERM_BLOCK_USERS)
async def users_block(cb: CallbackQuery, services: Services, ctx=None):
    uid = int(cb.data.split(":")[3])
    ok = services.users_repo.set_blocked(uid, True, reason=f"by:{ctx.telegram_id}")
    services.audit_repo.log(ctx.telegram_id, "block_user", "user", str(uid), result="success" if ok else "noop")
    await cb.answer("مسدود شد ✅" if ok else "قبلاً مسدود بوده.")
    await users_card(cb, services)


@router.callback_query(F.data.startswith("a:users:unblock:"))
@require(PERM_BLOCK_USERS)
async def users_unblock(cb: CallbackQuery, services: Services, ctx=None):
    uid = int(cb.data.split(":")[3])
    ok = services.users_repo.set_blocked(uid, False)
    services.audit_repo.log(ctx.telegram_id, "unblock_user", "user", str(uid), result="success" if ok else "noop")
    await cb.answer("رفع مسدودیت شد ✅" if ok else "تغییری نکرد.")
    await users_card(cb, services)


@router.callback_query(F.data.startswith("a:users:gift:"))
@require(PERM_CHANGE_PLAN)
async def users_gift(cb: CallbackQuery, services: Services, ctx=None):
    uid = int(cb.data.split(":")[3])
    plans = services.plans_repo.list()
    kbrows = [[(p["title"], f"a:users:gift2:{uid}:{p['id']}")] for p in plans]
    kbrows.append(ak.BACK_HOME_ROW)
    await edit_or_answer(cb, "🎁 پلن هدیه را انتخاب کن:", ak.kb(kbrows))


@router.callback_query(F.data.startswith("a:users:gift2:"))
@require(PERM_CHANGE_PLAN)
async def users_gift2(cb: CallbackQuery, services: Services, ctx=None):
    _, _, _, uid, plan_id = cb.data.split(":")
    sub_id = services.sub_service.gift_days(int(uid), int(plan_id), 0, by_admin=ctx.telegram_id)
    services.audit_repo.log(ctx.telegram_id, "gift_subscription", "user", uid,
                            new_value={"plan": int(plan_id), "subscription": sub_id})
    await cb.answer("هدیه فعال شد ✅")
    await users_card(cb, services)


@router.callback_query(F.data.startswith("a:users:extend:"))
@require(PERM_CHANGE_PLAN)
async def users_extend(cb: CallbackQuery, state: FSMContext, services: Services, ctx=None):
    uid = cb.data.split(":")[3]
    await ask_input(
        state, cb,
        "➕ تمدید اشتراک\n"
        "تعداد روز تمدید را به عدد بفرست:\n\n"
        "📌 نمونه:\n"
        "7",
        "users:extend", uid,
    )


@router.callback_query(F.data.startswith("a:users:exception:"))
@require(PERM_EDIT_USERS)
async def users_exception(cb: CallbackQuery, state: FSMContext, services: Services, ctx=None):
    uid = cb.data.split(":")[3]
    await ask_input(
        state, cb,
        "⚡️ استثنای فردی کاربر\n"
        "فیلدهای دلخواه را به شکل «کلید=مقدار» بفرست (همه اختیاری‌اند):\n"
        "1️⃣ concurrent: تعداد دانلود هم‌زمان\n"
        "2️⃣ wait: زمان انتظار به ثانیه\n"
        "3️⃣ hourly: سهمیه ساعتی\n"
        "4️⃣ daily: سهمیه روزانه\n"
        "5️⃣ priority: اولویت در صف\n"
        "6️⃣ days: مدت اعتبار استثنا به روز\n\n"
        "📌 نمونه:\n"
        "concurrent=3, wait=0, hourly=20, daily=100, priority=2, days=7",
        "users:exception", uid,
    )


@router.callback_query(F.data.startswith("a:users:unlink:"))
@require(PERM_EDIT_USERS)
async def users_unlink(cb: CallbackQuery, services: Services, ctx=None):
    uid = int(cb.data.split(":")[3])
    ig = services.links_repo.unlink_by_admin(uid)
    services.audit_repo.log(ctx.telegram_id, "unlink_instagram", "user", str(uid),
                            new_value={"ig": ig})
    await cb.answer(f"اتصال @{ig} قطع شد." if ig else "اتصالی نبود.")
    await users_card(cb, services)


@router.callback_query(F.data.startswith("a:users:addtogroup:"))
@require(PERM_EDIT_USERS)
async def users_addtogroup(cb: CallbackQuery, state: FSMContext, services: Services, ctx=None):
    """افزودن کاربر به گروه دستی (§۹ سند ۲: «افزودن به گروه‌های دستی»)."""
    uid = cb.data.split(":")[3]
    groups = services.groups_repo.list()
    kbrows = [[(g["name"], f"a:users:addgroup2:{uid}:{g['id']}")] for g in groups]
    kbrows.append(ak.BACK_HOME_ROW)
    await edit_or_answer(cb, f"👥 کاربر <code>{uid}</code> به کدام گروه اضافه شود؟",
                        ak.kb(kbrows))


@router.callback_query(F.data.startswith("a:users:addgroup2:"))
@require(PERM_EDIT_USERS)
async def users_addtogroup2(cb: CallbackQuery, services: Services, ctx=None):
    _, _, _, uid, gid = cb.data.split(":")
    added = services.groups_repo.add_members(int(gid), [int(uid)])
    services.audit_repo.log(ctx.telegram_id, "add_group_member", "group", gid,
                            new_value={"user": int(uid), "added": added})
    await cb.answer("عضو اضافه شد ✅" if added else "قبلاً عضو بوده.")
    await users_card(cb, services)


# ============================ ۲) اشتراک‌ها و پلن‌ها ============================
@router.callback_query(F.data == "a:subs:menu:")
@require(PERM_VIEW_ORDERS)
async def subs_menu(cb: CallbackQuery, services: Services, ctx=None):
    await edit_or_answer(cb, "💎 مدیریت اشتراک‌ها و پلن‌ها:", ak.menu_of("subs"))


@router.callback_query(F.data == "a:subs:plans:")
@require(PERM_VIEW_ORDERS)
async def subs_plans(cb: CallbackQuery, services: Services, ctx=None):
    plans = services.plans_repo.list(include_archived=True)
    if not plans:
        await edit_or_answer(cb, "پلنی ثبت نشده است.", ak.menu_of("subs"))
        return
    lines = ["💎 پلن‌ها:", ""]
    kbrows = []
    for p in plans:
        v = services.plans_repo.current_version(p["id"])
        cfg = services.plan_service.config_of_version(v) if v else None
        archived = " [بایگانی]" if p["is_archived"] else ""
        price = f"{cfg.price:,} تومان" if cfg else "-"
        lines.append(f"• {p['title']} — {price} (نسخه {v['version'] if v else '-'}){archived}")
        kbrows.append([(p["title"], f"a:subs:plan:{p['id']}")])
    kbrows.append(ak.BACK_HOME_ROW)
    await edit_or_answer(cb, "\n".join(lines), ak.kb(kbrows))


@router.callback_query(F.data.startswith("a:subs:plan:"))
@require(PERM_VIEW_ORDERS)
async def subs_plan_card(cb: CallbackQuery, services: Services, ctx=None):
    plan_id = int(cb.data.split(":")[3])
    plan = services.plans_repo.get(plan_id)
    if not plan:
        await cb.answer("پلن پیدا نشد.")
        return
    v = services.plans_repo.current_version(plan_id)
    cfg = services.plan_service.config_of_version(v)
    drafts = services.db.query(
        "SELECT * FROM plan_versions WHERE plan_id=? AND status='draft' ORDER BY version DESC",
        (plan_id,))
    lines = [
        f"💎 {plan['title']} (نام داخلی: {plan['name']})",
        plan["description"] or "",
        "",
        f"نسخه منتشرشده: {v['version']} — قیمت {cfg.price:,} تومان — {cfg.duration_label}",
        f"هم‌زمان: {cfg.concurrent_downloads} | انتظار: {cfg.wait_seconds}s | صف: {cfg.queue_limit}",
        f"سقف فایل: {cfg.max_file_mb}MB | اولویت: {cfg.priority}",
        f"مشترکان فعال: {services.plans_repo.affected_subscribers(plan_id)}",
    ]
    kbrows = [
        [("💰 تغییر قیمت (نسخه جدید)", f"a:subs:price:{plan_id}")],
        [("📜 نسخه‌ها", f"a:subs:versions:{plan_id}")],
        [("📦 بایگانی پلن", f"a:subs:archive:{plan_id}")],
    ]
    if drafts:
        lines.append("", "⚠️ نسخه‌های پیش‌نویس (منتشرنشده):")
        kbrows.insert(0, [(f"انتشار پیش‌نویس v{d['version']}", f"a:subs:publish:{d['id']}") for d in drafts[:2]][0])
    kbrows.append(ak.BACK_HOME_ROW)
    await edit_or_answer(cb, "\n".join(lines), ak.kb(kbrows))


@router.callback_query(F.data.startswith("a:subs:price:"))
@require(PERM_CHANGE_PRICE)
async def subs_price(cb: CallbackQuery, state: FSMContext, services: Services, ctx=None):
    plan_id = cb.data.split(":")[3]
    await ask_input(
        state, cb,
        "💰 تغییر قیمت پلن\n"
        "قیمت جدید را به تومان بفرست:\n\n"
        "📌 نمونه:\n"
        "299000",
        "subs:price", plan_id,
    )


@router.callback_query(F.data.startswith("a:subs:publish:"))
@require(PERM_CHANGE_PRICE)
async def subs_publish(cb: CallbackQuery, services: Services, ctx=None):
    version_id = int(cb.data.split(":")[3])
    v = services.plans_repo.version(version_id)
    # پیش‌نمایش تغییرات حساس (بخش ۹.۴)
    preview = services.plan_service.change_preview(v["plan_id"],
                                                   services.plan_service.config_of_version(v))
    await edit_or_answer(
        cb,
        "⚠️ انتشار نسخه جدید — دامنه اعمال را انتخاب کن:\n"
        f"تعداد مشترکان فعلی این پلن: {preview['affected_subscribers']}",
        ak.kb([
            [("🛒 فقط خریداران آینده", f"a:subs:pub2:{version_id}:future")],
            [("👥 فقط مشترکان فعلی", f"a:subs:pub2:{version_id}:current")],
            [("🔄 هر دو گروه", f"a:subs:pub2:{version_id}:both")],
            ak.BACK_HOME_ROW,
        ]),
    )


@router.callback_query(F.data.startswith("a:subs:pub2:"))
@require(PERM_CHANGE_PRICE)
async def subs_publish2(cb: CallbackQuery, services: Services, ctx=None):
    _, _, _, version_id, scope = cb.data.split(":")
    ok, msg = services.plan_service.publish(int(version_id), scope, ctx.telegram_id)
    services.audit_repo.log(ctx.telegram_id, "publish_plan_version", "plan_version", version_id,
                            new_value={"scope": scope}, result="success" if ok else "failed")
    await cb.answer(msg, show_alert=not ok)
    # بازگشت به کارت پلن
    v = services.plans_repo.version(int(version_id))
    cb.data = f"a:subs:plan:{v['plan_id']}"
    await subs_plan_card(cb, services)


@router.callback_query(F.data.startswith("a:subs:versions:"))
@require(PERM_VIEW_ORDERS)
async def subs_versions(cb: CallbackQuery, services: Services, ctx=None):
    plan_id = int(cb.data.split(":")[3])
    rows = services.db.query(
        "SELECT * FROM plan_versions WHERE plan_id=? ORDER BY version DESC", (plan_id,))
    plan = services.plans_repo.get(plan_id)
    lines = [f"📜 نسخه‌های «{plan['title']}»:", ""]
    for v in rows:
        status = {"draft": "پیش‌نویس", "published": "منتشرشده", "archived": "بایگانی"}[v["status"]]
        cfg = services.plan_service.config_of_version(v)
        lines.append(f"v{v['version']} — {status} — {cfg.price:,} تومان — {ts_fa(v['created_at'])}")
    lines.append("", "سوابق خرید به نسخه لحظه خرید متصل‌اند و تغییرات اثری روی آن‌ها ندارد.")
    await edit_or_answer(cb, "\n".join(lines), ak.with_back([]))


@router.callback_query(F.data.startswith("a:subs:archive:"))
@require(PERM_CHANGE_PLAN)
async def subs_archive(cb: CallbackQuery, services: Services, ctx=None):
    plan_id = int(cb.data.split(":")[3])
    plan = services.plans_repo.get(plan_id)
    await edit_or_answer(
        cb,
        f"⚠️ بایگانی «{plan['title']}»؟\nسفارش‌ها و اشتراک‌های تاریخی حفظ می‌شوند و فروش متوقف است.",
        ak.confirm_kb(f"a:subs:archive2:{plan_id}", "a:subs:plans:"),
    )


@router.callback_query(F.data.startswith("a:subs:archive2:"))
@require(PERM_CHANGE_PLAN)
async def subs_archive2(cb: CallbackQuery, services: Services, ctx=None):
    plan_id = int(cb.data.split(":")[3])
    ok = services.plans_repo.archive_plan(plan_id)
    services.audit_repo.log(ctx.telegram_id, "archive_plan", "plan", str(plan_id),
                            result="success" if ok else "noop")
    await cb.answer("بایگانی شد ✅" if ok else "تغییری نکرد.")
    cb.data = "a:subs:plans:"
    await subs_plans(cb, services)


@router.callback_query(F.data == "a:subs:newplan:")
@require(PERM_CHANGE_PLAN)
async def subs_newplan(cb: CallbackQuery, state: FSMContext, services: Services, ctx=None):
    await ask_input(
        state, cb,
        "➕ ساخت پلن جدید\n"
        "می‌خواهی سریع و دقیق ثبت کنی؟ فقط این ۷ فیلد را به‌ترتیب پر کن:\n"
        "1️⃣ name: نام انگلیسی پلن\n"
        "2️⃣ عنوان: نام نمایشی پلن\n"
        "3️⃣ قیمت: مبلغ پلن\n"
        "4️⃣ مدت_روز: مدت اعتبار به روز\n"
        "5️⃣ هم‌زمان: تعداد کاربر هم‌زمان\n"
        "6️⃣ انتظار_ثانیه: زمان انتظار به ثانیه\n"
        "7️⃣ ظرفیت_صف: ظرفیت صف\n\n"
        "📌 نمونه:\n"
        "name: gold\n"
        "عنوان: طلایی\n"
        "قیمت: 299000\n"
        "مدت_روز: 30\n"
        "هم‌زمان: 2\n"
        "انتظار_ثانیه: 10\n"
        "ظرفیت_صف: 15",
        "subs:newplan",
    )


@router.callback_query(F.data == "a:subs:salesmode:")
@require(PERM_VIEW_ORDERS)
async def subs_salesmode(cb: CallbackQuery, services: Services, ctx=None):
    mode = services.settings.sales_mode
    labels = {"free": "🎁 رایگان", "scheduled": "⏰ زمان‌بندی‌شده", "paid": "💰 فروش فعال"}
    start = services.settings.get("sales.scheduled_start")
    lines = [
        "💰 حالت تجاری ربات:",
        f"وضعیت فعلی: {labels.get(mode, mode)}",
        f"شروع زمان‌بندی‌شده: {ts_fa(start) if start else '-'}",
        "",
        "توقف فروش اشتراک‌های فعال را باطل نمی‌کند (بخش ۱۱.۵ سند).",
    ]
    await edit_or_answer(cb, "\n".join(lines), ak.kb([
        [("🎁 حالت رایگان", "a:subs:mode:free")],
        [("💰 شروع فروش فوری", "a:subs:mode:paid")],
        [("⏰ شروع زمان‌بندی‌شده", "a:subs:sched:")],
        ak.BACK_HOME_ROW,
    ]))


@router.callback_query(F.data.startswith("a:subs:mode:"))
@require(PERM_CHANGE_PRICE)
async def subs_set_mode(cb: CallbackQuery, services: Services, ctx=None):
    mode = cb.data.split(":")[3]
    if mode == "free":
        services.settings.back_to_free()
    else:
        services.settings.set("sales.mode", "paid")
        services.settings.set("sales.selling_stopped", False)
    services.audit_repo.log(ctx.telegram_id, "set_sales_mode", "settings", mode)
    await cb.answer("اعمال شد ✅")
    await subs_salesmode(cb, services)


@router.callback_query(F.data == "a:subs:sched:")
@require(PERM_CHANGE_PRICE)
async def subs_sched(cb: CallbackQuery, state: FSMContext, services: Services, ctx=None):
    await ask_input(
        state, cb,
        "⏰ شروع زمان‌بندی‌شده فروش\n"
        "زمان شروع را به فرمت زیر بفرست (به وقت UTC):\n\n"
        "📌 نمونه:\n"
        "2026-01-15 18:30",
        "subs:sched",
    )


@router.callback_query(F.data == "a:subs:paymethods:")
@require(PERM_VIEW_ORDERS)
async def subs_paymethods(cb: CallbackQuery, services: Services, ctx=None):
    rows = services.payments_repo.list()
    lines = ["💳 روش‌های پرداخت:", ""]
    kbrows = []
    for m in rows:
        status = "فعال ✅" if m["is_active"] else "غیرفعال"
        lines.append(f"• [{m['id']}] {m['title']} — {m['card_number']} ({status})")
        kbrows.append([(f"{'✅' if m['is_active'] else '⭕️'} {m['title']}",
                        f"a:subs:paytoggle:{m['id']}")])
    kbrows.append(ak.BACK_HOME_ROW)
    await edit_or_answer(cb, "\n".join(lines), ak.kb(kbrows))


@router.callback_query(F.data.startswith("a:subs:paytoggle:"))
@require(PERM_CHANGE_PRICE)
async def subs_paytoggle(cb: CallbackQuery, services: Services, ctx=None):
    mid = int(cb.data.split(":")[3])
    m = services.payments_repo.get_active(mid)
    services.payments_repo.set_active(mid, not (m and m["id"] == mid))
    services.audit_repo.log(ctx.telegram_id, "toggle_payment_method", "payment_method", str(mid))
    await cb.answer("تغییر کرد.")
    await subs_paymethods(cb, services)


# ============================ ۳) آمار و گزارش‌ها ============================
@router.callback_query(F.data == "a:stats:menu:")
@require(PERM_REPORTS)
async def stats_menu(cb: CallbackQuery, services: Services, ctx=None):
    await edit_or_answer(cb, "📊 آمار و گزارش‌ها:", ak.menu_of("stats"))


@router.callback_query(F.data == "a:stats:dash:")
@require(PERM_REPORTS)
async def stats_dash(cb: CallbackQuery, services: Services, ctx=None):
    o = services.orders_repo.stats()
    s = services.subs_repo.stats()
    d = services.downloads_repo.stats()
    now_day = services.orders_repo.sales_between(int(__import__("time").time()) - 86400,
                                                 int(__import__("time").time()))
    lines = [
        "📈 داشبورد کلی:",
        "",
        f"👥 کاربران: {services.users_repo.count()} (با اشتراک فعال: {services.users_repo.count(True)})",
        f"💎 اشتراک فعال: {s['active']} | منقضی: {s['expired']} | لغوشده: {s['canceled']}",
        f"📥 دانلود موفق: {d['completed']} (۲۴س اخیر: {d['today']}) | ناموفق: {d['failed']}",
        "",
        f"🧾 سفارش‌ها: {o['total']}",
        f"✅ تأییدشده: {o['confirmed']} — فروش کل: {o['revenue']:,} تومان",
        f"⏳ در انتظار بررسی: {o['pending']} | ردشده: {o['rejected']}",
        f"📊 فروش ۲۴ ساعت اخیر: {now_day['cnt']} سفارش / {now_day['total']:,} تومان",
        f"⏱ میانگین زمان بررسی سفارش: "
        + (f"{o['avg_review_seconds'] // 60} دقیقه" if o['avg_review_seconds'] else "-"),
    ]
    await edit_or_answer(cb, "\n".join(lines), ak.with_back([]))


@router.callback_query(F.data == "a:stats:ledger:")
@require(PERM_REPORTS)
async def stats_ledger(cb: CallbackQuery, services: Services, ctx=None):
    rows = services.orders_repo.sales_by_plan()
    import time as _t
    week = services.orders_repo.sales_between(int(_t.time()) - 7 * 86400, int(_t.time()))
    month = services.orders_repo.sales_between(int(_t.time()) - 30 * 86400, int(_t.time()))
    lines = ["🧾 دفتر فروش (فقط سفارش‌های تأییدشده):", ""]
    if not rows:
        lines.append("فروشی ثبت نشده است.")
    for r in rows:
        lines.append(f"• {r['title']}: {r['cnt']} سفارش — {r['total']:,} تومان")
    lines += [
        "",
        f"📅 ۷ روز اخیر: {week['cnt']} سفارش / {week['total']:,} تومان",
        f"📅 ۳۰ روز اخیر: {month['cnt']} سفارش / {month['total']:,} تومان",
    ]
    await edit_or_answer(cb, "\n".join(lines), ak.with_back([]))


@router.callback_query(F.data == "a:stats:users:")
@require(PERM_REPORTS)
async def stats_users(cb: CallbackQuery, services: Services, ctx=None):
    groups = services.groups_repo.list()
    lines = ["👥 آمار گروه‌های کاربری:", ""]
    for g in groups:
        lines.append(f"• {g['name']}: {len(services.groups_repo.member_ids(g['id']))} نفر")
    lines.append(f"کل کاربران: {services.users_repo.count()}")
    await edit_or_answer(cb, "\n".join(lines), ak.with_back([]))


# ============================ ۴) دانلودها و صف ============================
@router.callback_query(F.data == "a:downloads:menu:")
@require(PERM_TECH_SETTINGS)
async def downloads_menu(cb: CallbackQuery, services: Services, ctx=None):
    await edit_or_answer(cb, "📥 مدیریت دانلودها و صف:", ak.menu_of("downloads"))


@router.callback_query(F.data == "a:downloads:queue:")
@require(PERM_TECH_SETTINGS)
async def downloads_queue(cb: CallbackQuery, services: Services, ctx=None):
    counts = services.downloads_repo.status_counts()
    labels = {"queued": "در صف", "downloading": "در حال دانلود", "sending": "در حال ارسال",
              "completed": "تکمیل‌شده", "failed": "ناموفق", "canceled": "لغوشده",
              "expired": "منقضی", "ready_to_send": "آماده ارسال"}
    lines = ["📋 وضعیت صف دانلود:", ""]
    for key, label in labels.items():
        if counts.get(key):
            lines.append(f"• {label}: {counts[key]}")
    queued = services.downloads_repo.queued(limit=15)
    kbrows = []
    if queued:
        lines += ["", "درخواست‌های در صف (اولویت با دکمه قابل تغییر است — §۶۴):"]
        for r in queued:
            lines.append(
                f"#{r['id']} {mention(r['user_id'], None)} {r['shortcode']} — اولویت {r['priority']}"
            )
            kbrows.append([(f"⏫ اولویت #{r['id']} → {r['priority'] + 1}",
                            f"a:downloads:prio:{r['id']}:{r['priority'] + 1}")])
    lines += [
        "",
        "🔎 برای پیگیری یک درخواست، شناسه پیگیری (مثل #۱۲) را جست‌وجو کن.",
    ]
    kbrows.append([("🔎 جستجوی شناسه پیگیری", "a:downloads:track:")])
    await edit_or_answer(cb, "\n".join(lines), ak.with_back(kbrows))


@router.callback_query(F.data.startswith("a:downloads:prio:"))
@require(PERM_TECH_SETTINGS)
async def downloads_set_priority(cb: CallbackQuery, services: Services, ctx=None):
    """افزایش اولویت دستی درخواست صف (§۶۴: مدیر بتواند درخواست را به اولویت بالاتر برد)."""
    parts = cb.data.split(":")
    rid, prio = int(parts[3]), int(parts[4])
    ok = services.downloads_repo.set_priority(rid, prio)
    services.audit_repo.log(ctx.telegram_id, "set_queue_priority", "request", str(rid),
                            new_value={"priority": prio}, result="success" if ok else "noop")
    await cb.answer("اولویت اعمال شد ✅" if ok else "درخواست دیگر در صف نیست.")
    await downloads_queue(cb, services)


@router.callback_query(F.data == "a:downloads:track:")
@require(PERM_TECH_SETTINGS)
async def downloads_track_prompt(cb: CallbackQuery, state: FSMContext, services: Services, ctx=None):
    await ask_input(
        state, cb,
        "🔎 پیگیری درخواست دانلود\n"
        "شناسه پیگیری را بفرست (فقط عدد):\n\n"
        "📌 نمونه:\n"
        "12\n\n"
        "ℹ️ کاربر این شناسه را از ربات گرفته و به پشتیبانی می‌دهد (§۵۶).",
        "downloads:track",
    )


@router.callback_query(F.data == "a:downloads:failed:")
@require(PERM_TECH_SETTINGS)
async def downloads_failed(cb: CallbackQuery, services: Services, ctx=None):
    rows = services.downloads_repo.recent(status="failed", limit=10)
    if not rows:
        await edit_or_answer(cb, "درخواست ناموفقی وجود ندارد ✅", ak.menu_of("downloads"))
        return
    lines = ["❌ درخواست‌های ناموفق (برای تلاش مجدد بزن):", ""]
    kbrows = []
    for r in rows:
        err = (r["last_error"] or "")[:60]
        lines.append(f"#{r['id']} {r['shortcode']} — {err}")
        kbrows.append([(f"🔁 تلاش #{r['id']}", f"a:downloads:retry:{r['id']}")])
    kbrows.append(ak.BACK_HOME_ROW)
    await edit_or_answer(cb, "\n".join(lines), ak.kb(kbrows))


@router.callback_query(F.data.startswith("a:downloads:retry:"))
@require(PERM_TECH_SETTINGS)
async def downloads_retry(cb: CallbackQuery, services: Services, ctx=None):
    rid = int(cb.data.split(":")[3])
    ok = services.downloads_repo.retry(rid)
    services.audit_repo.log(ctx.telegram_id, "retry_download", "request", str(rid),
                            result="success" if ok else "noop")
    await cb.answer("به صف بازگشت ✅" if ok else "امکان‌پذیر نیست.")
    await downloads_failed(cb, services)


@router.callback_query(F.data == "a:downloads:capacity:")
@require(PERM_TECH_SETTINGS)
async def downloads_capacity(cb: CallbackQuery, services: Services, ctx=None):
    s = services.settings
    lines = [
        "⚙️ ظرفیت کلی سرور (بخش ۱۵.۸):",
        f"• حداکثر دانلود فعال: {s.get('capacity.max_active_downloads')}",
        f"• حداکثر ارسال فعال: {s.get('capacity.max_active_sends')}",
        f"• حداکثر کل صف: {s.get('capacity.max_queue_total')}",
        "",
        "برای تغییر، مقدار جدید را به شکل «کلید=مقدار» بفرست:",
        "1️⃣ dl: حداکثر دانلود فعال",
        "2️⃣ send: حداکثر ارسال فعال",
        "3️⃣ queue: حداکثر کل صف",
        "",
        "📌 نمونه:",
        "dl=3, send=5, queue=100",
    ]
    await edit_or_answer(cb, "\n".join(lines), ak.with_back([[("✏️ ویرایش", "a:downloads:capedit:")]]))


@router.callback_query(F.data == "a:downloads:capedit:")
@require(PERM_TECH_SETTINGS)
async def downloads_capedit(cb: CallbackQuery, state: FSMContext, services: Services, ctx=None):
    await ask_input(
        state, cb,
        "⚙️ تغییر ظرفیت سرور\n"
        "مقادیر جدید را به شکل «کلید=مقدار» بفرست:\n\n"
        "📌 نمونه:\n"
        "dl=3, send=5, queue=100",
        "downloads:capacity",
    )
