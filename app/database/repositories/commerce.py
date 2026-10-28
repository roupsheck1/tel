"""Repository های تجاری: پلن‌ها، نسخه‌ها، اشتراک‌ها، سفارش‌ها، پرداخت، کدها، کمپین‌ها."""
from __future__ import annotations

import json
import secrets
import time
import uuid

from ..database import Database

NOW = lambda: int(time.time())  # noqa: E731


# ---------------------------- پلن‌ها و نسخه‌ها ----------------------------
class PlansRepo:
    def __init__(self, db: Database):
        self.db = db

    def create(self, name: str, title: str, description: str, config: dict, created_by: int,
               changelog: str = "ایجاد اولیه") -> int:
        """ایجاد پلن + نسخه پیش‌نویس ۱ (بخش ۸.۱)."""
        with self.db.tx() as conn:
            cur = conn.execute(
                "INSERT INTO plans (name, title, description, created_at) VALUES (?,?,?,?)",
                (name, title, description, NOW()),
            )
            plan_id = int(cur.lastrowid)
            cur = conn.execute(
                "INSERT INTO plan_versions (plan_id, version, config, changelog, status, created_by, created_at)"
                " VALUES (?,?,?,?, 'draft', ?, ?)",
                (plan_id, 1, json.dumps(config, ensure_ascii=False), changelog, created_by, NOW()),
            )
            version_id = int(cur.lastrowid)
            conn.execute("UPDATE plans SET current_version_id=? WHERE id=?", (version_id, plan_id))
        return plan_id

    def get(self, plan_id: int):
        return self.db.query_one("SELECT * FROM plans WHERE id=?", (plan_id,))

    def get_by_name(self, name: str):
        return self.db.query_one("SELECT * FROM plans WHERE name=?", (name,))

    def list(self, include_archived: bool = False):
        extra = "" if include_archived else " WHERE is_archived=0"
        return self.db.query(
            f"SELECT * FROM plans{extra} ORDER BY sort_order, id"
        )

    def saleable(self):
        """پلن‌های قابل فروش: نسخه جاری منتشر شده و فعال."""
        rows = self.db.query(
            "SELECT p.*, pv.config, pv.id AS version_id, pv.version FROM plans p "
            "JOIN plan_versions pv ON pv.id=p.current_version_id "
            "WHERE p.is_archived=0 AND pv.status='published' ORDER BY p.sort_order, p.id"
        )
        out = []
        now = NOW()
        for r in rows:
            cfg = json.loads(r["config"])
            if not cfg.get("is_sellable", True):
                continue
            start, end = cfg.get("sale_start"), cfg.get("sale_end")
            if start and now < start:
                continue
            if end and now > end:
                continue
            out.append(r)
        return out

    def version(self, version_id: int):
        return self.db.query_one("SELECT * FROM plan_versions WHERE id=?", (version_id,))

    def current_version(self, plan_id: int):
        return self.db.query_one(
            "SELECT pv.* FROM plans p JOIN plan_versions pv ON pv.id=p.current_version_id "
            "WHERE p.id=?",
            (plan_id,),
        )

    def draft_new_version(self, plan_id: int, config: dict, created_by: int,
                          changelog: str) -> int:
        """ایجاد نسخه جدید پیش‌نویس از روی تنظیمات داده‌شده (بخش ۹.۱)."""
        last = self.db.scalar("SELECT MAX(version) FROM plan_versions WHERE plan_id=?", (plan_id,))
        with self.db.tx() as conn:
            cur = conn.execute(
                "INSERT INTO plan_versions (plan_id, version, config, changelog, status, created_by, created_at)"
                " VALUES (?,?,?,?, 'draft', ?, ?)",
                (plan_id, int(last or 0) + 1, json.dumps(config, ensure_ascii=False), changelog,
                 created_by, NOW()),
            )
            return int(cur.lastrowid)

    def publish_version(self, version_id: int, scope: str = "future") -> dict:
        """انتشار نسخه با دامنه انتخابی (بخش ۹.۲).

        scope: future (فقط خریداران آینده) | current (فقط مشترکان فعلی)
               | both (هر دو گروه)
        """
        v = self.version(version_id)
        if not v or v["status"] != "draft":
            raise ValueError("نسخه پیدا نشد یا قبلاً منتشر شده است.")
        with self.db.tx() as conn:
            conn.execute(
                "UPDATE plan_versions SET status='published', published_at=? WHERE id=?",
                (NOW(), version_id),
            )
            if scope in ("future", "both"):
                conn.execute(
                    "UPDATE plans SET current_version_id=? WHERE id=?", (version_id, v["plan_id"])
                )
            if scope in ("current", "both"):
                conn.execute(
                    "UPDATE subscriptions SET plan_version_id=? WHERE plan_id=? AND status='active'",
                    (version_id, v["plan_id"]),
                )
        return {"plan_id": v["plan_id"], "version": v["version"], "scope": scope}

    def archive_version(self, version_id: int) -> None:
        self.db.execute("UPDATE plan_versions SET status='archived' WHERE id=?", (version_id,))

    def archive_plan(self, plan_id: int) -> bool:
        """حذف منطقی پلن؛ سوابق تاریخی دست‌نخورده می‌ماند (بخش ۸.۱)."""
        return self.db.execute(
            "UPDATE plans SET is_archived=1 WHERE id=?", (plan_id,)
        ).rowcount > 0

    def set_order(self, plan_id: int, sort_order: int) -> None:
        self.db.execute("UPDATE plans SET sort_order=? WHERE id=?", (sort_order, plan_id))

    def affected_subscribers(self, plan_id: int) -> int:
        return int(self.db.scalar(
            "SELECT COUNT(*) FROM subscriptions WHERE plan_id=? AND status='active'", (plan_id,)
        ) or 0)


# ---------------------------- اشتراک‌ها ----------------------------
class SubscriptionsRepo:
    def __init__(self, db: Database):
        self.db = db

    def create(self, user_id: int, plan_id: int, plan_version_id: int, duration_seconds: int,
               source: str, source_ref: str | None = None, starts_at: int | None = None) -> int:
        """ایجاد اشتراک با سیاست تمدید (بخش ۱۳.۹).

        اشتراک فعال → افزودن به انتهای اعتبار فعلی؛ منقضی → شروع از الان.
        """
        now = NOW()
        current = self.active_of(user_id)
        if current:
            start = max(now, current["expires_at"])
            self._insert_extension(current["id"], starts_at or start, duration_seconds)
            sub_id = current["id"]
        else:
            start = starts_at or now
            with self.db.tx() as conn:
                cur = conn.execute(
                    "INSERT INTO subscriptions (user_id, plan_id, plan_version_id, status, "
                    "starts_at, expires_at, source, source_ref, created_at) "
                    "VALUES (?,?,?,?,?,?,?, ?,?)",
                    (user_id, plan_id, plan_version_id, "active", start,
                     start + duration_seconds, source, source_ref, now),
                )
                sub_id = int(cur.lastrowid)
        self._expire_stale(user_id)
        return sub_id

    def _insert_extension(self, subscription_id: int, from_ts: int, seconds: int) -> None:
        """تمدید اشتراک فعال و ثبت سابقه تمدید در یک تراکنش."""
        with self.db.tx() as conn:
            row = conn.execute(
                "SELECT id, expires_at FROM subscriptions WHERE id=? AND status='active'",
                (subscription_id,),
            ).fetchone()
            if not row:
                raise ValueError("اشتراک فعال نیست.")
            new_expiry = max(NOW(), row["expires_at"]) + seconds
            conn.execute(
                "UPDATE subscriptions SET expires_at=? WHERE id=?", (new_expiry, subscription_id)
            )
            conn.execute(
                "INSERT INTO subscription_extensions (subscription_id, added_seconds, reason, created_at)"
                " VALUES (?,?,?,?)",
                (subscription_id, seconds, "extension", NOW()),
            )

    def active_of(self, user_id: int):
        return self.db.query_one(
            "SELECT * FROM subscriptions WHERE user_id=? AND status='active' AND starts_at<=? "
            "ORDER BY expires_at DESC LIMIT 1",
            (user_id, NOW()),
        )

    def _expire_stale(self, user_id: int) -> None:
        self.db.execute(
            "UPDATE subscriptions SET status='expired' WHERE user_id=? AND status='active' "
            "AND expires_at<=?",
            (user_id, NOW()),
        )

    def sweep_expired(self) -> int:
        """منقضی‌سازی تنبل اشتراک‌های سررسیدشده."""
        return self.db.execute(
            "UPDATE subscriptions SET status='expired' WHERE status='active' AND expires_at<=?",
            (NOW(),),
        ).rowcount

    def cancel(self, subscription_id: int, by: int, reason: str | None = None) -> bool:
        return self.db.execute(
            "UPDATE subscriptions SET status='canceled', canceled_at=?, canceled_by=?, "
            "suspend_reason=? WHERE id=? AND status IN ('active','suspended')",
            (NOW(), by, reason, subscription_id),
        ).rowcount > 0

    def suspend(self, subscription_id: int, reason: str) -> bool:
        return self.db.execute(
            "UPDATE subscriptions SET status='suspended', suspend_reason=? WHERE id=? AND status='active'",
            (reason, subscription_id),
        ).rowcount > 0

    def resume(self, subscription_id: int) -> bool:
        return self.db.execute(
            "UPDATE subscriptions SET status='active', suspend_reason=NULL WHERE id=? "
            "AND status='suspended' AND expires_at>?",
            (subscription_id, NOW()),
        ).rowcount > 0

    def get(self, subscription_id: int):
        return self.db.query_one("SELECT * FROM subscriptions WHERE id=?", (subscription_id,))

    def history_of(self, user_id: int, limit: int = 20):
        return self.db.query(
            "SELECT s.*, p.title AS plan_title FROM subscriptions s "
            "JOIN plans p ON p.id=s.plan_id WHERE s.user_id=? ORDER BY s.created_at DESC LIMIT ?",
            (user_id, limit),
        )

    def stats(self) -> dict:
        return {
            "active": int(self.db.scalar("SELECT COUNT(*) FROM subscriptions WHERE status='active'") or 0),
            "expired": int(self.db.scalar("SELECT COUNT(*) FROM subscriptions WHERE status='expired'") or 0),
            "canceled": int(self.db.scalar("SELECT COUNT(*) FROM subscriptions WHERE status='canceled'") or 0),
        }


# ---------------------------- روش‌های پرداخت ----------------------------
class PaymentMethodsRepo:
    def __init__(self, db: Database):
        self.db = db

    def create(self, title: str, card_number: str, holder_name: str | None, bank_name: str | None,
               instructions: str | None, note: str | None = None) -> int:
        cur = self.db.execute(
            "INSERT INTO payment_methods (title, card_number, holder_name, bank_name, instructions,"
            " note, created_at) VALUES (?,?,?,?,?,?,?)",
            (title, card_number, holder_name, bank_name, instructions, note, NOW()),
        )
        return int(cur.lastrowid)

    def get_active(self, method_id: int | None = None):
        if method_id:
            return self.db.query_one("SELECT * FROM payment_methods WHERE id=? AND is_active=1",
                                     (method_id,))
        return self.db.query_one("SELECT * FROM payment_methods WHERE is_active=1 ORDER BY id LIMIT 1")

    def list(self):
        return self.db.query("SELECT * FROM payment_methods ORDER BY is_active DESC, id")

    def set_active(self, method_id: int, active: bool) -> None:
        self.db.execute("UPDATE payment_methods SET is_active=? WHERE id=?", (int(active), method_id))

    def update(self, method_id: int, **fields) -> None:
        allowed = ("title", "card_number", "holder_name", "bank_name", "instructions", "note")
        sets, args = [], []
        for k, v in fields.items():
            if k in allowed and v is not None:
                sets.append(f"{k}=?")
                args.append(v)
        if sets:
            args.append(method_id)
            self.db.execute(f"UPDATE payment_methods SET {', '.join(sets)} WHERE id=?", args)


# ---------------------------- سفارش‌ها ----------------------------
ORDER_STATUSES = (
    "awaiting_payment", "payment_announced", "receipt_received", "under_review",
    "confirmed", "rejected", "expired", "canceled", "needs_review",
)


class OrdersRepo:
    def __init__(self, db: Database):
        self.db = db

    def create(self, user_id: int, plan_row, amount: int, duration_seconds: int,
               method_row, ttl_seconds: int = 86400) -> str:
        """ایجاد سفارش یکتا با اسنپ‌شات پرداخت و پلن (بخش ۱۳.۱ و ۱۳.۳)."""
        order_id = uuid.uuid4().hex[:12]
        snapshot = {
            "title": method_row["title"],
            "card_number": method_row["card_number"],
            "holder_name": method_row["holder_name"],
            "bank_name": method_row["bank_name"],
            "instructions": method_row["instructions"],
        }
        self.db.execute(
            "INSERT INTO orders (id, user_id, plan_id, plan_version_id, amount, duration_seconds,"
            " status, payment_method_id, payment_snapshot, created_at, expires_at)"
            " VALUES (?,?,?,?,?,?, 'awaiting_payment', ?,?,?,?)",
            (order_id, user_id, plan_row["id"], plan_row["version_id"] if "version_id" in plan_row.keys()
             else plan_row["current_version_id"], amount, duration_seconds,
             method_row["id"], json.dumps(snapshot, ensure_ascii=False), NOW(), NOW() + ttl_seconds),
        )
        return order_id

    def get(self, order_id: str):
        return self.db.query_one(
            "SELECT o.*, p.title AS plan_title, u.username FROM orders o "
            "JOIN plans p ON p.id=o.plan_id LEFT JOIN users u ON u.telegram_id=o.user_id "
            "WHERE o.id=?",
            (order_id,),
        )

    def open_of_user(self, user_id: int):
        """آخرین سفارش باز کاربر (قابل اعلام پرداخت/ارسال رسید)."""
        return self.db.query_one(
            "SELECT * FROM orders WHERE user_id=? AND status IN "
            "('awaiting_payment','payment_announced','receipt_received','under_review','needs_review') "
            "AND expires_at>? ORDER BY created_at DESC LIMIT 1",
            (user_id, NOW()),
        )

    def list(self, status: str | None = None, user_id: int | None = None,
             limit: int = 20, offset: int = 0):
        cond, args = [], []
        if status:
            cond.append("o.status=?")
            args.append(status)
        if user_id:
            cond.append("o.user_id=?")
            args.append(user_id)
        where = (" WHERE " + " AND ".join(cond)) if cond else ""
        args += [limit, offset]
        return self.db.query(
            "SELECT o.*, p.title AS plan_title, u.username FROM orders o "
            "JOIN plans p ON p.id=o.plan_id LEFT JOIN users u ON u.telegram_id=o.user_id"
            f"{where} ORDER BY o.created_at DESC LIMIT ? OFFSET ?",
            args,
        )

    def _transition(self, order_id: str, from_statuses: tuple, new_status: str,
                    extra_sql: str = "", extra_args: tuple = ()) -> bool:
        with self.db.tx() as conn:
            cur = conn.execute(
                f"UPDATE orders SET status=?{extra_sql} WHERE id=? AND status IN "
                f"({','.join('?' * len(from_statuses))})",
                (new_status, *extra_args, order_id, *from_statuses),
            )
            return cur.rowcount > 0

    def announce_payment(self, order_id: str) -> bool:
        return self._transition(
            order_id, ("awaiting_payment", "payment_announced"), "payment_announced",
            ", announced_at=?", (NOW(),)
        )

    def attach_receipt(self, order_id: str, file_id: str, note: str | None = None) -> bool:
        with self.db.tx() as conn:
            cur = conn.execute(
                "UPDATE orders SET status='receipt_received', receipt_file_id=?, "
                "receipt_received_at=? WHERE id=? AND status IN "
                "('awaiting_payment','payment_announced','receipt_received')",
                (file_id, NOW(), order_id),
            )
            if cur.rowcount:
                conn.execute(
                    "INSERT INTO receipts (order_id, file_id, note, received_at) VALUES (?,?,?,?)",
                    (order_id, file_id, note, NOW()),
                )
        return bool(cur.rowcount)

    def start_review(self, order_id: str) -> bool:
        return self._transition(
            order_id, ("receipt_received", "under_review"), "under_review", ", review_started_at=?",
            (NOW(),)
        )

    def needs_review(self, order_id: str) -> bool:
        return self._transition(order_id, tuple(ORDER_STATUSES), "needs_review")

    def reject(self, order_id: str, by: int, reason: str) -> bool:
        """رد سفارش با ثبت دلیل (بخش ۱۳.۷)؛ idempotent از طریق گارد وضعیت."""
        return self._transition(
            order_id, ("awaiting_payment", "payment_announced", "receipt_received",
                       "under_review", "needs_review"),
            "rejected", ", rejected_at=?, rejected_by=?, rejection_reason=?",
            (NOW(), by, reason),
        )

    def cancel_by_user(self, order_id: str, user_id: int) -> bool:
        with self.db.tx() as conn:
            cur = conn.execute(
                "UPDATE orders SET status='canceled' WHERE id=? AND user_id=? AND status IN "
                "('awaiting_payment','payment_announced')",
                (order_id, user_id),
            )
            return cur.rowcount > 0

    def confirm(self, order_id: str, by: int) -> tuple[bool, int | None]:
        """تأیید پرداخت + فعال‌سازی اشتراک در یک تراکنش اتمیک (بخش ۱۳.۸ و ۲۱.۲).

        گارد وضعیت تضمین می‌کند تأیید چندباره فقط یک اشتراک بسازد.
        خروجی: (موفق، شناسه اشتراک)
        """
        with self.db.tx() as conn:
            row = conn.execute(
                "SELECT * FROM orders WHERE id=?", (order_id,)
            ).fetchone()
            if not row:
                return False, None
            if row["status"] == "confirmed":
                return False, None  # تأیید تکراری
            if row["status"] not in ("receipt_received", "under_review", "needs_review",
                                     "payment_announced", "awaiting_payment"):
                return False, None
            # فعال‌سازی اشتراک داخل همان تراکنش
            now = NOW()
            active = conn.execute(
                "SELECT id, expires_at FROM subscriptions WHERE user_id=? AND status='active'",
                (row["user_id"],),
            ).fetchone()
            if active:
                start = max(now, active["expires_at"])
                cur = conn.execute(
                    "UPDATE subscriptions SET expires_at=expires_at+?, plan_id=?, plan_version_id=?"
                    " WHERE id=?",
                    (row["duration_seconds"], row["plan_id"], row["plan_version_id"], active["id"]),
                )
                conn.execute(
                    "INSERT INTO subscription_extensions (subscription_id, added_seconds, reason,"
                    " created_by, created_at) VALUES (?,?,?,?,?)",
                    (active["id"], row["duration_seconds"], f"order:{order_id}", by, now),
                )
                sub_id = active["id"]
            else:
                start = now
                cur = conn.execute(
                    "INSERT INTO subscriptions (user_id, plan_id, plan_version_id, status, starts_at,"
                    " expires_at, source, source_ref, created_at) VALUES (?,?,?,?,?,?, 'purchase', ?,?)",
                    (row["user_id"], row["plan_id"], row["plan_version_id"], "active", start,
                     start + row["duration_seconds"], order_id, now),
                )
                sub_id = int(cur.lastrowid)
            conn.execute(
                "UPDATE orders SET status='confirmed', confirmed_at=?, confirmed_by=?, subscription_id=?"
                " WHERE id=? AND status=?",
                (now, by, sub_id, order_id, row["status"]),
            )
            return True, sub_id

    def sweep_expired(self) -> int:
        return self.db.execute(
            "UPDATE orders SET status='expired' WHERE status IN "
            "('awaiting_payment','payment_announced') AND expires_at<=?",
            (NOW(),),
        ).rowcount

    def count(self, status: str | None = None) -> int:
        """تعداد سفارش‌ها (با فیلتر وضعیت اختیاری) — برای صفحه‌بندی پنل."""
        if status:
            return int(self.db.scalar(
                "SELECT COUNT(*) FROM orders WHERE status=?", (status,)) or 0)
        return int(self.db.scalar("SELECT COUNT(*) FROM orders") or 0)

    def stats(self) -> dict:
        def cnt(status):
            return int(self.db.scalar("SELECT COUNT(*) FROM orders WHERE status=?", (status,)) or 0)
        revenue = int(self.db.scalar(
            "SELECT COALESCE(SUM(amount),0) FROM orders WHERE status='confirmed'"
        ) or 0)
        avg_review = self.db.scalar(
            "SELECT AVG(confirmed_at - receipt_received_at) FROM orders "
            "WHERE confirmed_at IS NOT NULL AND receipt_received_at IS NOT NULL"
        )
        return {
            "total": int(self.db.scalar("SELECT COUNT(*) FROM orders") or 0),
            "confirmed": cnt("confirmed"),
            "pending": cnt("receipt_received") + cnt("under_review") + cnt("needs_review"),
            "rejected": cnt("rejected"),
            "canceled": cnt("canceled"),
            "expired": cnt("expired"),
            "revenue": revenue,
            "avg_review_seconds": round(float(avg_review)) if avg_review is not None else None,
        }

    def sales_by_plan(self):
        return self.db.query(
            "SELECT p.title, COUNT(*) AS cnt, COALESCE(SUM(o.amount),0) AS total FROM orders o "
            "JOIN plans p ON p.id=o.plan_id WHERE o.status='confirmed' GROUP BY p.id ORDER BY total DESC"
        )

    def sales_between(self, start: int, end: int):
        return self.db.query(
            "SELECT COUNT(*) AS cnt, COALESCE(SUM(amount),0) AS total FROM orders "
            "WHERE status='confirmed' AND confirmed_at BETWEEN ? AND ?",
            (start, end),
        )[0]


# ---------------------------- کدهای اشتراک ----------------------------
class PromoCodesRepo:
    def __init__(self, db: Database):
        self.db = db

    def generate(self, plan_id: int, duration_seconds: int, max_uses: int | None,
                 expires_at: int | None, kind: str = "gift", allowed_user_id: int | None = None,
                 batch_id: str | None = None, note: str | None = None,
                 created_by: int | None = None, count: int = 1) -> list[str]:
        """ساخت تکی یا دسته‌ای کد یکتا (بخش ۱۴.۲)."""
        codes = []
        for _ in range(count):
            while True:
                code = "IG-" + secrets.token_hex(4).upper()
                if not self.db.query_one("SELECT 1 FROM promo_codes WHERE code=?", (code,)):
                    break
            self.db.execute(
                "INSERT INTO promo_codes (code, plan_id, duration_seconds, max_uses, expires_at,"
                " allowed_user_id, batch_id, kind, note, created_by, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (code, plan_id, duration_seconds, max_uses, expires_at, allowed_user_id,
                 batch_id, kind, note, created_by, NOW()),
            )
            codes.append(code)
        return codes

    def get(self, code: str):
        return self.db.query_one("SELECT * FROM promo_codes WHERE code=?", (code,))

    def redeem(self, code: str, user_id: int) -> tuple[bool, str, int | None]:
        """استفاده از کد در تراکنش + جلوگیری از مصرف تکراری (بخش ۱۴.۳)."""
        with self.db.tx() as conn:
            row = conn.execute("SELECT * FROM promo_codes WHERE code=?", (code,)).fetchone()
            if not row or row["status"] != "active":
                return False, "کد وجود ندارد یا غیرفعال است.", None
            if row["expires_at"] and row["expires_at"] < NOW():
                return False, "کد منقضی شده است.", None
            if row["allowed_user_id"] and row["allowed_user_id"] != user_id:
                return False, "این کد برای کاربر دیگری ساخته شده است.", None
            if row["max_uses"] is not None and row["used_count"] >= row["max_uses"]:
                return False, "ظرفیت استفاده از این کد پر شده است.", None
            already = conn.execute(
                "SELECT 1 FROM promo_code_uses WHERE code_id=? AND user_id=?",
                (row["id"], user_id),
            ).fetchone()
            if already:
                return False, "شما قبلاً از این کد استفاده کرده‌اید.", None
            # فعال‌سازی اشتراک داخل همان تراکنش
            now = NOW()
            active = conn.execute(
                "SELECT id, expires_at FROM subscriptions WHERE user_id=? AND status='active'",
                (user_id,),
            ).fetchone()
            if active:
                conn.execute(
                    "UPDATE subscriptions SET expires_at=expires_at+? WHERE id=?",
                    (row["duration_seconds"], active["id"]),
                )
                sub_id = active["id"]
            else:
                cur = conn.execute(
                    "INSERT INTO subscriptions (user_id, plan_id, plan_version_id, status, starts_at,"
                    " expires_at, source, source_ref, created_at) VALUES (?,?,?,?,?,?, 'code', ?,?)",
                    (user_id, row["plan_id"], self._current_version_id(conn, row["plan_id"]),
                     "active", now, now + row["duration_seconds"], row["code"], now),
                )
                sub_id = int(cur.lastrowid)
            conn.execute(
                "INSERT INTO promo_code_uses (code_id, user_id, subscription_id, used_at)"
                " VALUES (?,?,?,?)",
                (row["id"], user_id, sub_id, now),
            )
            conn.execute("UPDATE promo_codes SET used_count=used_count+1 WHERE id=?", (row["id"],))
            if row["max_uses"] is not None and row["used_count"] + 1 >= row["max_uses"]:
                conn.execute("UPDATE promo_codes SET status='exhausted' WHERE id=?", (row["id"],))
        return True, "اشتراک با موفقیت فعال شد.", sub_id

    def _current_version_id(self, conn, plan_id: int) -> int:
        return conn.execute(
            "SELECT current_version_id FROM plans WHERE id=?", (plan_id,)
        ).fetchone()[0]

    def cancel_batch(self, batch_id: str) -> int:
        """لغو گروهی کدهای استفاده‌نشده؛ اشتراک‌های فعال‌شده باقی می‌مانند (بخش ۱۴.۳)."""
        return self.db.execute(
            "UPDATE promo_codes SET status='canceled' WHERE batch_id=? AND status='active' "
            "AND used_count=0",
            (batch_id,),
        ).rowcount

    def cancel(self, code: str) -> bool:
        return self.db.execute(
            "UPDATE promo_codes SET status='canceled' WHERE code=? AND status='active' AND used_count=0",
            (code,),
        ).rowcount > 0

    def list(self, limit: int = 30, offset: int = 0):
        return self.db.query(
            "SELECT pc.*, p.title AS plan_title FROM promo_codes pc "
            "JOIN plans p ON p.id=pc.plan_id ORDER BY pc.created_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        )

    def uses_of(self, code_id: int):
        return self.db.query(
            "SELECT u.*, us.telegram_id AS user_id_, us.username FROM promo_code_uses u "
            "JOIN users us ON us.telegram_id=u.user_id WHERE u.code_id=? ORDER BY u.used_at DESC",
            (code_id,),
        )


# ---------------------------- کمپین‌ها ----------------------------
class CampaignsRepo:
    def __init__(self, db: Database):
        self.db = db

    def create(self, name: str, kind: str, plan_id: int | None, target_group_id: int | None,
               starts_at: int | None, ends_at: int | None, discount_percent: int | None,
               discount_amount: int | None, max_uses: int | None, per_user_limit: int,
               message_text: str | None, created_by: int) -> int:
        cur = self.db.execute(
            "INSERT INTO campaigns (name, kind, plan_id, target_group_id, starts_at, ends_at,"
            " discount_percent, discount_amount, max_uses, per_user_limit, message_text, status,"
            " created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?, 'draft', ?,?)",
            (name, kind, plan_id, target_group_id, starts_at, ends_at, discount_percent,
             discount_amount, max_uses, per_user_limit, message_text, created_by, NOW()),
        )
        return int(cur.lastrowid)

    def get(self, campaign_id: int):
        return self.db.query_one("SELECT * FROM campaigns WHERE id=?", (campaign_id,))

    def list(self):
        return self.db.query("SELECT * FROM campaigns ORDER BY created_at DESC")

    def set_status(self, campaign_id: int, status: str) -> bool:
        return self.db.execute(
            "UPDATE campaigns SET status=? WHERE id=?", (status, campaign_id)
        ).rowcount > 0

    def increment_use(self, campaign_id: int) -> None:
        self.db.execute("UPDATE campaigns SET used_count=used_count+1 WHERE id=?", (campaign_id,))

    def active_trial_for(self, plan_id: int) -> dict | None:
        """کمپین آزمایشی فعال برای یک پلن (بخش ۱۰.۵)."""
        now = NOW()
        row = self.db.query_one(
            "SELECT * FROM campaigns WHERE kind='trial' AND plan_id=? AND status='active' "
            "AND (starts_at IS NULL OR starts_at<=?) AND (ends_at IS NULL OR ends_at>=?) "
            "AND (max_uses IS NULL OR used_count<max_uses)",
            (plan_id, now, now),
        )
        return dict(row) if row else None
