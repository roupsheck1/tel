"""تنظیمات محیطی سامانه.

طبق بخش ۴.۳ سند مشخصات:
- اطلاعات محرمانه (توکن، رمز) فقط از متغیرهای محیطی خوانده می‌شوند.
- تنظیمات تجاری و عملیاتی قابل تغییر از پنل، در پایگاه داده نگهداری می‌شوند
  (مدول services.settings).
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """تنظیمات محیطی غیرقابل تغییر از پنل."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- تلگرام ---
    telegram_bot_token: str = Field(default="", alias="TELEGRAM_BOT_TOKEN")
    telegram_bot_username: str = Field(default="", alias="TELEGRAM_BOT_USERNAME")
    telegram_base_url: str | None = Field(default=None, alias="TELEGRAM_BASE_URL")
    telegram_api_id: int | None = Field(default=None, alias="TELEGRAM_API_ID")
    telegram_api_hash: str | None = Field(default=None, alias="TELEGRAM_API_HASH")

    # --- اینستاگرام ---
    instagram_username: str = Field(default="", alias="INSTAGRAM_USERNAME")
    instagram_password: str = Field(default="", alias="INSTAGRAM_PASSWORD")
    instagram_2fa_code: str = Field(default="", alias="INSTAGRAM_2FA_CODE")

    # --- مدیران اولیه ---
    admin_ids: list[int] = Field(default_factory=list, alias="ADMIN_IDS")

    # --- شبکه ---
    proxy: str = Field(default="", alias="PROXY")

    # --- مسیرها ---
    database_path: str = Field(default="storage/bot.db", alias="DATABASE_PATH")
    storage_dir: str = Field(default="storage", alias="STORAGE_DIR")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    # --- پایش دایرکت ---
    ig_poll_min: int = Field(default=30, alias="IG_POLL_MIN")
    ig_poll_max: int = Field(default=60, alias="IG_POLL_MAX")

    @field_validator("admin_ids", mode="before")
    @classmethod
    def _parse_admin_ids(cls, v):
        """پذیرش هر دو شکل: «111,222» (رشته) و «111» (پارس JSON شده به int)."""
        if v is None or v == "":
            return []
        if isinstance(v, str):
            return [int(x) for x in v.replace(" ", "").split(",") if x]
        if isinstance(v, int):
            return [v]
        if isinstance(v, list):
            return [int(x) for x in v if str(x).strip()]
        return v

    @property
    def db_file(self) -> Path:
        """مسیر فایل پایگاه داده؛ مسیر مطلق (مثل ولوم Lucity) پذیرفته می‌شود."""
        p = Path(self.database_path)
        return p if p.is_absolute() else Path(self.storage_dir) / p.name

    @property
    def temp_dir(self) -> Path:
        return Path(self.storage_dir) / "downloads"

    @property
    def session_file(self) -> Path:
        return Path(self.storage_dir) / "ig_session.json"

    @property
    def logs_dir(self) -> Path:
        return Path(self.storage_dir) / "logs"

    def ensure_dirs(self) -> None:
        for p in (self.db_file.parent, self.temp_dir, self.logs_dir):
            p.mkdir(parents=True, exist_ok=True)

    def validate_runtime(self) -> list[str]:
        """خطاهای راه‌اندازی را برمی‌گرداند (بدون توقف بقیه سامانه)."""
        errors = []
        if not self.telegram_bot_token or "PUT-YOUR" in self.telegram_bot_token:
            errors.append("TELEGRAM_BOT_TOKEN تنظیم نشده است.")
        if not self.instagram_username or not self.instagram_password:
            errors.append("اطلاعات اکانت اینستاگرام (INSTAGRAM_USERNAME/INSTAGRAM_PASSWORD) تنظیم نشده است.")
        if not self.admin_ids:
            errors.append("حداقل یک شناسه مدیر در ADMIN_IDS لازم است.")
        if self.ig_poll_min < 5:
            errors.append("IG_POLL_MIN نباید کمتر از ۵ ثانیه باشد (پایداری اکانت).")
        return errors


@lru_cache
def get_settings() -> Settings:
    return Settings()
