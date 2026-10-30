"""آزمون ساختار روترها — free_text نباید دستورات/ورودی‌های ادمین را ببلعد.

پیش از این فیکس، هندلر همه‌گیر free_text روی «روتر کاربر» بود که اولین
روتر ثبت‌شده بود؛ در نتیجه /panel و ورودی‌های متنی حالت‌های FSM ادمین
(پیام همگانی، جستجوی کاربر و ...) هرگز به هندلرهای ادمین نمی‌رسیدند.
"""
from __future__ import annotations

from app.telegram.handlers import user as uh


def test_free_text_lives_on_fallback_router():
    fallback_names = {h.callback.__name__ for h in uh.fallback.message.handlers}
    main_names = {h.callback.__name__ for h in uh.router.message.handlers}
    assert "free_text" in fallback_names
    assert "free_text" not in main_names
    # دستورات و دکمه‌های کاربر همچنان روی روتر اصلی‌اند
    for needed in ("cmd_start", "cmd_status", "cmd_plans", "cmd_code"):
        assert needed in main_names


def test_dispatcher_registers_fallback_last():
    """fallback باید بعد از روترهای ادمین ثبت شود تا دستورات ادمین اول ببینیم."""
    from tests.conftest import get_shared_dispatcher
    from app.telegram.handlers import admin as ah
    from app.telegram.handlers import admin_more as amh
    from app.telegram.handlers import admin_orders as aoh

    # روترها singleton‌اند؛ از دیسپچر مشترک استفاده می‌کنیم (فقط یک اتصال
    # در هر فرآیند ممکن است — وگرنه RuntimeError: Router is already attached)
    dp = get_shared_dispatcher(None)  # services فقط در میدلور ذخیره می‌شود
    routers = list(dp.sub_routers)
    assert len(routers) == 5
    assert routers[0] is uh.router
    assert routers[1] is ah.router
    assert routers[2] is amh.router
    assert routers[3] is aoh.router
    assert routers[4] is uh.fallback
