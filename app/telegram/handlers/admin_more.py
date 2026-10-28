"""پنل مدیریت — بخش‌های ۵ تا ۱۰ و دیسپچر ورودی مدیر (بخش ۱۶ سند)."""
from __future__ import annotations

import asyncio
import logging
import time

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from ...admin.permissions import (
    PERM_BROADCAST, PERM_CREATE_PROMO, PERM_MANAGE_ADMINS, PERM_REPORTS,
    PERM_TECH_SETTINGS, PERM_VIEW_USERS, ROLE_LABELS, ALL_PERMISSIONS, PERM_LABELS,
)
from ...services import Services
from ..keyboards import admin as ak
from .panel_utils import AdminInput, ask_input, edit_or_answer, mention, parse_kv, require, ts_fa

log = logging.getLogger("handlers.admin_more")
router = Router(name="admin_more")


# ============================ ۵) کدهای اشتراک ============================
@router.callback_query(F.data == "a:codes:menu:")
@require(PERM_CREATE_PROMO)
async def codes_menu(cb: CallbackQuery, services: Services, ctx=None):
    await edit_or_answer(cb, "🎟 مدیریت کدهای اشتراک:", ak.menu_of("codes"))


@router.callback_query(F.data == "a:codes:new:")
@require(PERM_CREATE_PROMO)
async def codes_new(cb: CallbackQuery, state: FSMContext, services: Services, ctx=None):
    plans = services.plans_repo.list()
    kbrows = [[(p["title"], f"a:codes:plan:{p['id']}")] for p in plans]
    kbrows.append(ak.BACK_HOME_ROW)
    await edit_or_answer(cb, "🎟 پلن کد را انتخاب کن:", ak.kb(kbrows))


@router.callback_query(F.data.startswith("a:codes:plan:"))
@require(PERM_CREATE_PROMO)
async def codes_plan(cb: CallbackQuery, state: FSMContext, services: Services, ctx=None):
    plan_id = cb.data.split(":")[3]
    await ask_input(
        state, cb,
        "🎟 مشخصات کد را بفرست:\n"
        "<code>days=30, count=5, max_uses=1, expires_hours=48</code>\n"
        "(count = تعداد کد؛ max_uses خالی = نامحدود)",
        "codes:new", plan_id,
    )


@router.callback_query(F.data.startswith("a:codes:list:"))
@require(PERM_CREATE_PROMO)
async def codes_list(cb: CallbackQuery, services: Services, ctx=None):
    offset = int(cb.data.split(":")[3] or 0)
    rows = services.codes_repo.list(limit=10, offset=offset)
    if not rows:
        await edit_or_answer(cb, "کدی ثبت نشده است.", ak.menu_of("codes"))
        return
    lines = ["🎟 کدهای اشتراک:", ""]
    kbrows = []
    for r in rows:
        status = {"active": "فعال", "canceled": "لغوشده", "exhausted": "تمام‌شده"}[r["status"]]
        lines.append(
            f"• <code>{r['code']}</code> — {r['plan_title']} — {r['duration_seconds'] // 86400}روز "
            f"— مصرف {r['used_count']}/{r['max_uses'] or '∞'} — {status}"
        )
        if r["status"] == "active" and r["used_count"] == 0:
            kbrows.append([(f"🗑 لغو {r['code']}", f"a:codes:cancel:{r['code']}")])
    kbrows.append(ak.BACK_HOME_ROW)
    await edit_or_answer(cb, "\n".join(lines), ak.kb(kbrows))


@router.callback_query(F.data.startswith("a:codes:cancel:"))
@require(PERM_CREATE_PROMO)
async def codes_cancel(cb: CallbackQuery, services: Services, ctx=None):
    code = cb.data.split(":", 3)[3]
    ok = services.codes_repo.cancel(code)
    services.audit_repo.log(ctx.telegram_id, "cancel_promo_code", "code", code,
                            result="success" if ok else "noop")
    await cb.answer("کد لغو شد ✅" if ok else "امکان لغو نیست (استفاده‌شده یا قبلاً لغو شده).")
    cb.data = "a:codes:list:0"
    await codes_list(cb, services)


# ============================ ۶) پیام همگانی (بخش ۱۷) ============================
AUDIENCES = {
    "all": "همه کاربران",
    "active_sub": "دارای اشتراک فعال",
    "no_sub": "بدون اشتراک",
    "expired_sub": "اشتراک منقضی",
    "blocked": "مسدودشده‌ها",
}


@router.callback_query(F.data == "a:broadcast:menu:")
@require(PERM_BROADCAST)
async def broadcast_menu(cb: CallbackQuery, services: Services, ctx=None):
    await edit_or_answer(cb, "📢 پیام همگانی:", ak.menu_of("broadcast"))


@router.callback_query(F.data == "a:broadcast:new:")
@require(PERM_BROADCAST)
async def broadcast_new(cb: CallbackQuery, services: Services, ctx=None):
    kbrows = [[(label, f"a:broadcast:aud:{key}")] for key, label in AUDIENCES.items()]
    kbrows.append(ak.BACK_HOME_ROW)
    await edit_or_answer(cb, "📢 مخاطبان را انتخاب کن (بخش ۱۷.۱):", ak.kb(kbrows))


@router.callback_query(F.data.startswith("a:broadcast:aud:"))
@require(PERM_BROADCAST)
async def broadcast_audience(cb: CallbackQuery, state: FSMContext, services: Services, ctx=None):
    audience = cb.data.split(":")[3]
    await ask_input(state, cb, "📝 متن پیام همگانی را بفرست (HTML ساده مجاز است):",
                    "broadcast:text", audience)


@router.callback_query(F.data.startswith("a:broadcast:confirm:"))
@require(PERM_BROADCAST)
async def broadcast_confirm(cb: CallbackQuery, state: FSMContext, services: Services, ctx=None):
    job_id = int(cb.data.split(":")[3])
    data = await state.get_data()
    if data.get("broadcast_job") != job_id:
        await cb.answer("جریان منقضی شده است. از نو شروع کن.")
        return
    services.broadcast_repo.start(job_id)
    await state.clear()
    await edit_or_answer(cb, "📢 ارسال آغاز شد... پیشرفت را از «گزارش ارسال‌ها» ببین.",
                         ak.with_back([]))
    asyncio.create_task(_run_broadcast(cb.message.bot, services, job_id, ctx.telegram_id))


async def _run_broadcast(bot: Bot, services: Services, job_id: int, admin_id: int) -> None:
    """اجرای ارسال همگانی با محدودیت سرعت و گزارش نتیجه (بخش ۱۷.۳)."""
    import json as _json
    job = services.broadcast_repo.get(job_id)
    spec = _json.loads(job["filter_spec"]) if isinstance(job["filter_spec"], str) else job["filter_spec"]
    user_ids = services.users_repo.filter_ids(spec.get("type", "all"), spec.get("value"))
    sent = failed = 0
    for uid in user_ids:
        if services.broadcast_repo.get(job_id)["status"] != "running":
            break  # توقف کنترل‌شده
        try:
            await bot.send_message(uid, job["message_text"],
                                   parse_mode=job["parse_mode"] or None)
            sent += 1
        except Exception:
            failed += 1
        services.broadcast_repo.update_progress(job_id, sent, failed, sent + failed)
        await asyncio.sleep(0.08)  # رعایت محدودیت سرعت تلگرام
    services.broadcast_repo.finish(job_id, "completed" if sent or failed else "failed")
    try:
        await bot.send_message(
            admin_id,
            f"📢 گزارش ارسال همگانی:\n✅ موفق: {sent}\n❌ ناموفق: {failed}",
        )
    except Exception:
        pass


@router.callback_query(F.data == "a:broadcast:jobs:")
@require(PERM_BROADCAST)
async def broadcast_jobs(cb: CallbackQuery, services: Services, ctx=None):
    rows = services.broadcast_repo.list()
    if not rows:
        await edit_or_answer(cb, "ارسالی ثبت نشده است.", ak.menu_of("broadcast"))
        return
    lines = ["📃 گزارش ارسال‌ها:", ""]
    kbrows = []
    for r in rows:
        status = {"draft": "پیش‌نویس", "preview": "پیش‌نمایش", "running": "در حال ارسال",
                  "completed": "تمام‌شده", "paused": "متوقف", "failed": "ناموفق"}[r["status"]]
        lines.append(
            f"• [{r['id']}] {mention(r['admin_id'], r['username'])} — "
            f"{r['sent']}/{r['total']} موفق — {status} — {ts_fa(r['created_at'])}"
        )
        if r["status"] == "running":
            kbrows.append([(f"⏹ توقف ارسال {r['id']}", f"a:broadcast:stop:{r['id']}")])
    kbrows.append(ak.BACK_HOME_ROW)
    await edit_or_answer(cb, "\n".join(lines), ak.kb(kbrows))


@router.callback_query(F.data.startswith("a:broadcast:stop:"))
@require(PERM_BROADCAST)
async def broadcast_stop(cb: CallbackQuery, services: Services, ctx=None):
    job_id = int(cb.data.split(":")[3])
    services.broadcast_repo.stop(job_id)
    services.audit_repo.log(ctx.telegram_id, "stop_broadcast", "job", str(job_id))
    await cb.answer("توقف درخواست شد.")
    await broadcast_jobs(cb, services)


def _noop():
    pass


# ============================ ۷) تنظیمات ربات (بخش ۱۸) ============================
EDITABLE_TEXTS = {
    "text.welcome": "پیام خوشامد",
    "text.help": "راهنما",
    "text.error": "متن خطای عمومی",
    "text.maintenance": "متن تعمیرات",
    "text.payment": "قالب پیام پرداخت ({order_id} {plan_title} {amount} {duration} {payment_info})",
}


@router.callback_query(F.data == "a:settings:menu:")
@require(PERM_TECH_SETTINGS)
async def settings_menu(cb: CallbackQuery, services: Services, ctx=None):
    await edit_or_answer(cb, "🛠 تنظیمات ربات:", ak.menu_of("settings"))


@router.callback_query(F.data == "a:settings:texts:")
@require(PERM_TECH_SETTINGS)
async def settings_texts(cb: CallbackQuery, services: Services, ctx=None):
    kbrows = [[(label, f"a:settings:edittext:{key}")] for key, label in EDITABLE_TEXTS.items()]
    kbrows.append(ak.BACK_HOME_ROW)
    await edit_or_answer(cb, "📝 کدام پیام ویرایش شود؟", ak.kb(kbrows))


@router.callback_query(F.data.startswith("a:settings:edittext:"))
@require(PERM_TECH_SETTINGS)
async def settings_edittext(cb: CallbackQuery, state: FSMContext, services: Services, ctx=None):
    key = cb.data.split(":", 3)[3]
    current = services.settings.get(key)
    await ask_input(state, cb,
                    f"📝 متن جدید برای «{EDITABLE_TEXTS.get(key, key)}» را بفرست.\n"
                    f"متن فعلی:\n\n{current}", "settings:text", key)


@router.callback_query(F.data == "a:settings:maint:")
@require(PERM_TECH_SETTINGS)
async def settings_maint(cb: CallbackQuery, services: Services, ctx=None):
    on = services.settings.is_maintenance
    toggle = ("✅ خاموش کن", "a:settings:maint:off") if on else ("🔧 روشن کن", "a:settings:maint:on")
    await edit_or_answer(
        cb,
        f"🔧 حالت تعمیرات: {'روشن ⚠️' if on else 'خاموش ✅'}\n"
        "در حالت تعمیرات فقط مدیران می‌توانند پیام بفرستند.",
        ak.kb([[toggle], ak.BACK_HOME_ROW]),
    )


@router.callback_query(F.data.in_({"a:settings:maint:on", "a:settings:maint:off"}))
@require(PERM_TECH_SETTINGS)
async def settings_maint_toggle(cb: CallbackQuery, services: Services, ctx=None):
    turn_on = cb.data.endswith(":on")
    services.settings.set("bot.maintenance", turn_on, ctx.telegram_id)
    services.audit_repo.log(ctx.telegram_id, "toggle_maintenance", "settings",
                            new_value={"on": turn_on})
    await cb.answer("حالت تعمیرات اعمال شد ✅")
    await settings_maint(cb, services)


@router.callback_query(F.data == "a:settings:wait:")
@require(PERM_TECH_SETTINGS)
async def settings_wait(cb: CallbackQuery, services: Services, ctx=None):
    point = services.settings.get("downloads.wait_start_point")
    labels = {"download_start": "از شروع دانلود", "download_end": "از پایان دانلود",
              "send_success": "از پایان موفق ارسال (پیش‌فرض سند)"}
    kbrows = [[(label, f"a:settings:waitset:{key}")]
              for key, label in labels.items()]
    kbrows.append(ak.BACK_HOME_ROW)
    await edit_or_answer(
        cb, f"⏱ نقطه شروع زمان انتظار (بخش ۱۵.۵):\nفعلی: {labels.get(point, point)}",
        ak.kb(kbrows))


@router.callback_query(F.data.startswith("a:settings:waitset:"))
@require(PERM_TECH_SETTINGS)
async def settings_waitset(cb: CallbackQuery, services: Services, ctx=None):
    point = cb.data.split(":")[3]
    services.settings.set("downloads.wait_start_point", point, ctx.telegram_id)
    services.audit_repo.log(ctx.telegram_id, "set_wait_point", "settings", point)
    await cb.answer("اعمال شد ✅")
    await settings_wait(cb, services)


@router.callback_query(F.data == "a:settings:free:")
@require(PERM_TECH_SETTINGS)
async def settings_free(cb: CallbackQuery, state: FSMContext, services: Services, ctx=None):
    cfg = services.settings.free_mode_config
    await ask_input(
        state, cb,
        "📥 پیکربندی حالت رایگان — مقادیر جدید:\n"
        f"<code>concurrent={cfg['concurrent_downloads']}, wait={cfg['wait_seconds']}, "
        f"queue={cfg['queue_limit']}, hourly={cfg['hourly_quota']}, daily={cfg['daily_quota']}</code>",
        "settings:free",
    )


# ============================ ۸) گزارش خطاها (بخش ۲۰.۲) ============================
@router.callback_query(F.data == "a:errors:menu:")
@require(PERM_TECH_SETTINGS)
async def errors_menu(cb: CallbackQuery, services: Services, ctx=None):
    counts = services.errors_repo.counts_by_category()
    lines = ["📋 دسته‌های خطا:", ""]
    for cat, c in counts.items():
        lines.append(f"• {cat}: {c}")
    await edit_or_answer(cb, "\n".join(lines), ak.menu_of("errors"))


@router.callback_query(F.data.startswith("a:errors:list:"))
@require(PERM_TECH_SETTINGS)
async def errors_list(cb: CallbackQuery, services: Services, ctx=None):
    category = cb.data.split(":")[3]
    if category == "all":
        category = None
    rows = services.errors_repo.recent(category=category, limit=15)
    if not rows:
        await edit_or_answer(cb, "خطایی ثبت نشده است ✅", ak.menu_of("errors"))
        return
    lines = ["📋 خطاهای اخیر:", ""]
    for e in rows:
        msg = (e["message"] or "")[:100].replace("\n", " ")
        lines.append(f"• [{e['category']}] {ts_fa(e['created_at'])} — {msg}")
    await edit_or_answer(cb, "\n".join(lines), ak.with_back([]))


# ============================ ۹) مدیریت مدیران (بخش ۱۹) ============================
@router.callback_query(F.data == "a:admins:menu:")
@require(PERM_MANAGE_ADMINS)
async def admins_menu(cb: CallbackQuery, services: Services, ctx=None):
    await edit_or_answer(cb, "🔐 مدیریت مدیران:", ak.menu_of("admins"))


@router.callback_query(F.data == "a:admins:list:")
@require(PERM_MANAGE_ADMINS)
async def admins_list(cb: CallbackQuery, services: Services, ctx=None):
    rows = services.admins_repo.list_admins()
    lines = ["🔐 مدیران فعال:", ""]
    kbrows = []
    for a in rows:
        role = ROLE_LABELS.get(a["role_name"], a["role_name"])
        lines.append(f"• {mention(a['telegram_id'], a['username'])} — {role}")
        kbrows.append([(f"🗑 حذف {a['telegram_id']}", f"a:admins:remove:{a['telegram_id']}")])
    kbrows.append(ak.BACK_HOME_ROW)
    await edit_or_answer(cb, "\n".join(lines), ak.kb(kbrows))


@router.callback_query(F.data.startswith("a:admins:remove:"))
@require(PERM_MANAGE_ADMINS)
async def admins_remove(cb: CallbackQuery, services: Services, ctx=None):
    uid = int(cb.data.split(":")[3])
    if uid == ctx.telegram_id:
        await cb.answer("خودت را نمی‌توانی حذف کنی!")
        return
    ok = services.admins_repo.remove(uid)
    services.audit_repo.log(ctx.telegram_id, "remove_admin", "admin", str(uid),
                            result="success" if ok else "noop")
    await cb.answer("حذف شد." if ok else "تغییری نکرد.")
    await admins_list(cb, services)


@router.callback_query(F.data == "a:admins:add:")
@require(PERM_MANAGE_ADMINS)
async def admins_add(cb: CallbackQuery, state: FSMContext, services: Services, ctx=None):
    roles = services.admins_repo.roles()
    role_list = "، ".join(f"{r['name']}" for r in roles)
    await ask_input(
        state, cb,
        "➕ افزودن مدیر — به شکل:\n<code>شناسه_عددی نقش</code>\n"
        f"نقش‌ها: {role_list}",
        "admins:add",
    )


@router.callback_query(F.data == "a:admins:roles:")
@require(PERM_MANAGE_ADMINS)
async def admins_roles(cb: CallbackQuery, services: Services, ctx=None):
    rows = services.admins_repo.roles()
    lines = ["🎭 نقش‌ها و مجوزها:", ""]
    for r in rows:
        perms = __import__("json").loads(r["permissions"])
        if "*" in perms:
            lines.append(f"• {r['name']} ({ROLE_LABELS.get(r['name'], '')}): همه مجوزها")
        else:
            labels = [PERM_LABELS.get(p, p) for p in perms]
            lines.append(f"• {r['name']}: {', '.join(labels) or 'بدون مجوز'}")
    await edit_or_answer(cb, "\n".join(lines), ak.with_back([]))


# ============================ ۱۰) مرکز سلامت (بخش ۲۰.۱) ============================
@router.callback_query(F.data == "a:health:menu:")
@require(PERM_TECH_SETTINGS)
async def health_menu(cb: CallbackQuery, services: Services, ctx=None):
    counts = services.downloads_repo.status_counts()
    active = services.downloads_repo.global_active()
    errors_1h = services.errors_repo.count_recent(3600)
    import shutil as _sh
    import os as _os
    disk = _sh.disk_usage(_os.getcwd())
    mode = services.settings.sales_mode
    labels = {"free": "رایگان 🎁", "scheduled": "زمان‌بندی‌شده ⏰", "paid": "فروش فعال 💰"}
    last_admin = services.audit_repo.recent(1)
    lines = [
        "🩺 مرکز سلامت و کنترل ربات:",
        "",
        f"💾 پایگاه داده: {'سالم ✅' if services.db.healthcheck() else 'خطا ❌'}",
        f"📥 صف: {counts.get('queued', 0)} در صف | {active} فعال",
        f"⚠️ خطاهای یک ساعت اخیر: {errors_1h}",
        f"💿 فضای دیسک: {disk.free // (1024**3)} گیگ آزاد از {disk.total // (1024**3)} گیگ",
        f"💰 وضعیت فروش: {labels.get(mode, mode)}",
    ]
    if last_admin:
        a = last_admin[0]
        lines.append(f"🕘 آخرین عملیات مدیریتی: {a['action']} — {ts_fa(a['created_at'])}")
    inbox = getattr(services, "inbox_health", lambda: None)()
    if inbox:
        lines.append(f"📸 اینستاگرام: {'متصل ✅' if inbox.get('ready') else 'قطع ❌'}"
                     f" | آخرین اسکن: {ts_fa(inbox.get('last_scan_at'))}"
                     + (f" | خطا: {inbox['last_error'][:40]}" if inbox.get("last_error") else ""))
        lines.append(f"🤖 تلگرام: فعال ✅")
    await edit_or_answer(cb, "\n".join(lines), ak.menu_of("health"))


# ============================ دیسپچر ورودی FSM ============================
@router.message(AdminInput.awaiting, F.text)
async def admin_input(message: Message, state: FSMContext, services: Services):
    """دریافت ورودی متنی مدیر و هدایت به جریان مربوط (بخش ۱۸.۷)."""
    data = await state.get_data()
    flow, arg = data.get("flow", ""), data.get("arg", "")
    text = (message.text or "").strip()
    await state.clear()
    admin_id = message.from_user.id

    async def done(text: str):
        await message.reply(text, reply_markup=ak.MAIN_MENU)

    try:
        # ---- کاربران ----
        if flow == "users:search":
            rows = services.users_repo.search(query=text, limit=5)
            if not rows:
                await done("کاربری یافت نشد.")
                return
            for u in rows:
                sub = services.subs_repo.active_of(u["telegram_id"])
                await message.reply(
                    f"👤 {mention(u['telegram_id'], u['username'])}\n"
                    f"وضعیت: {u['status']} | "
                    f"اشتراک: {'دارد' if sub else 'ندارد'}\n"
                    f"برای مدیریت: /panel → کاربران"
                )
            return
        if flow == "users:extend":
            days = int(text)
            sub = services.subs_repo.active_of(int(arg))
            if not sub:
                await done("اشتراک فعلی برای تمدید وجود ندارد؛ از «هدیه» استفاده کن.")
                return
            services.subs_repo._insert_extension(sub["id"], int(time.time()), days * 86400)
            services.audit_repo.log(admin_id, "extend_subscription", "user", arg,
                                    new_value={"days": days})
            await done(f"✅ {days} روز به اشتراک افزوده شد.")
            return
        if flow == "users:exception":
            kv = parse_kv(text)
            days = kv.pop("days", None)
            ends = int(time.time()) + days * 86400 if days else None
            services.exceptions_repo.add(int(arg), int(time.time()), ends, admin_id, **kv)
            services.audit_repo.log(admin_id, "add_exception", "user", arg, new_value=kv)
            await done("⚡️ استثنای فردی ثبت شد.")
            return
        # ---- پلن‌ها ----
        if flow == "subs:newplan":
            parts = [p.strip() for p in text.split("|")]
            if len(parts) < 7:
                await done("⚠️ همه ۷ فیلد لازم است. نمونه:\ngold | طلایی | 299000 | 30 | 2 | 10 | 15")
                return
            from ...services.plans import PlanConfig
            cfg = PlanConfig(
                price=int(parts[2]), duration_value=int(parts[3]), duration_unit="day",
                concurrent_downloads=int(parts[4]), wait_seconds=int(parts[5]),
                queue_limit=int(parts[6]),
            )
            ok, res = services.plan_service.create_plan(parts[0], parts[1], "", cfg, admin_id)
            if ok:
                plan = services.plans_repo.get_by_name(parts[0])
                v = services.plans_repo.current_version(plan["id"])
                services.plans_repo.publish_version(v["id"], "future")
                services.audit_repo.log(admin_id, "create_plan", "plan", str(plan["id"]))
                await done(f"✅ پلن «{parts[1]}» ساخته و منتشر شد.")
            else:
                await done(f"⚠️ {res}")
            return
        if flow == "subs:price":
            plan = services.plans_repo.get(int(arg))
            v = services.plans_repo.current_version(int(arg))
            cfg = services.plan_service.config_of_version(v)
            cfg.price = int(text)
            ok, res = services.plan_service.draft_new_version(int(arg), cfg, admin_id,
                                                              f"تغییر قیمت به {text}")
            if ok:
                await done(
                    f"✅ نسخه جدید (پیش‌نویس) ساخته شد. برای انتشار: /panel → پلن‌ها → کارت پلن.\n"
                    f"⚠️ مشترکان فعلی: {services.plans_repo.affected_subscribers(int(arg))}"
                )
            else:
                await done(f"⚠️ {res}")
            return
        if flow == "subs:sched":
            import datetime as _dt
            when = int(_dt.datetime.strptime(text, "%Y-%m-%d %H:%M")
                       .replace(tzinfo=_dt.timezone.utc).timestamp())
            services.settings.start_sales(when)
            services.audit_repo.log(admin_id, "schedule_sales", "settings", str(when))
            await done(f"⏰ فروش در {text} (UTC) آغاز می‌شود. یادآوری برای مدیران ارسال می‌شود.")
            return
        # ---- کدها ----
        if flow == "codes:new":
            kv = parse_kv(text)
            plan = services.plans_repo.get(int(arg))
            v = services.plans_repo.current_version(int(arg))
            cfg = services.plan_service.config_of_version(v)
            days = kv.get("days") or max(1, cfg.duration_seconds // 86400)
            count = kv.get("count", 1)
            expires = int(time.time()) + kv["expires_hours"] * 3600 if kv.get("expires_hours") else None
            codes = services.codes_repo.generate(
                plan_id=int(arg), duration_seconds=days * 86400,
                max_uses=kv.get("max_uses"), expires_at=expires,
                created_by=admin_id, count=count, batch_id=f"b{int(time.time())}",
            )
            services.audit_repo.log(admin_id, "create_promo_codes", "plan", arg,
                                    new_value={"count": count, "days": days})
            await done("🎟 کدهای ساخته‌شده:\n" + "\n".join(f"<code>{c}</code>" for c in codes))
            return
        # ---- پیام همگانی ----
        if flow == "broadcast:text":
            user_ids = services.users_repo.filter_ids(arg)
            job_id = services.broadcast_repo.create(admin_id, {"type": arg}, text)
            services.broadcast_repo.set_preview(job_id, len(user_ids))
            await state.update_data(broadcast_job=job_id)
            # پیش‌نمایش + ارسال آزمایشی (بخش ۱۷.۲)
            try:
                await message.bot.send_message(admin_id, f"🧪 پیش‌نمایش پیام همگانی:\n\n{text}")
            except Exception:
                pass
            await message.reply(
                f"📢 پیش‌نمایش بالا برای خودت ارسال شد.\n"
                f"تعداد مخاطبان ({AUDIENCES.get(arg, arg)}): {len(user_ids)}\n"
                "ارسال را تأیید می‌کنی؟",
                reply_markup=ak.confirm_kb(f"a:broadcast:confirm:{job_id}", "a:cancel:"),
            )
            return
        # ---- تنظیمات ----
        if flow == "settings:text":
            old, new = services.settings.set(arg, text, admin_id)
            services.audit_repo.log(admin_id, "edit_setting", "settings", arg,
                                    old_value=str(old)[:100], new_value=str(new)[:100])
            await done("✅ متن ذخیره شد.")
            return
        if flow == "settings:free":
            kv = parse_kv(text)
            cfg = services.settings.free_mode_config
            cfg.update({
                "concurrent_downloads": kv.get("concurrent", cfg["concurrent_downloads"]),
                "wait_seconds": kv.get("wait", cfg["wait_seconds"]),
                "queue_limit": kv.get("queue", cfg["queue_limit"]),
                "hourly_quota": kv.get("hourly", cfg["hourly_quota"]),
                "daily_quota": kv.get("daily", cfg["daily_quota"]),
            })
            services.settings.set("downloads.free_mode", cfg, admin_id)
            services.audit_repo.log(admin_id, "edit_free_mode", "settings", "free_mode")
            await done("✅ پیکربندی حالت رایگان ذخیره شد.")
            return
        if flow == "downloads:capacity":
            kv = parse_kv(text)
            if "dl" in kv:
                services.settings.set("capacity.max_active_downloads", kv["dl"], admin_id)
            if "send" in kv:
                services.settings.set("capacity.max_active_sends", kv["send"], admin_id)
            if "queue" in kv:
                services.settings.set("capacity.max_queue_total", kv["queue"], admin_id)
            services.audit_repo.log(admin_id, "edit_capacity", "settings", "capacity")
            await done("✅ ظرفیت سرور ذخیره شد.")
            return
        # ---- سفارش‌ها: رد با دلیل (بخش ۱۳.۷ سند ۲) ----
        if flow == "orders:reject":
            order_id = arg
            order = services.orders_repo.get(order_id)
            if not order:
                await done("سفارش پیدا نشد.")
                return
            ok, msg = services.order_service.reject(order_id, admin_id, text.strip())
            if not ok:
                await done(f"⚠️ {msg}")
                return
            services.audit_repo.log(admin_id, "reject_order", "order", order_id,
                                    new_value={"reason": text.strip()[:200]})
            await done(f"❌ سفارش <code>{order_id}</code> رد شد.")
            # گام ۱۰ بخش ۱۳: کاربر نتیجه را دریافت می‌کند
            try:
                await message.bot.send_message(
                    order["user_id"],
                    f"❌ پرداخت سفارش <code>{order_id}</code> تأیید نشد.\n"
                    f"دلیل: {text.strip()}\n"
                    "در صورت اشتباه بودن، دوباره سفارش ثبت کن یا با پشتیبانی تماس بگیر.")
            except Exception as e:
                log.warning("اطلاع رد به کاربر %s ناموفق: %s", order["user_id"], e)
            return
        # ---- مدیران ----
        if flow == "admins:add":
            parts = text.split()
            if len(parts) != 2:
                await done("⚠️ شکل صحیح: شناسه_عددی نقش")
                return
            uid, role_name = int(parts[0]), parts[1]
            roles = {r["name"]: r["id"] for r in services.admins_repo.roles()}
            if role_name not in roles:
                await done(f"⚠️ نقش «{role_name}» وجود ندارد.")
                return
            if not services.users_repo.get(uid):
                await done("⚠️ این کاربر هنوز ربات را استارت نکرده است.")
                return
            services.admins_repo.add(uid, roles[role_name], admin_id)
            services.audit_repo.log(admin_id, "add_admin", "admin", str(uid),
                                    new_value={"role": role_name})
            await done(f"✅ مدیر جدید با نقش {ROLE_LABELS.get(role_name, role_name)} افزوده شد.")
            return
        await done("جریان ناشناخته — انصراف شد.")
    except Exception as e:
        log.exception("خطای پردازش ورودی پنل: %s", e)
        await message.reply(f"⚠️ ورودی نامعتبر یا خطا:\n{e}", reply_markup=ak.MAIN_MENU)
