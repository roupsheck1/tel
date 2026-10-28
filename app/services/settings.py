"""تنظیمات عملیاتی و تجاری قابل تغییر از پنل (بخش ۱۸ سند).

تمام مقادیر در پایگاه داده ذخیره می‌شوند و پیش‌فرض‌های معقول دارند؛
کسب‌وکار بدون پیکربندی اولیه هم با مقادیر پیش‌فرض امن کار می‌کند.
"""
from __future__ import annotations

import time

from ..database.repositories import SettingsRepo

NOW = lambda: int(time.time())  # noqa: E731

# حالت‌های تجاری (بخش ۱۱)
SALES_FREE = "free"            # رایگان نامحدود از نظر زمانی
SALES_SCHEDULED = "scheduled"  # شروع زمان‌بندی‌شده فروش
SALES_PAID = "paid"            # فروش فعال

# نقطه شروع زمان انتظار (بخش ۱۵.۵)
WAIT_FROM_DOWNLOAD_START = "download_start"
WAIT_FROM_DOWNLOAD_END = "download_end"
WAIT_FROM_SEND_SUCCESS = "send_success"  # پیش‌فرض پیشنهادی سند

DEFAULTS: dict[str, object] = {
    # --- پیام‌ها (۱۸.۱) ---
    "text.welcome": (
        "سلام {name} عزیز 👋\n\n"
        "به ربات دریافت محتوای اینستاگرام خوش آمدی!\n\n"
        "🔹 برای شروع، حساب اینستاگرامت را وصل کن:\n"
        "از اینستاگرام به پیج ربات @{ig_user} یک پیام بفرست تا کد اتصال بگیری، "
        "سپس همان کد را همین‌جا ارسال کن.\n\n"
        "🔹 بعد از اتصال، هر پست یا ریلزی را در دایرکت اینستاگرام برای پیج ربات "
        "بفرست تا فایلش همین‌جا برایت بیاید."
    ),
    "text.help": (
        "راهنمای استفاده:\n"
        "1️⃣ /connect — راهنمای اتصال حساب اینستاگرام\n"
        "2️⃣ /status — وضعیت اشتراک و اتصال\n"
        "3️⃣ /plans — مشاهده پلن‌ها و خرید\n"
        "4️⃣ /myorders — سفارش‌های من\n"
        "5️⃣ /code — فعال‌سازی کد اشتراک\n\n"
        "دریافت محتوا فقط از طریق دایرکت اینستاگرام انجام می‌شود؛ "
        "پست یا ریلز را در اینستاگرام برای پیج ربات بفرست."
    ),
    "text.error": "خطایی رخ داد. لطفاً کمی بعد دوباره تلاش کن.",
    "text.maintenance": "ربات در حال تعمیر است ⚠️ لطفاً بعداً مراجعه کن.",
    "text.payment": (
        "🧾 سفارش {order_id}\n"
        "پلن: {plan_title}\n"
        "مبلغ: {amount:,} تومان\n"
        "مدت: {duration}\n\n"
        "💳 پرداخت به:\n{payment_info}\n\n"
        "پس از پرداخت، دکمه «پرداخت کردم» را بزن و رسید را برای مدیران بفرست."
    ),

    # --- وضعیت تجاری (بخش ۱۱) ---
    "sales.mode": SALES_FREE,
    "sales.scheduled_start": None,     # timestamp
    "sales.alert_minutes_before": 60,  # هشدار نزدیک‌شدن شروع فروش
    "sales.alert_on_start": True,

    # --- موتور دانلود (۱۸.۳ و بخش ۱۵) ---
    "downloads.wait_start_point": WAIT_FROM_SEND_SUCCESS,
    "downloads.max_retries": 2,
    "downloads.processing_window": 900,        # انقضای درخواست معلق در صف (ثانیه)
    "downloads.cleanup_retention": 300,        # حذف فایل موقت پس از ارسال (ثانیه)
    # پیکربندی حالت رایگان (وقتی اشتراک لازم نیست)
    "downloads.free_mode": {
        "concurrent_downloads": 1,
        "wait_seconds": 60,
        "queue_limit": 3,
        "hourly_quota": 5,
        "daily_quota": 30,
        "total_quota": None,
        "max_file_mb": 50,
        "priority": 0,
    },

    # --- ظرفیت کلی سرور (بخش ۱۵.۸) ---
    "capacity.max_active_downloads": 3,
    "capacity.max_active_sends": 5,
    "capacity.max_queue_total": 100,
    "capacity.max_temp_gb": 10,

    # --- اطلاع‌رسانی (۱۸.۴) ---
    "notify.expiry_warn_days": 3,
    "notify.admin_errors": True,

    # --- اعلان‌های قابل تنظیم (§۳۱ سند ۲: مدیر بتواند هر اعلان را خاموش/روشن کند) ---
    "notify.on_queue": True,          # ورود درخواست به صف
    "notify.on_success": True,        # تحویل موفق
    "notify.on_failure": True,        # شکست عملیات
    "notify.on_order": True,          # دریافت سفارش/رسید جدید (به مدیران)
    "notify.on_expiry": True,         # یادآوری انقضای اشتراک

    # --- کلیدهای قابلیت (§۵۳ سند ۲: هر قابلیت مهم مستقل قابل خاموش‌کردن) ---
    "feature.story_download": True,     # دریافت استوری
    "feature.large_files": True,        # فایل حجیم (> ۵۰MB با فشرده‌سازی)
    "feature.promo_codes": True,        # کدهای اشتراک
    "feature.manual_payment": True,     # پرداخت دستی
    "feature.broadcast": True,          # پیام همگانی
    "feature.trial": True,              # پلن آزمایشی

    # --- حالت نگهداری (§۵۴ سند ۲) ---
    # new_requests: توقف ثبت درخواست جدید
    # continue_queue: ادامه/توقف درخواست‌های در جریان
    "maintenance.block_new_requests": True,
    "maintenance.continue_queue": True,

    # --- آزمایش (بخش ۱۰.۴) ---
    "trial.limit_scope": "per_plan",  # per_plan | global
}


class SettingsService:
    """سرویس تنظیمات با مقادیر پیش‌فرض و کش سبک."""

    def __init__(self, repo: SettingsRepo):
        self.repo = repo
        self._cache: dict = {}

    def get(self, key: str, default=None):
        if key in self._cache:
            return self._cache[key]
        value = self.repo.get(key, DEFAULTS.get(key, default))
        self._cache[key] = value
        return value

    def set(self, key: str, value, updated_by: int | None = None) -> tuple[object, object]:
        """تغییر تنظیمات با نگهداری مقدار قبلی (کنترل تغییرات — ۱۸.۷)."""
        old = self.get(key)
        self.repo.set(key, value, updated_by)
        self._cache[key] = value
        return old, value

    def reset_cache(self) -> None:
        self._cache.clear()

    def seed_defaults(self) -> None:
        """درج پیش‌فرض‌ها در صورت نبود (idempotent)."""
        for key, value in DEFAULTS.items():
            if self.repo.get(key) is None:
                self.repo.set(key, value)

    # --- کمکی‌های تجاری ---
    @property
    def sales_mode(self) -> str:
        mode = self.get("sales.mode")
        if mode == SALES_SCHEDULED:
            start = self.get("sales.scheduled_start")
            if start and NOW() >= int(start):
                return SALES_PAID  # زمان‌بندی رسیده → فروش فعال
            return SALES_SCHEDULED
        return mode

    @property
    def free_mode_config(self) -> dict:
        return dict(self.get("downloads.free_mode"))

    @property
    def is_maintenance(self) -> bool:
        return bool(self.get("bot.maintenance", False))

    def start_sales(self, when: int | None = None) -> str:
        """شروع فروش: دستی یا زمان‌بندی‌شده (بخش ۱۱.۲ و ۱۱.۳)."""
        if when is None:
            self.set("sales.mode", SALES_PAID)
            return SALES_PAID
        self.set("sales.scheduled_start", int(when))
        self.set("sales.mode", SALES_SCHEDULED)
        return SALES_SCHEDULED

    def stop_sales(self) -> None:
        """توقف فروش؛ اشتراک‌های فعال مستقل از وضعیت فروش می‌مانند (۱۱.۵)."""
        self.set("sales.mode", SALES_PAID)
        self.set("sales.selling_stopped", True)

    def back_to_free(self) -> None:
        self.set("sales.mode", SALES_FREE)
        self.set("sales.selling_stopped", False)
