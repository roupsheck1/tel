"""آزمون موتور دانلود و صف (بخش ۲۴.۶ سند).

سه محدودیت مستقل + ظرفیت کلی + خطا/تلاش مجدد + بازیابی صف.
"""
from __future__ import annotations

import asyncio
import time

from app.services.download_engine import DownloadEngine

from .conftest import USER1, make_paid_sub


async def _drain_one(engine):
    """برداشتن یک درخواست واجد شرایط و پردازش آن."""
    req = engine._pick_next()
    assert req is not None, "درخواست واجد شرایطی برای پردازش نیست"
    await engine._process(req)


async def test_full_flow_success(engine, services, fake_sender, notifier):
    """جریان کامل: ثبت → پردازش → ارسال → شارژ سهمیه (گام‌های ۹ تا ۱۶ بخش ۱۵.۴)."""
    make_paid_sub(services, USER1)
    ok, rid = await engine.submit(USER1, "https://instagram.com/reel/xyz/", "xyz")
    assert ok and isinstance(rid, int)
    assert services.downloads_repo.get(rid)["status"] == "queued"
    await _drain_one(engine)
    row = services.downloads_repo.get(rid)
    assert row["status"] == "completed"
    assert row["quota_charged"] == 1
    assert len(fake_sender.sent) == 1
    assert any("✅" in t for _, t in notifier.messages)


async def test_queue_limit(engine, services):
    """سقف درخواست در صف هر کاربر (بخش ۱۵.۳)."""
    make_paid_sub(services, USER1)
    # پلن پایه queue_limit=10 دارد؛ موقتاً کمش می‌کنیم
    services.settings.set("capacity.max_queue_total", 100)
    sub = services.subs_repo.active_of(USER1)
    version = services.plans_repo.version(sub["plan_version_id"])
    import json
    from app.services.plans import PlanConfig
    cfg = PlanConfig.from_dict(json.loads(version["config"]))
    cfg.queue_limit = 1
    services.db.execute("UPDATE plan_versions SET config=? WHERE id=?",
                        (json.dumps(cfg.to_dict()), version["id"]))
    services.settings.reset_cache()
    ok1, _ = await engine.submit(USER1, "https://instagram.com/reel/a/", "a")
    ok2, msg2 = await engine.submit(USER1, "https://instagram.com/reel/b/", "b")
    assert ok1
    assert ok2 is False
    assert "صف" in msg2


async def test_concurrency_limit(engine, services):
    """محدودیت دانلود هم‌زمان (اولین محدودیت بخش ۱۵.۳)."""
    make_paid_sub(services, USER1)
    ok, rid = await engine.submit(USER1, "https://instagram.com/reel/c/", "c")
    assert ok
    # شبیه‌سازی یک عملیات فعال
    services.downloads_repo.set_status(rid, "downloading", started_at=int(time.time()))
    assert engine._ineligibility_reason(USER1) == "concurrency"
    assert engine._pick_next() is None
    # پایان عملیات → واجد شرایط مجدد
    services.downloads_repo.set_status(rid, "queued")
    assert engine._ineligibility_reason(USER1) is None


async def test_wait_cooldown(engine, services):
    """زمان انتظار بین دانلودها (دومین محدودیت بخش ۱۵.۳ + سیاست ۱۵.۵)."""
    make_paid_sub(services, USER1)
    ok, rid = await engine.submit(USER1, "https://instagram.com/reel/d/", "d")
    assert ok
    await _drain_one(engine)
    row = services.downloads_repo.get(rid)
    assert row["cooldown_at"] is not None  # پس از ارسال موفق، کوچک‌داون فعال شد
    assert engine._ineligibility_reason(USER1) == "cooldown"
    # پس از گذشت زمان انتظار → واجد شرایط
    services.db.execute(
        "UPDATE download_requests SET cooldown_at=? WHERE id=?",
        (int(time.time()) - 1, rid))
    assert engine._ineligibility_reason(USER1) is None


async def test_hourly_quota(engine, services):
    """سهمیه ساعتی (سومین محدودیت بخش ۱۵.۳)."""
    make_paid_sub(services, USER1)
    # محدود کردن پلن به ۱ دانلود در ساعت
    sub = services.subs_repo.active_of(USER1)
    version = services.plans_repo.version(sub["plan_version_id"])
    import json
    from app.services.plans import PlanConfig
    cfg = PlanConfig.from_dict(json.loads(version["config"]))
    cfg.hourly_quota = 1
    cfg.wait_seconds = 0
    services.db.execute("UPDATE plan_versions SET config=? WHERE id=?",
                        (json.dumps(cfg.to_dict()), version["id"]))
    services.settings.reset_cache()
    ok, rid = await engine.submit(USER1, "https://instagram.com/reel/e/", "e")
    assert ok
    await _drain_one(engine)
    assert engine._ineligibility_reason(USER1) == "hourly_quota"


async def test_download_failure_retry_then_failed(engine, services, fake_downloader, notifier):
    """خطای دانلود: تلاش مجدد همان درخواست است و در نهایت failed (بخش ۱۵.۱۱)."""
    make_paid_sub(services, USER1)
    services.settings.set("downloads.max_retries", 1)
    services.settings.set("downloads.wait_start_point", "download_end")
    fake_downloader.fail = True
    ok, rid = await engine.submit(USER1, "https://instagram.com/reel/f/", "f")
    assert ok
    # تلاش اول → صف مجدد (attempts=1 <= max_retries=1 → امتحان مجدد)
    req = engine._pick_next()
    await engine._process(req)
    row = services.downloads_repo.get(rid)
    assert row["status"] == "queued" and row["attempts"] == 1
    # تلاش دوم → شکست نهایی
    req2 = engine._pick_next()
    await engine._process(req2)
    row2 = services.downloads_repo.get(rid)
    assert row2["status"] == "failed"
    assert row2["quota_charged"] == 0  # درخواست ناموفق سهمیه مصرف نکرد (۱۵.۱۲)
    assert any("❌" in t for _, t in notifier.messages)


async def test_send_failure_separate_stage(engine, services, fake_sender):
    """شکست ارسال از شکست دانلود تفکیک می‌شود (بخش ۱۵.۱۲)."""
    make_paid_sub(services, USER1)
    services.settings.set("downloads.max_retries", 0)
    fake_sender.fail = True
    ok, rid = await engine.submit(USER1, "https://instagram.com/reel/g/", "g")
    assert ok
    await _drain_one(engine)
    row = services.downloads_repo.get(rid)
    assert row["status"] == "failed"
    assert row["error_stage"] == "send"
    assert "ارسال" in row["last_error"]


async def test_queue_expiry(engine, services):
    """انقضای درخواست معلق در صف (بخش ۱۵.۱۱)."""
    make_paid_sub(services, USER1)
    services.settings.set("downloads.processing_window", 10)
    ok, rid = await engine.submit(USER1, "https://instagram.com/reel/h/", "h")
    assert ok
    services.db.execute("UPDATE download_requests SET created_at=? WHERE id=?",
                        (int(time.time()) - 100, rid))
    assert engine._pick_next() is None
    assert services.downloads_repo.get(rid)["status"] == "expired"


async def test_recovery_after_restart(engine, services):
    """بازیابی صف پس از راه‌اندازی مجدد (بخش ۱۵.۱۴)."""
    make_paid_sub(services, USER1)
    ok, rid = await engine.submit(USER1, "https://instagram.com/reel/i/", "i")
    assert ok
    services.downloads_repo.set_status(rid, "downloading")
    report = services.downloads_repo.recover_on_startup()
    assert report["reset_to_queue"] == 1
    assert services.downloads_repo.get(rid)["status"] == "queued"


async def test_no_subscription_rejected_in_paid_mode(engine, services):
    """در حالت پولی، کاربر بدون اشتراک ثبت درخواست نمی‌تواند (گام ۳)."""
    services.user_service.register(USER1, "u", None)
    ok, msg = await engine.submit(USER1, "https://instagram.com/reel/j/", "j")
    assert ok is False
    assert "اشتراک" in msg


async def test_free_mode_allows_without_subscription(engine, services, fake_sender):
    """حالت رایگان: بدون اشتراک با محدودیت‌های حالت رایگان (بخش ۱۱.۱)."""
    services.settings.set("sales.mode", "free")
    services.user_service.register(USER1, "u", None)
    ok, rid = await engine.submit(USER1, "https://instagram.com/reel/k/", "k")
    assert ok
    await _drain_one(engine)
    assert services.downloads_repo.get(rid)["status"] == "completed"


async def test_global_capacity(engine, services):
    """ظرفیت کلی سرور (بخش ۱۵.۸) — مستقل از محدودیت هر کاربر (استاندارد ۲۰)."""
    services.settings.set("capacity.max_active_downloads", 0)  # ظرفیت صفر
    make_paid_sub(services, USER1)
    ok, _ = await engine.submit(USER1, "https://instagram.com/reel/l/", "l")
    assert ok  # ثبت در صف ممکن است
    assert engine._pick_next() is None  # اما پردازش انتخاب نمی‌شود
    services.settings.set("capacity.max_active_downloads", 3)
    assert engine._pick_next() is not None


async def test_scheduler_loop_no_duplicate_processing(engine, services, fake_downloader,
                                                      fake_sender):
    """رگرسیون فریز حلقه رویداد: زمان‌بند واقعی نباید همان درخواست را دوباره بردارد.

    پیش از فیکس، حلقه زمان‌بند بعد از create_task بدون هیچ نقطه await دوباره
    همان ردیف queued را برمی‌داشت، بی‌نهایت تسک تکراری می‌ساخت و حلقه رویداد
    (و کل ربات) فریز می‌شد. اکنون _inflight از برداشتن تکراری جلوگیری می‌کند.
    """
    make_paid_sub(services, USER1)
    ok, rid = await engine.submit(USER1, "https://instagram.com/reel/dup1/", "dup1")
    assert ok
    await engine.start()
    try:
        async def _wait_completed() -> None:
            while services.downloads_repo.get(rid)["status"] != "completed":
                await asyncio.sleep(0.05)

        await asyncio.wait_for(_wait_completed(), timeout=15)
        # دقیقاً یک دانلود — بدون تکرار
        assert fake_downloader.calls.count("dup1") == 1, fake_downloader.calls
        assert len(fake_sender.sent) == 1
        # هیچ درخواست معلقی در مجموعه inflight نمانده است
        assert not engine._inflight
        assert not engine._workers
    finally:
        await engine.stop()
