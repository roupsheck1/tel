"""Repository های عملیاتی: صف دانلود، رویداد مدیریتی، خطا، پیام همگانی، گروه‌ها."""
from __future__ import annotations

import json
import time

from ..database import Database

NOW = lambda: int(time.time())  # noqa: E731

DL_QUEUED = "queued"
DL_ACTIVE = ("downloading", "ready_to_send", "sending")
DL_TERMINAL = ("completed", "failed", "canceled", "expired")


class DownloadsRepo:
    """ذخیره‌سازی پایدار صف و وضعیت درخواست‌ها (بخش ۱۵.۱ و ۱۵.۱۴)."""

    def __init__(self, db: Database):
        self.db = db

    def create(self, user_id: int, shortcode: str, media_url: str, media_kind: str = "post",
               priority: int = 0) -> int:
        cur = self.db.execute(
            "INSERT INTO download_requests (user_id, shortcode, media_url, media_kind, status,"
            " priority, created_at, updated_at) VALUES (?,?,?,?, 'queued', ?,?,?)",
            (user_id, shortcode, media_url, media_kind, priority, NOW(), NOW()),
        )
        return int(cur.lastrowid)

    def get(self, request_id: int):
        return self.db.query_one("SELECT * FROM download_requests WHERE id=?", (request_id,))

    def set_status(self, request_id: int, status: str, **extra) -> bool:
        sets = ["status=?", "updated_at=?"]
        args: list = [status, NOW()]
        for key in ("started_at", "finished_at", "last_error", "error_stage", "attempts",
                    "file_paths", "file_size", "quota_charged", "cooldown_at", "priority"):
            if key in extra and extra[key] is not None:
                sets.append(f"{key}=?")
                val = extra[key]
                if key == "file_paths" and isinstance(val, list):
                    val = json.dumps(val)
                args.append(val)
        args.append(request_id)
        cur = self.db.execute(
            f"UPDATE download_requests SET {', '.join(sets)} WHERE id=?", args
        )
        return cur.rowcount > 0

    def queued(self, limit: int = 200):
        return self.db.query(
            "SELECT * FROM download_requests WHERE status='queued' "
            "ORDER BY priority DESC, created_at ASC LIMIT ?",
            (limit,),
        )

    def active_of_user(self, user_id: int) -> int:
        """تعداد عملیات فعال کاربر (هم‌زمانی — محدودیت اول بخش ۱۵.۳)."""
        return int(self.db.scalar(
            "SELECT COUNT(*) FROM download_requests WHERE user_id=? AND status IN "
            "('downloading','ready_to_send','sending')",
            (user_id,),
        ) or 0)

    def queued_of_user(self, user_id: int) -> int:
        return int(self.db.scalar(
            "SELECT COUNT(*) FROM download_requests WHERE user_id=? AND status='queued'",
            (user_id,),
        ) or 0)

    def global_active(self) -> int:
        return int(self.db.scalar(
            "SELECT COUNT(*) FROM download_requests WHERE status IN "
            "('downloading','ready_to_send','sending')"
        ) or 0)

    def global_queued(self) -> int:
        return int(self.db.scalar(
            "SELECT COUNT(*) FROM download_requests WHERE status='queued'"
        ) or 0)

    def last_completed_at(self, user_id: int) -> int | None:
        """زمان آخرین تکمیل موفق — مبنای زمان انتظار (محدودیت دوم بخش ۱۵.۳)."""
        return self.db.scalar(
            "SELECT MAX(finished_at) FROM download_requests WHERE user_id=? AND status='completed'",
            (user_id,),
        )

    def set_cooldown(self, user_id: int, until: int) -> None:
        self.db.execute(
            "UPDATE download_requests SET cooldown_at=? WHERE user_id=? AND status='completed'",
            (until, user_id),
        )

    def cooldown_until(self, user_id: int) -> int | None:
        return self.db.scalar(
            "SELECT MAX(cooldown_at) FROM download_requests WHERE user_id=? AND status='completed'",
            (user_id,),
        )

    def quota_used(self, user_id: int, since: int | None = None) -> dict:
        """مصرف سهمیه بر اساس دانلودهای موفق شارژشده (محدودیت سوم بخش ۱۵.۳)."""
        total = int(self.db.scalar(
            "SELECT COUNT(*) FROM download_requests WHERE user_id=? AND quota_charged=1",
            (user_id,),
        ) or 0)
        hourly = int(self.db.scalar(
            "SELECT COUNT(*) FROM download_requests WHERE user_id=? AND quota_charged=1 "
            "AND finished_at>=?",
            (user_id, NOW() - 3600),
        ) or 0)
        day_start = NOW() - 86400
        daily = int(self.db.scalar(
            "SELECT COUNT(*) FROM download_requests WHERE user_id=? AND quota_charged=1 "
            "AND finished_at>=?",
            (user_id, day_start),
        ) or 0)
        return {"total": total, "hourly": hourly, "daily": daily}

    def charge_quota(self, request_id: int) -> None:
        self.db.execute(
            "UPDATE download_requests SET quota_charged=1 WHERE id=?", (request_id,)
        )

    def refund_quota(self, request_id: int) -> bool:
        """اصلاح مصرف سهمیه توسط مدیر (§۶۰ سند ۲: «آیا مدیر می‌تواند سهمیه
        مصرف‌شده را اصلاح کند؟» — بله؛ مصرف تاریخی حذف نمی‌شود، فقط شارژِ
        اشتباهِ همان درخواست پس گرفته می‌شود و در audit ثبت می‌شود)."""
        return self.db.execute(
            "UPDATE download_requests SET quota_charged=0 WHERE id=? AND quota_charged=1",
            (request_id,),
        ).rowcount > 0

    def recover_on_startup(self) -> dict:
        """بازیابی صف پس از راه‌اندازی مجدد (بخش ۱۵.۱۴)."""
        reset = self.db.execute(
            "UPDATE download_requests SET status='queued', updated_at=? WHERE status IN "
            "('downloading','ready_to_send','sending')",
            (NOW(),),
        ).rowcount
        return {"reset_to_queue": reset}

    def cancel(self, request_id: int, user_id: int | None = None) -> bool:
        cond = "id=?"
        args: list = [request_id]
        if user_id is not None:
            cond += " AND user_id=?"
            args.append(user_id)
        cur = self.db.execute(
            f"UPDATE download_requests SET status='canceled', finished_at=?, updated_at=? "
            f"WHERE {cond} AND status IN ('queued','received')",
            (NOW(), NOW(), *args),
        )
        return cur.rowcount > 0

    def retry(self, request_id: int) -> bool:
        return self.db.execute(
            "UPDATE download_requests SET status='queued', attempts=0, last_error=NULL, updated_at=?"
            " WHERE id=? AND status='failed'",
            (NOW(), request_id),
        ).rowcount > 0

    def set_priority(self, request_id: int, priority: int) -> bool:
        """تغییر اولویت دستی یک درخواست صف توسط مدیر (بخش ۶۴ سند ۲).

        اولویت صف مستقل از سهمیه و تعداد دانلود هم‌زمان است (پیشنهاد §۶۴).
        فقط درخواست‌های در صف قابل تغییرند تا وضعیت عملیات در جریان دست‌کاری نشود.
        """
        return self.db.execute(
            "UPDATE download_requests SET priority=?, updated_at=? WHERE id=? AND status='queued'",
            (priority, NOW(), request_id),
        ).rowcount > 0

    def recent(self, status: str | None = None, user_id: int | None = None, limit: int = 20):
        cond, args = [], []
        if status:
            cond.append("status=?")
            args.append(status)
        if user_id:
            cond.append("user_id=?")
            args.append(user_id)
        where = (" WHERE " + " AND ".join(cond)) if cond else ""
        args.append(limit)
        return self.db.query(
            f"SELECT d.*, u.username FROM download_requests d "
            f"LEFT JOIN users u ON u.telegram_id=d.user_id{where} "
            "ORDER BY d.created_at DESC LIMIT ?",
            args,
        )

    def status_counts(self) -> dict:
        rows = self.db.query("SELECT status, COUNT(*) AS c FROM download_requests GROUP BY status")
        return {r["status"]: r["c"] for r in rows}

    def stats(self) -> dict:
        return {
            "completed": int(self.db.scalar(
                "SELECT COUNT(*) FROM download_requests WHERE status='completed'") or 0),
            "failed": int(self.db.scalar(
                "SELECT COUNT(*) FROM download_requests WHERE status='failed'") or 0),
            "today": int(self.db.scalar(
                "SELECT COUNT(*) FROM download_requests WHERE status='completed' "
                "AND finished_at>=?", (NOW() - 86400,)) or 0),
        }


class AuditRepo:
    """ثبت رویدادهای مدیریتی (بخش ۶.۳)."""

    def __init__(self, db: Database):
        self.db = db

    def log(self, admin_id: int, action: str, target_type: str | None = None,
            target_id: str | None = None, old_value=None, new_value=None,
            result: str = "success") -> None:
        self.db.execute(
            "INSERT INTO audit_log (admin_id, action, target_type, target_id, old_value, new_value,"
            " result, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (admin_id, action, target_type, target_id,
             json.dumps(old_value, ensure_ascii=False) if old_value is not None else None,
             json.dumps(new_value, ensure_ascii=False) if new_value is not None else None,
             result, NOW()),
        )

    def recent(self, limit: int = 20):
        return self.db.query(
            "SELECT a.*, u.username FROM audit_log a LEFT JOIN users u "
            "ON u.telegram_id=a.admin_id ORDER BY a.created_at DESC LIMIT ?",
            (limit,),
        )


class ErrorsRepo:
    """گزارش خطا با دسته‌بندی (بخش ۲۰.۲)."""

    def __init__(self, db: Database):
        self.db = db

    def log(self, category: str, operation: str, message: str, request_id: int | None = None,
            user_id: int | None = None, stage: str | None = None,
            retry_status: str | None = None) -> int:
        # ماسک اطلاعات محرمانه پیش از ذخیره (بخش ۲۰.۳ سند)
        from ...logging_config import mask_secrets
        message = mask_secrets(message)
        cur = self.db.execute(
            "INSERT INTO error_log (created_at, category, operation, request_id, user_id, stage,"
            " message, retry_status) VALUES (?,?,?,?,?,?,?,?)",
            (NOW(), category, operation, request_id, user_id, stage, message[:2000], retry_status),
        )
        return int(cur.lastrowid)

    def recent(self, category: str | None = None, limit: int = 20, search: str | None = None):
        cond, args = [], []
        if category:
            cond.append("category=?")
            args.append(category)
        if search:
            cond.append("message LIKE ?")
            args.append(f"%{search}%")
        where = (" WHERE " + " AND ".join(cond)) if cond else ""
        args.append(limit)
        return self.db.query(
            f"SELECT * FROM error_log{where} ORDER BY created_at DESC LIMIT ?", args
        )

    def count_recent(self, seconds: int = 3600) -> int:
        return int(self.db.scalar(
            "SELECT COUNT(*) FROM error_log WHERE created_at>=?", (NOW() - seconds,)
        ) or 0)

    def counts_by_category(self) -> dict:
        return {
            r["category"]: r["c"]
            for r in self.db.query(
                "SELECT category, COUNT(*) AS c FROM error_log GROUP BY category"
            )
        }


class BroadcastRepo:
    """کارهای پیام همگانی با پیشرفت پایدار (بخش ۱۷)."""

    def __init__(self, db: Database):
        self.db = db

    def create(self, admin_id: int, filter_spec: dict, message_text: str,
               parse_mode: str | None = None) -> int:
        cur = self.db.execute(
            "INSERT INTO broadcast_jobs (admin_id, filter_spec, message_text, parse_mode, status,"
            " created_at) VALUES (?,?,?,?, 'draft', ?)",
            (admin_id, json.dumps(filter_spec, ensure_ascii=False), message_text, parse_mode, NOW()),
        )
        return int(cur.lastrowid)

    def get(self, job_id: int):
        row = self.db.query_one("SELECT * FROM broadcast_jobs WHERE id=?", (job_id,))
        if row:
            return dict(row)
        return None

    def set_preview(self, job_id: int, total: int) -> None:
        self.db.execute(
            "UPDATE broadcast_jobs SET total=?, status='preview' WHERE id=?", (total, job_id)
        )

    def start(self, job_id: int) -> None:
        self.db.execute("UPDATE broadcast_jobs SET status='running' WHERE id=?", (job_id,))

    def update_progress(self, job_id: int, sent: int, failed: int, offset: int) -> None:
        self.db.execute(
            "UPDATE broadcast_jobs SET sent=?, failed=?, offset=? WHERE id=?",
            (sent, failed, offset, job_id),
        )

    def finish(self, job_id: int, status: str = "completed") -> None:
        self.db.execute(
            "UPDATE broadcast_jobs SET status=?, finished_at=? WHERE id=?", (status, NOW(), job_id)
        )

    def stop(self, job_id: int) -> None:
        self.db.execute(
            "UPDATE broadcast_jobs SET status='paused' WHERE id=? AND status='running'", (job_id,)
        )

    def list(self, limit: int = 10):
        return self.db.query(
            "SELECT b.*, u.username FROM broadcast_jobs b LEFT JOIN users u "
            "ON u.telegram_id=b.admin_id ORDER BY b.created_at DESC LIMIT ?",
            (limit,),
        )

    def filter_spec(self, job_id: int) -> dict:
        row = self.get(job_id)
        return json.loads(row["filter_spec"]) if row else {}


class GroupsRepo:
    """گروه‌های کاربری و سیاست‌های زمان‌بندی‌شده (بخش ۷)."""

    def __init__(self, db: Database):
        self.db = db

    def create(self, name: str, kind: str, boundary_start: int | None = None,
               boundary_end: int | None = None, filter_type: str | None = None,
               filter_value: str | None = None, description: str | None = None) -> int:
        cur = self.db.execute(
            "INSERT INTO user_groups (name, kind, boundary_start, boundary_end, filter_type,"
            " filter_value, description, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (name, kind, boundary_start, boundary_end, filter_type, filter_value, description, NOW()),
        )
        return int(cur.lastrowid)

    def get(self, group_id: int):
        return self.db.query_one("SELECT * FROM user_groups WHERE id=?", (group_id,))

    def list(self):
        return self.db.query("SELECT * FROM user_groups ORDER BY created_at DESC")

    def member_ids(self, group_id: int) -> list[int]:
        """محاسبه اعضای گروه براساس نوع آن."""
        g = self.get(group_id)
        if not g:
            return []
        if g["kind"] == "manual":
            return [r["user_id"] for r in self.db.query(
                "SELECT user_id FROM user_group_members WHERE group_id=?", (group_id,))]
        if g["kind"] == "time_window":
            return self._users_in_window(g["boundary_start"], g["boundary_end"])
        return self._users_by_filter(g["filter_type"], g["filter_value"])

    def _users_in_window(self, start, end):
        if start is None and end is None:
            return [r["telegram_id"] for r in self.db.query(
                "SELECT telegram_id FROM users WHERE is_deleted=0")]
        if start is None:
            rows = self.db.query("SELECT telegram_id FROM users WHERE registered_at < ?", (end,))
        elif end is None:
            rows = self.db.query("SELECT telegram_id FROM users WHERE registered_at >= ?", (start,))
        else:
            rows = self.db.query(
                "SELECT telegram_id FROM users WHERE registered_at >= ? AND registered_at < ?",
                (start, end))
        return [r["telegram_id"] for r in rows]

    def _users_by_filter(self, filter_type, filter_value):
        if filter_type == "plan":
            rows = self.db.query(
                "SELECT DISTINCT user_id FROM subscriptions WHERE plan_id=? AND status='active'",
                (int(filter_value),))
        elif filter_type == "active_sub":
            rows = self.db.query(
                "SELECT DISTINCT user_id FROM subscriptions WHERE status='active'")
        elif filter_type == "no_sub":
            rows = self.db.query(
                "SELECT telegram_id FROM users WHERE is_deleted=0 AND telegram_id NOT IN "
                "(SELECT user_id FROM subscriptions WHERE status='active')")
        elif filter_type == "expired_sub":
            rows = self.db.query(
                "SELECT DISTINCT user_id FROM subscriptions WHERE status='expired'")
        elif filter_type == "blocked":
            rows = self.db.query("SELECT telegram_id FROM users WHERE status='blocked'")
        else:
            rows = self.db.query("SELECT telegram_id FROM users WHERE is_deleted=0")
        return [r[0] for r in rows]

    def add_members(self, group_id: int, user_ids: list[int]) -> int:
        if not user_ids:
            return 0
        self.db.executemany(
            "INSERT OR IGNORE INTO user_group_members (group_id, user_id, added_at) VALUES (?,?,?)",
            [(group_id, uid, NOW()) for uid in user_ids],
        )
        return len(user_ids)

    def create_policy(self, group_id: int, action: str, params: dict, scheduled_at: int | None,
                      created_by: int) -> int:
        cur = self.db.execute(
            "INSERT INTO group_policies (group_id, action, params, scheduled_at, status,"
            " created_by, created_at) VALUES (?,?,?,?, 'pending', ?,?)",
            (group_id, action, json.dumps(params, ensure_ascii=False), scheduled_at, created_by, NOW()),
        )
        return int(cur.lastrowid)

    def policy(self, policy_id: int):
        return self.db.query_one("SELECT * FROM group_policies WHERE id=?", (policy_id,))

    def set_preview(self, policy_id: int, count: int) -> None:
        self.db.execute(
            "UPDATE group_policies SET preview_count=?, status='previewed' WHERE id=?",
            (count, policy_id),
        )

    def mark_applied(self, policy_id: int, applied_by: int, result: dict) -> None:
        self.db.execute(
            "UPDATE group_policies SET status='applied', applied_at=?, applied_by=?, result=? "
            "WHERE id=?",
            (NOW(), applied_by, json.dumps(result, ensure_ascii=False), policy_id),
        )

    def mark_failed(self, policy_id: int, error: str) -> None:
        self.db.execute(
            "UPDATE group_policies SET status='failed', result=? WHERE id=?",
            (json.dumps({"error": error}, ensure_ascii=False), policy_id),
        )

    def due_policies(self) -> list:
        return self.db.query(
            "SELECT * FROM group_policies WHERE status='previewed' AND scheduled_at IS NOT NULL "
            "AND scheduled_at<=?",
            (NOW(),),
        )

    def policies_of(self, group_id: int):
        return self.db.query(
            "SELECT * FROM group_policies WHERE group_id=? ORDER BY created_at DESC LIMIT 10",
            (group_id,),
        )

    def policy_history(self, limit: int = 20):
        return self.db.query(
            "SELECT gp.*, g.name AS group_name FROM group_policies gp "
            "JOIN user_groups g ON g.id=gp.group_id ORDER BY gp.created_at DESC LIMIT ?",
            (limit,),
        )


class ExceptionsRepo:
    """استثناهای فردی کاربران با بازه اعتبار (بخش ۱۵.۱۰)."""

    def __init__(self, db: Database):
        self.db = db

    def add(self, user_id: int, starts_at: int, ends_at: int | None, created_by: int,
            **limits) -> int:
        cur = self.db.execute(
            "INSERT INTO user_exceptions (user_id, concurrent_downloads, wait_seconds,"
            " hourly_quota, daily_quota, total_quota, priority, starts_at, ends_at, note, created_by,"
            " created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (user_id, limits.get("concurrent_downloads"), limits.get("wait_seconds"),
             limits.get("hourly_quota"), limits.get("daily_quota"), limits.get("total_quota"),
             limits.get("priority"), starts_at, ends_at, limits.get("note"), created_by, NOW()),
        )
        return int(cur.lastrowid)

    def effective(self, user_id: int) -> dict | None:
        """استثنای معتبر فعلی کاربر (در صورت هم‌پوشانی، جدیدترین)."""
        row = self.db.query_one(
            "SELECT * FROM user_exceptions WHERE user_id=? AND starts_at<=? "
            "AND (ends_at IS NULL OR ends_at>=?) ORDER BY created_at DESC LIMIT 1",
            (user_id, NOW(), NOW()),
        )
        return dict(row) if row else None

    def of_user(self, user_id: int) -> list:
        return self.db.query(
            "SELECT * FROM user_exceptions WHERE user_id=? ORDER BY created_at DESC", (user_id,)
        )


class TrialRepo:
    """سابقه دریافت آزمایشی برای جلوگیری از سوءاستفاده (بخش ۱۰.۴)."""

    def __init__(self, db: Database):
        self.db = db

    def grant(self, user_id: int, plan_id: int, source: str = "admin") -> bool:
        """ثبت دریافت آزمایش؛ False یعنی قبلاً دریافت کرده است."""
        cur = self.db.execute(
            "INSERT OR IGNORE INTO trial_grants (user_id, plan_id, source, created_at)"
            " VALUES (?,?,?,?)",
            (user_id, plan_id, source, NOW()),
        )
        return cur.rowcount > 0

    def granted_count(self, user_id: int, plan_id: int | None = None) -> int:
        if plan_id:
            return int(self.db.scalar(
                "SELECT COUNT(*) FROM trial_grants WHERE user_id=? AND plan_id=?",
                (user_id, plan_id)) or 0)
        return int(self.db.scalar(
            "SELECT COUNT(*) FROM trial_grants WHERE user_id=?", (user_id,)) or 0)


class BackupRepo:
    """ثبت وضعیت پشتیبان‌گیری برای مرکز سلامت (§۳۳ و §۴۲ سند ۲).

    «وضعیت آخرین پشتیبان‌گیری» یکی از الزام‌های صریح مرکز سلامت است؛
    بدون این جدول فقط یک مقدار ثابت نمایش داده می‌شد که سند آن را
    «معتبر» نمی‌داند.
    """

    def __init__(self, db: Database):
        self.db = db

    def record(self, path: str, size: int, ok: bool, note: str | None = None) -> None:
        # فقط ۲۰ رکورد آخر نگه داشته می‌شود (سیاست نگهداری §۶۵)
        self.db.execute(
            "INSERT INTO backup_log (path, size_bytes, status, note, created_at)"
            " VALUES (?,?,?,?,?)",
            (path, size, "ok" if ok else "failed", note, NOW()),
        )
        self.db.execute(
            "DELETE FROM backup_log WHERE id NOT IN "
            "(SELECT id FROM backup_log ORDER BY created_at DESC LIMIT 20)"
        )

    def last(self):
        return self.db.query_one(
            "SELECT * FROM backup_log WHERE status='ok' ORDER BY created_at DESC LIMIT 1"
        )
