"""ساختار پایگاه داده — مهاجرت‌های نسخه‌بندی‌شده (بخش ۲۱ و ۲۱.۴ سند).

قرارداد زمانی: تمام فیلدهای منطقی/زمان‌بندی به‌صورت timestamp یونیکس (INTEGER)
ذخیره می‌شوند؛ فیلدهای حسابرسی (audit) به‌صورت متن ISO.
"""
from __future__ import annotations

MIGRATIONS: list[tuple[int, list[str]]] = [
    (1, [
        # ---------- کاربران و اتصال حساب‌ها (بخش ۵ و ۶) ----------
        """CREATE TABLE users (
            telegram_id INTEGER PRIMARY KEY,
            username TEXT,
            display_name TEXT,
            registered_at INTEGER NOT NULL,
            last_seen_at INTEGER,
            status TEXT NOT NULL DEFAULT 'active',
            blocked_at INTEGER,
            blocked_reason TEXT,
            is_deleted INTEGER NOT NULL DEFAULT 0
        )""",
        "CREATE INDEX idx_users_registered ON users(registered_at)",
        "CREATE INDEX idx_users_username ON users(username)",

        """CREATE TABLE account_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER NOT NULL REFERENCES users(telegram_id),
            ig_username TEXT NOT NULL,
            ig_user_id TEXT,
            status TEXT NOT NULL DEFAULT 'linked',
            linked_at INTEGER NOT NULL,
            unlinked_at INTEGER,
            last_activity_at INTEGER,
            last_error TEXT
        )""",
        "CREATE UNIQUE INDEX idx_links_ig_active ON account_links(ig_username) WHERE status='linked'",
        "CREATE INDEX idx_links_tg ON account_links(telegram_id, status)",

        """CREATE TABLE link_codes (
            code TEXT PRIMARY KEY,
            ig_username TEXT NOT NULL,
            expires_at INTEGER NOT NULL,
            used INTEGER NOT NULL DEFAULT 0,
            used_by INTEGER,
            created_at INTEGER NOT NULL
        )""",
        "CREATE INDEX idx_codes_ig ON link_codes(ig_username, used)",

        """CREATE TABLE ig_seen (
            thread_id TEXT PRIMARY KEY,
            last_msg_id TEXT NOT NULL,
            updated_at INTEGER NOT NULL
        )""",

        # ---------- مدیران، نقش‌ها و مجوزها (بخش ۱۹) ----------
        """CREATE TABLE roles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            is_system INTEGER NOT NULL DEFAULT 0,
            permissions TEXT NOT NULL DEFAULT '[]'
        )""",
        """CREATE TABLE admins (
            telegram_id INTEGER PRIMARY KEY REFERENCES users(telegram_id),
            role_id INTEGER NOT NULL REFERENCES roles(id),
            added_by INTEGER,
            added_at INTEGER NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1
        )""",

        # ---------- تنظیمات پایدار (بخش ۱۸) ----------
        """CREATE TABLE settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at INTEGER,
            updated_by INTEGER
        )""",

        # ---------- پلن‌ها و نسخه‌ها (بخش ۸ و ۹) ----------
        """CREATE TABLE plans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            title TEXT NOT NULL,
            description TEXT,
            current_version_id INTEGER,
            sort_order INTEGER NOT NULL DEFAULT 0,
            is_archived INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL
        )""",
        """CREATE TABLE plan_versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            plan_id INTEGER NOT NULL REFERENCES plans(id),
            version INTEGER NOT NULL,
            config TEXT NOT NULL,
            changelog TEXT,
            status TEXT NOT NULL DEFAULT 'draft',
            created_by INTEGER,
            created_at INTEGER NOT NULL,
            published_at INTEGER,
            UNIQUE(plan_id, version)
        )""",
        "CREATE INDEX idx_plan_versions_plan ON plan_versions(plan_id, status)",

        # ---------- اشتراک‌ها (بخش ۱۳.۹) ----------
        """CREATE TABLE subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(telegram_id),
            plan_id INTEGER NOT NULL REFERENCES plans(id),
            plan_version_id INTEGER NOT NULL REFERENCES plan_versions(id),
            status TEXT NOT NULL DEFAULT 'active',
            starts_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL,
            source TEXT NOT NULL DEFAULT 'purchase',
            source_ref TEXT,
            created_at INTEGER NOT NULL,
            canceled_at INTEGER,
            canceled_by INTEGER,
            suspend_reason TEXT
        )""",
        "CREATE INDEX idx_subs_user ON subscriptions(user_id, status)",
        "CREATE INDEX idx_subs_expiry ON subscriptions(status, expires_at)",

        """CREATE TABLE subscription_extensions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subscription_id INTEGER NOT NULL REFERENCES subscriptions(id),
            added_seconds INTEGER NOT NULL,
            reason TEXT,
            created_by INTEGER,
            created_at INTEGER NOT NULL
        )""",

        # ---------- سفارش و پرداخت دستی (بخش ۱۳) ----------
        """CREATE TABLE payment_methods (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            card_number TEXT,
            holder_name TEXT,
            bank_name TEXT,
            instructions TEXT,
            note TEXT,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at INTEGER NOT NULL
        )""",
        """CREATE TABLE orders (
            id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(telegram_id),
            plan_id INTEGER NOT NULL REFERENCES plans(id),
            plan_version_id INTEGER NOT NULL REFERENCES plan_versions(id),
            amount INTEGER NOT NULL,
            duration_seconds INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'awaiting_payment',
            payment_method_id INTEGER REFERENCES payment_methods(id),
            payment_snapshot TEXT,
            created_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL,
            announced_at INTEGER,
            receipt_file_id TEXT,
            receipt_received_at INTEGER,
            review_started_at INTEGER,
            confirmed_at INTEGER,
            confirmed_by INTEGER,
            subscription_id INTEGER,
            rejected_at INTEGER,
            rejected_by INTEGER,
            rejection_reason TEXT
        )""",
        "CREATE INDEX idx_orders_user ON orders(user_id, created_at)",
        "CREATE INDEX idx_orders_status ON orders(status, created_at)",

        """CREATE TABLE receipts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id TEXT NOT NULL REFERENCES orders(id),
            file_id TEXT,
            note TEXT,
            received_at INTEGER NOT NULL
        )""",

        # ---------- کدهای اشتراک (بخش ۱۴) ----------
        """CREATE TABLE promo_codes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT UNIQUE NOT NULL,
            plan_id INTEGER NOT NULL REFERENCES plans(id),
            duration_seconds INTEGER NOT NULL,
            max_uses INTEGER,
            used_count INTEGER NOT NULL DEFAULT 0,
            expires_at INTEGER,
            allowed_user_id INTEGER,
            allowed_group_id INTEGER,
            campaign_id INTEGER,
            batch_id TEXT,
            kind TEXT NOT NULL DEFAULT 'gift',
            status TEXT NOT NULL DEFAULT 'active',
            note TEXT,
            created_by INTEGER,
            created_at INTEGER NOT NULL
        )""",
        """CREATE TABLE promo_code_uses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code_id INTEGER NOT NULL REFERENCES promo_codes(id),
            user_id INTEGER NOT NULL,
            subscription_id INTEGER,
            used_at INTEGER NOT NULL,
            UNIQUE(code_id, user_id)
        )""",

        # ---------- کمپین‌ها (بخش ۱۰.۵ و ۱۲.۳) ----------
        """CREATE TABLE campaigns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            kind TEXT NOT NULL DEFAULT 'trial',
            plan_id INTEGER REFERENCES plans(id),
            target_group_id INTEGER,
            starts_at INTEGER,
            ends_at INTEGER,
            discount_percent INTEGER,
            discount_amount INTEGER,
            max_uses INTEGER,
            used_count INTEGER NOT NULL DEFAULT 0,
            per_user_limit INTEGER NOT NULL DEFAULT 1,
            message_text TEXT,
            status TEXT NOT NULL DEFAULT 'draft',
            stats TEXT NOT NULL DEFAULT '{}',
            created_by INTEGER,
            created_at INTEGER NOT NULL
        )""",

        # ---------- گروه‌های کاربری و سیاست‌ها (بخش ۷) ----------
        """CREATE TABLE user_groups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            kind TEXT NOT NULL DEFAULT 'time_window',
            boundary_start INTEGER,
            boundary_end INTEGER,
            filter_type TEXT,
            filter_value TEXT,
            description TEXT,
            created_at INTEGER NOT NULL
        )""",
        """CREATE TABLE user_group_members (
            group_id INTEGER NOT NULL REFERENCES user_groups(id),
            user_id INTEGER NOT NULL REFERENCES users(telegram_id),
            added_at INTEGER NOT NULL,
            PRIMARY KEY (group_id, user_id)
        )""",
        """CREATE TABLE group_policies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id INTEGER NOT NULL REFERENCES user_groups(id),
            action TEXT NOT NULL,
            params TEXT NOT NULL DEFAULT '{}',
            scheduled_at INTEGER,
            status TEXT NOT NULL DEFAULT 'pending',
            preview_count INTEGER,
            applied_at INTEGER,
            applied_by INTEGER,
            result TEXT,
            created_by INTEGER,
            created_at INTEGER NOT NULL
        )""",
        "CREATE INDEX idx_policies_due ON group_policies(status, scheduled_at)",

        # ---------- استثناهای فردی (بخش ۱۵.۱۰) ----------
        """CREATE TABLE user_exceptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(telegram_id),
            concurrent_downloads INTEGER,
            wait_seconds INTEGER,
            hourly_quota INTEGER,
            daily_quota INTEGER,
            total_quota INTEGER,
            priority INTEGER,
            starts_at INTEGER NOT NULL,
            ends_at INTEGER,
            note TEXT,
            created_by INTEGER,
            created_at INTEGER NOT NULL
        )""",
        "CREATE INDEX idx_exceptions_user ON user_exceptions(user_id, starts_at, ends_at)",

        # ---------- موتور دانلود و صف (بخش ۱۵) ----------
        """CREATE TABLE download_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(telegram_id),
            shortcode TEXT NOT NULL,
            media_url TEXT NOT NULL,
            media_kind TEXT NOT NULL DEFAULT 'post',
            status TEXT NOT NULL DEFAULT 'queued',
            priority INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            started_at INTEGER,
            finished_at INTEGER,
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            error_stage TEXT,
            file_paths TEXT,
            file_size INTEGER,
            quota_charged INTEGER NOT NULL DEFAULT 0,
            cooldown_at INTEGER
        )""",
        "CREATE INDEX idx_dl_user_status ON download_requests(user_id, status)",
        "CREATE INDEX idx_dl_queue ON download_requests(status, priority DESC, created_at)",
        "CREATE INDEX idx_dl_quota ON download_requests(user_id, quota_charged, finished_at)",

        # ---------- رویدادهای مدیریتی (بخش ۶.۳) ----------
        """CREATE TABLE audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            admin_id INTEGER NOT NULL,
            action TEXT NOT NULL,
            target_type TEXT,
            target_id TEXT,
            old_value TEXT,
            new_value TEXT,
            result TEXT NOT NULL DEFAULT 'success',
            created_at INTEGER NOT NULL
        )""",
        "CREATE INDEX idx_audit_admin ON audit_log(admin_id, created_at)",

        # ---------- گزارش خطا (بخش ۲۰.۲) ----------
        """CREATE TABLE error_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at INTEGER NOT NULL,
            category TEXT NOT NULL,
            operation TEXT,
            request_id INTEGER,
            user_id INTEGER,
            stage TEXT,
            message TEXT NOT NULL,
            retry_status TEXT
        )""",
        "CREATE INDEX idx_errors_cat ON error_log(category, created_at)",

        # ---------- پیام همگانی (بخش ۱۷) ----------
        """CREATE TABLE broadcast_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            admin_id INTEGER NOT NULL,
            filter_spec TEXT NOT NULL,
            message_text TEXT NOT NULL,
            parse_mode TEXT,
            status TEXT NOT NULL DEFAULT 'draft',
            total INTEGER NOT NULL DEFAULT 0,
            sent INTEGER NOT NULL DEFAULT 0,
            failed INTEGER NOT NULL DEFAULT 0,
            offset INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL,
            finished_at INTEGER
        )""",

        # ---------- سابقه دریافت آزمایشی (بخش ۱۰.۴) ----------
        """CREATE TABLE trial_grants (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(telegram_id),
            plan_id INTEGER NOT NULL REFERENCES plans(id),
            source TEXT NOT NULL DEFAULT 'admin',
            created_at INTEGER NOT NULL
        )""",
        "CREATE UNIQUE INDEX idx_trial_once ON trial_grants(user_id, plan_id, source)",
    ]),
    # ---------- سابقه پشتیبان‌گیری (§۳۳ و §۴۲ سند ۲) ----------
    (3, [
        """CREATE TABLE backup_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            path TEXT NOT NULL,
            size_bytes INTEGER,
            status TEXT NOT NULL DEFAULT 'ok',
            note TEXT,
            created_at INTEGER NOT NULL
        )""",
    ]),
]
