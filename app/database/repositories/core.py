"""Repository های پایه: کاربران، اتصال حساب‌ها، مدیران، تنظیمات."""
from __future__ import annotations

import json
import secrets
import time

from ..database import Database

NOW = lambda: int(time.time())  # noqa: E731


class UsersRepo:
    def __init__(self, db: Database):
        self.db = db

    def register(self, telegram_id: int, username: str | None, display_name: str | None) -> bool:
        """ثبت‌نام کاربر؛ ثبت‌نام تکراری تاریخ اولیه را تغییر نمی‌دهد (بخش ۶.۱)."""
        existing = self.get(telegram_id)
        now = NOW()
        if existing:
            self.db.execute(
                "UPDATE users SET username=?, display_name=?, last_seen_at=?, is_deleted=0 "
                "WHERE telegram_id=?",
                (username, display_name, now, telegram_id),
            )
            return False
        self.db.execute(
            "INSERT INTO users (telegram_id, username, display_name, registered_at, last_seen_at)"
            " VALUES (?,?,?,?,?)",
            (telegram_id, username, display_name, now, now),
        )
        return True

    def get(self, telegram_id: int):
        return self.db.query_one("SELECT * FROM users WHERE telegram_id=?", (telegram_id,))

    def touch(self, telegram_id: int) -> None:
        self.db.execute("UPDATE users SET last_seen_at=? WHERE telegram_id=?", (NOW(), telegram_id))

    def set_blocked(self, telegram_id: int, blocked: bool, reason: str | None = None) -> bool:
        if blocked:
            ok = self.db.execute(
                "UPDATE users SET status='blocked', blocked_at=?, blocked_reason=? "
                "WHERE telegram_id=? AND status!='blocked'",
                (NOW(), reason, telegram_id),
            ).rowcount > 0
        else:
            ok = self.db.execute(
                "UPDATE users SET status='active', blocked_at=NULL, blocked_reason=NULL "
                "WHERE telegram_id=?",
                (telegram_id,),
            ).rowcount > 0
        return ok

    def is_blocked(self, telegram_id: int) -> bool:
        u = self.get(telegram_id)
        return bool(u and u["status"] == "blocked")

    def search(self, query: str | None = None, limit: int = 20, offset: int = 0):
        if query:
            q = f"%{query.strip().lstrip('@').lower()}%"
            return self.db.query(
                "SELECT * FROM users WHERE CAST(telegram_id AS TEXT) LIKE ? "
                "OR LOWER(COALESCE(username,'')) LIKE ? ORDER BY registered_at DESC LIMIT ? OFFSET ?",
                (q, q, limit, offset),
            )
        return self.db.query(
            "SELECT * FROM users ORDER BY registered_at DESC LIMIT ? OFFSET ?", (limit, offset)
        )

    def count(self, active_sub_only: bool = False) -> int:
        if active_sub_only:
            return int(self.db.scalar(
                "SELECT COUNT(DISTINCT s.user_id) FROM subscriptions s WHERE s.status='active'"
            ) or 0)
        return int(self.db.scalar("SELECT COUNT(*) FROM users WHERE is_deleted=0") or 0)

    def filter_ids(self, filter_type: str, filter_value: str | None = None) -> list[int]:
        """شناسه کاربران مشمول یک فیلتر (بخش ۷.۳)."""
        sql = {
            "all": "SELECT telegram_id FROM users WHERE is_deleted=0",
            "active_sub": "SELECT DISTINCT s.user_id FROM subscriptions s WHERE s.status='active'",
            "no_sub": (
                "SELECT telegram_id FROM users WHERE is_deleted=0 AND telegram_id NOT IN "
                "(SELECT user_id FROM subscriptions WHERE status='active')"
            ),
            "expired_sub": (
                "SELECT DISTINCT s.user_id FROM subscriptions s WHERE s.status='expired'"
            ),
            "blocked": "SELECT telegram_id FROM users WHERE status='blocked'",
        }
        args: tuple = ()
        if filter_type == "plan":
            sql = {
                "plan": "SELECT DISTINCT s.user_id FROM subscriptions s WHERE plan_id=? AND status='active'"
            }["plan"]
            args = (int(filter_value),)
        return [r["telegram_id"] for r in self.db.query(sql, args)]

    def ids_in_time_window(self, start: int | None, end: int | None) -> list[int]:
        """کاربران با registered_at در بازه [start, end) — برای گروه‌های زمانی."""
        if start is None and end is None:
            return self.filter_ids("all")
        if start is None:
            rows = self.db.query("SELECT telegram_id FROM users WHERE registered_at < ?", (end,))
        elif end is None:
            rows = self.db.query("SELECT telegram_id FROM users WHERE registered_at >= ?", (start,))
        else:
            rows = self.db.query(
                "SELECT telegram_id FROM users WHERE registered_at >= ? AND registered_at < ?",
                (start, end),
            )
        return [r["telegram_id"] for r in rows]


class LinksRepo:
    """مدیریت اتصال حساب اینستاگرام به تلگرام (بخش ۵)."""

    def __init__(self, db: Database):
        self.db = db

    def create_code(self, ig_username: str, ttl_seconds: int = 1200) -> str:
        """کد اتصال ۶ رقمی؛ کد معتبر قبلی همان کاربر برگردانده می‌شود."""
        now = NOW()
        self.db.execute("DELETE FROM link_codes WHERE expires_at < ?", (now,))
        row = self.db.query_one(
            "SELECT code FROM link_codes WHERE ig_username=? AND used=0 AND expires_at>?",
            (ig_username, now),
        )
        if row:
            return row["code"]
        while True:
            code = str(secrets.randbelow(900000) + 100000)
            if not self.db.query_one("SELECT 1 FROM link_codes WHERE code=?", (code,)):
                break
        self.db.execute(
            "INSERT INTO link_codes (code, ig_username, expires_at, created_at) VALUES (?,?,?,?)",
            (code, ig_username, now + ttl_seconds, now),
        )
        return code

    def valid_code(self, code: str):
        now = NOW()
        return self.db.query_one(
            "SELECT * FROM link_codes WHERE code=? AND used=0 AND expires_at>?", (code, now)
        )

    def link(self, code: str, telegram_id: int, ig_user_id: str | None = None) -> tuple[bool, str]:
        """اتصال حساب؛ اطمینان از یک‌به‌یک‌بودن ارتباط (قفل تراکنشی).

        ترتیب مهم است: همه بررسی‌ها «قبل از» مصرف‌شدن کد انجام می‌شوند تا
        یک تلاش ناموفق، کدِ هنوز معتبر را نسوزاند (قبلاً کد در همان ابتدا
        used=1 می‌شد و تلاش بعدی با همان کد «اشتباه یا منقضی» می‌گرفت).
        """
        from ...utils import normalize_digits
        code = normalize_digits(str(code).strip())
        row = self.valid_code(code)
        if not row:
            return False, "کد اشتباه است یا منقضی شده است."
        ig_username = row["ig_username"].lower()
        with self.db.tx() as conn:
            # اتصال تکراری همان جفت → موفقیت بدون تغییر (idempotent)
            existing = conn.execute(
                "SELECT id, telegram_id FROM account_links WHERE ig_username=? AND status='linked'",
                (ig_username,),
            ).fetchone()
            if existing is not None:
                if existing["telegram_id"] == telegram_id:
                    return True, ig_username
                return False, "این اکانت اینستاگرام قبلاً به تلگرام دیگری وصل شده است."
            mine = conn.execute(
                "SELECT id, ig_username FROM account_links WHERE telegram_id=? AND status='linked'",
                (telegram_id,),
            ).fetchone()
            if mine:
                return False, (
                    f"حساب تلگرامت قبلاً به @{mine['ig_username']} وصل است. "
                    "اول با /unlink قطعش کن."
                )
            used = conn.execute(
                "UPDATE link_codes SET used=1, used_by=? WHERE code=? AND used=0",
                (telegram_id, code),
            ).rowcount
            if not used:
                return False, "کد قبلاً استفاده شده است."
            conn.execute(
                "INSERT INTO account_links (telegram_id, ig_username, ig_user_id, status, linked_at)"
                " VALUES (?,?,?,'linked',?)",
                (telegram_id, ig_username, ig_user_id, NOW()),
            )
        return True, ig_username

    def unlink_by_admin(self, telegram_id: int) -> str | None:
        """قطع اتصال توسط مدیر؛ سابقه اشتراک/سفارش حفظ می‌شود (بخش ۵.۴)."""
        row = self.get_active(telegram_id)
        if not row:
            return None
        self.db.execute(
            "UPDATE account_links SET status='unlinked', unlinked_at=? WHERE id=?",
            (NOW(), row["id"]),
        )
        return row["ig_username"]

    def get_active(self, telegram_id: int):
        return self.db.query_one(
            "SELECT * FROM account_links WHERE telegram_id=? AND status='linked'", (telegram_id,)
        )

    def find_by_ig(self, ig_username: str):
        """یافتن کاربر تلگرامی متصل به یک یوزرنیم اینستاگرام."""
        return self.db.query_one(
            "SELECT * FROM account_links WHERE ig_username=? AND status='linked'",
            (ig_username.lower(),),
        )

    def touch_activity(self, telegram_id: int, error: str | None = None) -> None:
        self.db.execute(
            "UPDATE account_links SET last_activity_at=?, last_error=? WHERE telegram_id=? AND status='linked'",
            (NOW(), error, telegram_id),
        )

    def stats(self) -> dict:
        return {
            "linked": int(self.db.scalar("SELECT COUNT(*) FROM account_links WHERE status='linked'") or 0),
        }


class AdminsRepo:
    def __init__(self, db: Database):
        self.db = db

    def seed(self, owner_ids: list[int]) -> None:
        """ایجاد نقش‌های سیستمی و مدیران اولیه (بخش ۱۹.۱)."""
        from ...admin.permissions import SYSTEM_ROLES
        for name, perms, is_system in SYSTEM_ROLES:
            self.db.execute(
                "INSERT INTO roles (name, permissions, is_system) VALUES (?,?,?) "
                "ON CONFLICT(name) DO UPDATE SET permissions=excluded.permissions",
                (name, json.dumps(perms), is_system),
            )
        owner_role = self.db.query_one("SELECT id FROM roles WHERE name='owner'")
        for tid in owner_ids:
            self.db.execute("INSERT OR IGNORE INTO users (telegram_id, registered_at) VALUES (?,?)",
                            (tid, NOW()))
            self.db.execute(
                "INSERT INTO admins (telegram_id, role_id, added_at) VALUES (?,?,?) "
                "ON CONFLICT(telegram_id) DO UPDATE SET is_active=1",
                (tid, owner_role["id"], NOW()),
            )

    def get_admin(self, telegram_id: int):
        return self.db.query_one(
            "SELECT a.*, r.name AS role_name, r.permissions FROM admins a "
            "JOIN roles r ON r.id=a.role_id WHERE a.telegram_id=? AND a.is_active=1",
            (telegram_id,),
        )

    def has_permission(self, telegram_id: int, permission: str) -> bool:
        admin = self.get_admin(telegram_id)
        if not admin:
            return False
        perms = json.loads(admin["permissions"])
        return "*" in perms or permission in perms

    def list_admins(self):
        return self.db.query(
            "SELECT a.telegram_id, a.added_at, r.name AS role_name, u.username "
            "FROM admins a JOIN roles r ON r.id=a.role_id "
            "LEFT JOIN users u ON u.telegram_id=a.telegram_id WHERE a.is_active=1"
        )

    def add(self, telegram_id: int, role_id: int, added_by: int) -> bool:
        if not self.db.query_one("SELECT 1 FROM users WHERE telegram_id=?", (telegram_id,)):
            return False
        cur = self.db.execute(
            "INSERT INTO admins (telegram_id, role_id, added_by, added_at) VALUES (?,?,?,?) "
            "ON CONFLICT(telegram_id) DO UPDATE SET role_id=excluded.role_id, is_active=1",
            (telegram_id, role_id, added_by, NOW()),
        )
        return cur.rowcount > 0

    def remove(self, telegram_id: int) -> bool:
        return self.db.execute(
            "UPDATE admins SET is_active=0 WHERE telegram_id=?", (telegram_id,)
        ).rowcount > 0

    def roles(self):
        return self.db.query("SELECT * FROM roles ORDER BY is_system DESC, name")

    def create_role(self, name: str, permissions: list[str]) -> int:
        cur = self.db.execute(
            "INSERT INTO roles (name, permissions) VALUES (?,?)", (name, json.dumps(permissions))
        )
        return int(cur.lastrowid)

    def delete_role(self, role_id: int) -> bool:
        return self.db.execute(
            "DELETE FROM roles WHERE id=? AND is_system=0 AND id NOT IN "
            "(SELECT DISTINCT role_id FROM admins WHERE is_active=1)",
            (role_id,),
        ).rowcount > 0


class SettingsRepo:
    def __init__(self, db: Database):
        self.db = db

    def get(self, key: str, default=None):
        row = self.db.query_one("SELECT value FROM settings WHERE key=?", (key,))
        if not row:
            return default
        try:
            return json.loads(row["value"])
        except (json.JSONDecodeError, TypeError):
            return default

    def set(self, key: str, value, updated_by: int | None = None) -> None:
        self.db.execute(
            "INSERT INTO settings (key, value, updated_at, updated_by) VALUES (?,?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at,"
            " updated_by=excluded.updated_by",
            (key, json.dumps(value, ensure_ascii=False), NOW(), updated_by),
        )

    def all(self) -> dict:
        return {r["key"]: self.get(r["key"]) for r in self.db.query("SELECT key FROM settings")}
