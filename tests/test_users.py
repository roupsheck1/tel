"""آزمون کاربران و اتصال حساب‌ها (بخش ۲۴.۱ سند)."""
from __future__ import annotations

import time

from app.database.repositories import LinksRepo

from .conftest import USER1, USER2


# ---------- ثبت‌نام ----------
def test_register_new_user(services):
    assert services.user_service.register(USER1, "ali", "علی") is True
    u = services.users_repo.get(USER1)
    assert u["username"] == "ali"
    assert u["status"] == "active"


def test_duplicate_register_preserves_date(services):
    services.user_service.register(USER1, "ali", "علی")
    first = services.users_repo.get(USER1)["registered_at"]
    time.sleep(1.05)  # عبور از ثانیه برای اطمینان
    assert services.user_service.register(USER1, "ali2", "علی جدید") is False
    second = services.users_repo.get(USER1)
    assert second["registered_at"] == first  # تاریخ ثبت‌نام اولیه حفظ می‌شود
    assert second["username"] == "ali2"      # ولی نام کاربری به‌روز می‌شود


def test_block_unblock(services):
    services.user_service.register(USER1, "ali", None)
    assert services.users_repo.set_blocked(USER1, True, "test") is True
    assert services.users_repo.is_blocked(USER1) is True
    # کاربر مسدود به سرویس دسترسی ندارد (بخش ۶)
    access = services.user_service.check_access(USER1)
    assert access.allowed is False
    assert "مسدود" in access.reason
    assert services.users_repo.set_blocked(USER1, False) is True
    assert services.users_repo.is_blocked(USER1) is False


# ---------- اتصال اینستاگرام (بخش ۵) ----------
def test_link_code_flow(services):
    services.user_service.register(USER1, "ali", None)
    code = services.links_repo.create_code("alice_ig", ttl_seconds=600)
    assert len(code) == 6 and code.isdigit()
    ok, ig = services.links_repo.link(code, USER1)
    assert ok and ig == "alice_ig"
    link = services.links_repo.get_active(USER1)
    assert link["ig_username"] == "alice_ig"
    # کد یک‌بارمصرف است
    ok2, _ = services.links_repo.link(code, USER2)
    assert ok2 is False


def test_code_regenerated_while_valid(services):
    c1 = services.links_repo.create_code("bob_ig", ttl_seconds=600)
    c2 = services.links_repo.create_code("bob_ig", ttl_seconds=600)
    assert c1 == c2  # کد معتبر قبلی برگردانده می‌شود


def test_expired_code_rejected(services):
    code = services.links_repo.create_code("carol_ig", ttl_seconds=600)
    services.db.execute("UPDATE link_codes SET expires_at=? WHERE code=?", (int(time.time()) - 1, code))
    ok, msg = services.links_repo.link(code, USER1)
    assert ok is False
    assert "منقضی" in msg


def test_ig_account_one_to_one(services):
    services.user_service.register(USER1, "a", None)
    services.user_service.register(USER2, "b", None)
    code1 = services.links_repo.create_code("dave_ig")
    ok, _ = services.links_repo.link(code1, USER1)
    assert ok
    # همان یوزرنیم نمی‌تواند به تلگرام دیگری وصل شود
    code2 = services.links_repo.create_code("dave_ig")
    ok2, msg2 = services.links_repo.link(code2, USER2)
    assert ok2 is False
    # یک تلگرام نمی‌تواند دو اینستاگرام داشته باشد
    code3 = services.links_repo.create_code("erin_ig")
    ok3, msg3 = services.links_repo.link(code3, USER1)
    assert ok3 is False


def test_unlink_preserves_subscription(services):
    from .conftest import make_paid_sub
    services.user_service.register(USER1, "a", None)
    make_paid_sub(services, USER1)
    code = services.links_repo.create_code("frank_ig")
    services.links_repo.link(code, USER1)
    ig = services.links_repo.unlink_by_admin(USER1)
    assert ig == "frank_ig"
    # اشتراک پس از قطع اتصال هم باید موجود باشد (بخش ۵.۴)
    assert services.subs_repo.active_of(USER1) is not None
    assert services.links_repo.get_active(USER1) is None


# ---------- ارقام فارسی و مصونیت کد (رفع باگ‌های اتصال) ----------
def test_link_with_persian_digits(services):
    """کاربر کد را با صفحه‌کلید فارسی (۴۸۳۹۲۰) می‌فرستد → باید وصل شود."""
    services.user_service.register(USER1, "ali", None)
    code = services.links_repo.create_code("alice_ig", ttl_seconds=600)
    fa = code.translate(str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹"))
    ok, ig = services.links_repo.link(fa, USER1)
    assert ok and ig == "alice_ig"
    assert services.links_repo.get_active(USER1)["ig_username"] == "alice_ig"


def test_link_with_arabic_digits_and_whitespace(services):
    services.user_service.register(USER1, "ali", None)
    code = services.links_repo.create_code("ahmad_ig", ttl_seconds=600)
    ar = code.translate(str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩"))
    ok, ig = services.links_repo.link(f" {ar} ", USER1)
    assert ok and ig == "ahmad_ig"


def test_failed_link_does_not_burn_code(services):
    """تلاش ناموفق نباید کدِ معتبر را مصرف کند (قبلاً کد می‌سوخت)."""
    services.user_service.register(USER1, "a", None)
    services.user_service.register(USER2, "b", None)
    c1 = services.links_repo.create_code("dave_ig")
    assert services.links_repo.link(c1, USER1)[0]
    c2 = services.links_repo.create_code("dave_ig")  # کد تازه (c1 استفاده شده)
    # اکانت به USER1 وصل است → تلاش USER2 ناموفق؛ کد نباید بسوزد
    ok, _msg = services.links_repo.link(c2, USER2)
    assert ok is False
    services.links_repo.unlink_by_admin(USER1)
    ok2, ig = services.links_repo.link(c2, USER2)
    assert ok2 and ig == "dave_ig"


def test_relink_same_pair_is_idempotent(services):
    """اتصال دوباره همان جفت (کاربر/اکانت یکسان) → موفقیت بدون خطا."""
    services.user_service.register(USER1, "a", None)
    assert services.links_repo.create_code("erin_ig") and \
        services.links_repo.link(services.links_repo.create_code("erin_ig"), USER1)[0]
    # یک کد معتبر دیگر برای همان اکانت (مثلاً از پیام در انتظار پیش از اتصال)
    code2 = "000999"
    services.db.execute(
        "INSERT INTO link_codes (code, ig_username, expires_at, created_at) VALUES (?,?,?,?)",
        (code2, "erin_ig", int(time.time()) + 600, int(time.time())),
    )
    ok, ig = services.links_repo.link(code2, USER1)
    assert ok and ig == "erin_ig"


def test_normalize_digits_util():
    from app.utils import normalize_digits
    assert normalize_digits("۴۸۳۹۲۰") == "483920"
    assert normalize_digits("٤٨٣٩٢٠") == "483920"
    assert normalize_digits("12۳۴") == "1234"
    assert normalize_digits("کد ۱۲۳") == "کد 123"
    assert normalize_digits("") == ""
    assert normalize_digits("no digits") == "no digits"


def test_db_file_absolute_and_relative():
    from pathlib import Path
    from app.config import Settings
    s = Settings(DATABASE_PATH="/mnt/vol/bot.db", STORAGE_DIR="storage", _env_file=None)
    assert s.db_file == Path("/mnt/vol/bot.db")
    s2 = Settings(DATABASE_PATH="bot.db", STORAGE_DIR="storage", _env_file=None)
    assert s2.db_file == Path("storage/bot.db")
