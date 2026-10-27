"""نقطه ورود سامانه — مونتاژ نهایی و راه‌اندازی همه اجزا (بخش ۲۷ سند).

اجرا:  python -m app.main
"""
from __future__ import annotations

import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from .config import get_settings
from .database import Database
from .instagram.client import InstagramClient
from .instagram.content import InstagramContentDownloader
from .instagram.inbox import InstagramInbox
from .logging_config import setup_logging
from .schedulers import start_all
from .services import Services, build_engine, build_services
from .telegram.delivery import TelegramSender
from .telegram.handlers import admin as admin_handlers
from .telegram.handlers import admin_more as admin_more_handlers
from .telegram.handlers import user as user_handlers
from .telegram.middlewares import UserMiddleware

log = logging.getLogger("main")


def build_dispatcher(services: Services) -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())
    dp.message.middleware(UserMiddleware(services))
    dp.callback_query.middleware(UserMiddleware(services))
    # ترتیب مهم است: روتر کاربر (بدون هندلر همه‌گیر)، سپس ادمین‌ها،
    # و در انتها روتر fallback که free_text همه‌گیر دارد — تا دستورات ادمین
    # (/panel و ورودی‌های FSM ادمین) بلعیده نشوند.
    dp.include_router(user_handlers.router)
    dp.include_router(admin_handlers.router)
    dp.include_router(admin_more_handlers.router)
    dp.include_router(user_handlers.fallback)
    return dp


async def run() -> None:
    cfg = get_settings()
    cfg.ensure_dirs()
    setup_logging(cfg.logs_dir, cfg.log_level)
    log.info("راه‌اندازی سامانه...")

    # خطاهای راه‌اندازی (بدون توقف کل سامانه — بخش ۱.۱)
    startup_errors = cfg.validate_runtime()
    for err in startup_errors:
        log.error("خطای تنظیمات: %s", err)

    # --- پایگاه داده و سرویس‌ها ---
    db = Database(cfg.db_file)
    services = build_services(db, cfg.admin_ids)
    # تشخیص راه دور: مسیر مطلق دیتابیس + وضعیت نشست + تعداد داده‌های موجود
    # (اگر بعد از هر دیپلوی این شمارنده‌ها صفر شوند، یعنی ذخیره‌سازی پایدار
    # (ولوم) به سرویس وصل نیست و همه اتصال‌ها با هر دیپلوی از بین می‌روند.)
    log.info(
        "پایگاه داده آماده شد (مهاجرت‌ها اعمال شدند) | مسیر: %s | نشست اینستاگرام: %s "
        "| کاربران: %s | اتصال‌های فعال: %s",
        cfg.db_file.resolve(),
        "موجود" if cfg.session_file.exists() else "ناموجود (ورود تازه لازم است)",
        services.users_repo.count(),
        services.links_repo.stats()["linked"],
    )

    # --- تلگرام ---
    bot = Bot(
        token=cfg.telegram_bot_token or "1:test",
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        **({"base_url": cfg.telegram_base_url} if cfg.telegram_base_url else {}),
    )
    use_local_api = bool(cfg.telegram_base_url)
    sender = TelegramSender(bot, use_local_api=use_local_api)

    # --- اینستاگرام ---
    ig_client = InstagramClient(
        cfg.instagram_username, cfg.instagram_password, cfg.session_file,
        proxy=cfg.proxy or None, two_factor_code=cfg.instagram_2fa_code or None,
    )
    downloader = InstagramContentDownloader(ig_client, proxy=cfg.proxy or None)

    # --- موتور دانلود ---
    async def notify(user_id: int, text: str) -> None:
        await bot.send_message(user_id, text)

    engine = build_engine(services, downloader, sender, notify, cfg.temp_dir)

    # --- پایش دایرکت (نخ جداگانه) ---
    inbox = InstagramInbox(
        ig_client, services.links_repo, engine,
        loop_holder=asyncio.get_running_loop(),
        poll_min=cfg.ig_poll_min, poll_max=cfg.ig_poll_max,
    )
    # سلامت اینستاگرام برای مرکز سلامت (بخش ۲۰.۱)
    services.inbox_health = inbox.health  # type: ignore[attr-defined]

    dp = build_dispatcher(services)

    # --- ورود به اینستاگرام (در صورت نبود اطلاعات، ادامه با هشدار) ---
    if not startup_errors:
        try:
            await asyncio.to_thread(ig_client.login)
        except Exception as e:
            log.error("ورود به اینستاگرام ناموفق بود: %s — پایش دایرکت غیرفعال می‌ماند.", e)
    else:
        log.warning("بدون اطلاعات کامل، اتصال اینستاگرام انجام نشد.")

    # --- راه‌اندازی اجزا ---
    await engine.start()
    inbox.start_background()
    tasks = start_all(bot, services)

    log.info("ربات راه‌اندازی شد. polling آغاز می‌شود...")
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        log.info("توقف سامانه...")
        inbox.stop()
        await engine.stop()
        for t in tasks:
            t.cancel()
        await bot.session.close()
        db.close()


def main() -> None:
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        asyncio.run(run())
    except (KeyboardInterrupt, SystemExit):
        logging.getLogger("main").info("خروج دستی.")


if __name__ == "__main__":
    main()
