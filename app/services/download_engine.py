"""موتور مدیریت دانلود و صف پایدار (بخش ۱۵ سند).

سه محدودیت مستقل هر کاربر (بخش ۱۵.۳):
1. دانلود هم‌زمان (concurrent_downloads)
2. زمان انتظار (wait_seconds) با نقطه شروع قابل تنظیم (بخش ۱۵.۵)
3. سهمیه مصرف (ساعتی/روزانه/کل دوره)

ظرفیت کلی سرور (بخش ۱۵.۸) و اولویت‌بندی منصفانه با افزایش سن درخواست
(بخش ۱۵.۹) نیز در همین ماژول اعمال می‌شوند. صف در پایگاه داده پایدار است
و پس از راه‌اندازی مجدد بازیابی می‌شود (بخش ۱۵.۱۴).
"""
from __future__ import annotations

import asyncio
import html as _html
import json
import logging
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Protocol

from ..database.repositories import (
    DownloadsRepo, ErrorsRepo, ExceptionsRepo, PlansRepo, SubscriptionsRepo, UsersRepo,
)
from .plans import PlanConfig
from .settings import (
    SettingsService, WAIT_FROM_DOWNLOAD_END, WAIT_FROM_DOWNLOAD_START, WAIT_FROM_SEND_SUCCESS,
)

log = logging.getLogger("download_engine")
NOW = lambda: int(time.time())  # noqa: E731


class ContentDownloader(Protocol):
    """قرارداد دریافت محتوا (جدا از پیاده‌سازی اینستاگرام — بخش ۴.۲)."""

    def download(self, url: str, shortcode: str, dest_dir: Path,
                 kind: str = "post") -> list[Path]: ...


class DeliverySender(Protocol):
    """قرارداد ارسال فایل به تلگرام."""

    async def send(self, user_id: int, path: Path) -> bool: ...


@dataclass
class Limits:
    """محدودیت‌های مؤثر یک کاربر پس از ترکیب پلن/حالت رایگان/استثنا."""
    concurrent_downloads: int = 1
    wait_seconds: int = 60
    queue_limit: int = 3
    hourly_quota: int | None = None
    daily_quota: int | None = None
    total_quota: int | None = None
    max_file_mb: int = 50
    priority: int = 0


@dataclass
class EngineDeps:
    downloads: DownloadsRepo
    users: UsersRepo
    subs: SubscriptionsRepo
    plans: PlansRepo
    exceptions: ExceptionsRepo
    errors: ErrorsRepo
    settings: SettingsService
    downloader: ContentDownloader
    sender: DeliverySender
    notify: Callable[[int, str], Awaitable[None]]
    temp_dir: Path
    plan_config_of_version: Callable[[int], PlanConfig] = field(default=None)


class DownloadEngine:
    def __init__(self, deps: EngineDeps):
        self.d = deps
        self._running = False
        self._scheduler_task: asyncio.Task | None = None
        self._cleanup_task: asyncio.Task | None = None
        self._workers: set[asyncio.Task] = set()
        # شناسه درخواست‌هایی که تسکشان همین الان ساخته شده ولی هنوز وضعیتشان
        # در پایگاه داده «downloading» نشده است. بدون این مجموعه، حلقه
        # زمان‌بند بی‌هیچ نقطه await همان درخواست را دوباره و دوباره برمی‌داشت،
        # بی‌نهایت تسک تکراری می‌ساخت و حلقه رویداد (و کل ربات) فریز می‌شد.
        self._inflight: set[int] = set()
        self._wake = asyncio.Event()
        self._send_sem = asyncio.Semaphore(int(deps.settings.get("capacity.max_active_sends")))
        self._stop_broadcast: bool = False

    # ============================ ثبت درخواست ============================
    async def submit(self, user_id: int, url: str, shortcode: str,
                     media_kind: str = "post") -> tuple[bool, int | str]:
        """دریافت درخواست جدید با همه بررسی‌های ورودی (گام‌های ۱ تا ۹ بخش ۱۵.۴)."""
        from .users import UserService  # جلوگیری از import دور

        user_svc = UserService(self.d.users, _NoLinks(), self.d.subs, self.d.settings)
        access = user_svc.check_access(user_id)
        if not access:
            return False, access.reason

        # حالت نگهداری (§۵۴ سند ۲): مدیر تعیین می‌کند درخواست جدید ثبت نشود
        if self.d.settings.is_maintenance and \
                self.d.settings.get("maintenance.block_new_requests", True):
            return False, "سامانه در حالت نگهداری است. کمی بعد دوباره بفرست."

        # کلید قابلیت استوری (§۵۳ سند ۲): مدیر بتواند فقط دریافت استوری را خاموش کند
        if media_kind == "story" and not self.d.settings.get("feature.story_download", True):
            return False, "دریافت استوری موقتاً غیرفعال است."

        limits = self.effective_limits(user_id)
        if self.d.downloads.queued_of_user(user_id) >= limits.queue_limit:
            return False, "صف درخواست‌های شما پر است. کمی صبر کن تا فعلی‌ها تمام شوند."
        if self.d.downloads.global_queued() >= int(self.d.settings.get("capacity.max_queue_total")):
            return False, "ظرفیت کلی سامانه پر است. کمی بعد دوباره بفرست."
        request_id = self.d.downloads.create(user_id, shortcode, url, media_kind,
                                             priority=limits.priority)
        self._wake.set()
        return True, request_id

    # ============================ محدودیت‌های مؤثر ============================
    def effective_limits(self, user_id: int) -> Limits:
        """ترکیب: حالت رایگان یا پیکربندی پلن اشتراک فعال + استثنای فردی (۱۵.۱۰)."""
        if self.d.settings.sales_mode == "free":
            base = self.d.settings.free_mode_config
            limits = Limits(
                concurrent_downloads=base.get("concurrent_downloads", 1),
                wait_seconds=base.get("wait_seconds", 60),
                queue_limit=base.get("queue_limit", 3),
                hourly_quota=base.get("hourly_quota"),
                daily_quota=base.get("daily_quota"),
                total_quota=base.get("total_quota"),
                max_file_mb=base.get("max_file_mb", 50),
                priority=base.get("priority", 0),
            )
        else:
            sub = self.d.subs.active_of(user_id)
            if not sub:
                return Limits(queue_limit=0)  # بدون اشتراک، صف صفر
            # پیکربندی از نسخه‌ای که اشتراک به آن اشاره می‌کند (نه لزوماً نسخه جاری)
            version_id = sub["plan_version_id"]
            limits = self._limits_from_version(version_id)
        # اعمال استثنای فردی معتبر
        exc = self.d.exceptions.effective(user_id)
        if exc:
            for key in ("concurrent_downloads", "wait_seconds", "hourly_quota", "daily_quota",
                        "total_quota", "priority"):
                if exc.get(key) is not None:
                    setattr(limits, key, exc[key])
        return limits

    def _limits_from_version(self, version_id: int) -> Limits:
        try:
            cfg = self.d.plan_config_of_version(version_id)
        except Exception:
            cfg = None
        if cfg is None:
            return Limits()
        return Limits(
            concurrent_downloads=cfg.concurrent_downloads,
            wait_seconds=cfg.wait_seconds,
            queue_limit=cfg.queue_limit,
            hourly_quota=cfg.hourly_quota,
            daily_quota=cfg.daily_quota,
            total_quota=cfg.total_quota,
            max_file_mb=cfg.max_file_mb,
            priority=cfg.priority,
        )

    # ============================ زمان‌بند ============================
    async def start(self) -> None:
        """راه‌اندازی زمان‌بند و پاک‌ساز؛ بازیابی صف نیز اینجا انجام می‌شود."""
        self._running = True
        report = self.d.downloads.recover_on_startup()
        if report["reset_to_queue"]:
            log.info("بازیابی صف: %s درخواست به صف بازگشت", report["reset_to_queue"])
        self._scheduler_task = asyncio.create_task(self._scheduler_loop())
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())

    async def stop(self) -> None:
        self._running = False
        self._wake.set()
        for task in (self._scheduler_task, self._cleanup_task):
            if task:
                task.cancel()
        for w in list(self._workers):
            w.cancel()

    async def _scheduler_loop(self) -> None:
        while self._running:
            try:
                picked = self._pick_next()
                if picked is None:
                    self._wake.clear()
                    try:
                        await asyncio.wait_for(self._wake.wait(), timeout=2.0)
                    except asyncio.TimeoutError:
                        pass
                    continue
                task = asyncio.create_task(self._process(picked))
                self._workers.add(task)
                self._inflight.add(picked["id"])
                task.add_done_callback(
                    lambda t, rid=picked["id"]: (self._workers.discard(t), self._inflight.discard(rid))
                )
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.exception("خطای زمان‌بند: %s", e)
                await asyncio.sleep(2)

    def _pick_next(self) -> dict | None:
        """انتخاب درخواست واجد شرایط بعدی.

        ترتیب: امتیاز = اولویت × ۱۰ + سن درخواست (دقیقه). افزایش سن باعث
        جلوگیری از گرسنگی درخواست‌های کم‌اولویت می‌شود (بخش ۱۵.۹).
        """
        # حالت نگهداری (§۵۴ سند ۲): مدیر تعیین می‌کند درخواست‌های قبلی ادامه پیدا کنند یا نه
        if self.d.settings.is_maintenance and \
                not self.d.settings.get("maintenance.continue_queue", True):
            return None
        queued = self.d.downloads.queued(limit=200)
        if not queued:
            return None
        if self.d.downloads.global_active() >= int(self.d.settings.get("capacity.max_active_downloads")):
            return None
        now = NOW()
        window = int(self.d.settings.get("downloads.processing_window"))
        candidates = []
        for r in queued:
            # درخواستی که همین حالا تسکش ساخته شده ولی هنوز شروع نشده
            if r["id"] in self._inflight:
                continue
            # انقضای درخواست معلق در صف (بخش ۱۵.۱۱)
            if now - r["created_at"] > window:
                self.d.downloads.set_status(r["id"], "expired", finished_at=now,
                                            last_error="انقضای در صف", error_stage="queue")
                continue
            why = self._ineligibility_reason(r["user_id"])
            if why:
                continue
            age_min = (now - r["created_at"]) / 60
            score = r["priority"] * 10 + min(age_min, 30)
            candidates.append((score, r["created_at"], r))
        if not candidates:
            return None
        candidates.sort(key=lambda x: (-x[0], x[1]))
        return dict(candidates[0][2])

    def _ineligibility_reason(self, user_id: int) -> str | None:
        """دلیل عدم احراز شرایط؛ None یعنی واجد شرایط است."""
        limits = self.effective_limits(user_id)
        if limits.queue_limit == 0:
            return "no_subscription"
        if self.d.downloads.active_of_user(user_id) >= limits.concurrent_downloads:
            return "concurrency"
        if limits.wait_seconds > 0:
            cooldown = self.d.downloads.cooldown_until(user_id)
            if cooldown and NOW() < cooldown:
                return "cooldown"
        used = self.d.downloads.quota_used(user_id)
        if limits.hourly_quota is not None and used["hourly"] >= limits.hourly_quota:
            return "hourly_quota"
        if limits.daily_quota is not None and used["daily"] >= limits.daily_quota:
            return "daily_quota"
        if limits.total_quota is not None and used["total"] >= limits.total_quota:
            return "total_quota"
        return None

    # ============================ پردازش ============================
    async def _process(self, request: dict) -> None:
        rid = request["id"]
        user_id = request["user_id"]
        limits = self.effective_limits(user_id)
        dest = Path(self.d.temp_dir) / f"req_{rid}"
        dest.mkdir(parents=True, exist_ok=True)
        stage_download_start = NOW()
        self.d.downloads.set_status(rid, "downloading", started_at=stage_download_start)
        try:
            # --- دریافت محتوا (در نخ جدا برای IO سنگین) ---
            # request ممکن است dict (از صف) یا sqlite3.Row باشد؛ keys() در هر دو کار می‌کند
            media_kind = (request["media_kind"]
                          if "media_kind" in request.keys() else "post") or "post"
            files = await asyncio.to_thread(
                self.d.downloader.download, request["media_url"], request["shortcode"], dest,
                media_kind,
            )
            # فرادادهٔ محتوا (نام کاربری سازنده) برای پیام لینک‌های اینستاگرام؛
            # دانلودرهای فیک (آزمون‌ها) این متد را ندارند → بی‌خطر نادیده گرفته می‌شود
            content_meta: dict = {}
            pop_meta = getattr(self.d.downloader, "pop_meta", None)
            if pop_meta:
                try:
                    content_meta = pop_meta(dest) or {}
                except Exception:
                    content_meta = {}
            stage_download_end = NOW()
            files = [f for f in files if f and Path(f).exists()]
            if not files:
                raise DownloadError("محتوایی برای این لینک یافت نشد (شاید خصوصی یا حذف‌شده باشد).",
                                    stage="download")
            # --- اعتبارسنجی فایل (بخش ۱۵.۱۳) ---
            max_bytes = limits.max_file_mb * 1024 * 1024
            files, skipped = [], files
            for f in skipped:
                if Path(f).stat().st_size <= max_bytes:
                    files.append(f)
                else:
                    log.warning("فایل %s از سقف پلن بزرگ‌تر است و نادیده گرفته شد", f)
            if not files:
                raise DownloadError("حجم فایل از سقف مجاز پلن شما بیشتر است.", stage="validation")
            # کلید قابلیت فایل حجیم (§۵۳ سند ۲): بالای ۵۰ مگابایت نیاز به
            # فشرده‌سازی/Local API دارد؛ اگر مدیر این قابلیت را خاموش کرده باشد
            # درخواست حجیم در همان ابتدا و با پیام روشن رد می‌شود.
            if not self.d.settings.get("feature.large_files", True):
                big = [f for f in files if Path(f).stat().st_size > 50 * 1024 * 1024]
                if big:
                    raise DownloadError(
                        "دریافت فایل حجیم موقتاً غیرفعال است.", stage="validation")
            total_size = sum(Path(f).stat().st_size for f in files)
            self.d.downloads.set_status(rid, "ready_to_send", file_paths=[str(f) for f in files],
                                        file_size=total_size)
            # --- ارسال به تلگرام (گام ۱۲) ---
            self.d.downloads.set_status(rid, "sending")
            async with self._send_sem:
                for f in files:
                    ok = await self.d.sender.send(user_id, Path(f))
                    if not ok:
                        raise SendError("ارسال فایل به تلگرام ناموفق بود.", stage="send")
            # --- لینک‌های اینستاگرام زیر پست (درخواست کاربر ۲۰۲۶-۱۰-۳۱) ---
            # «بازکردن پست در اینستاگرام» + «بازکردن صفحه سازنده پست» به‌صورت
            # متن هایپرلینک؛ لینک پست همان لینک اصلی خود پست است (ساخته نمی‌شود).
            links_text = self._links_message(request, content_meta)
            if links_text:
                await self._safe_notify(user_id, links_text)
            # --- موفقیت: شارژ سهمیه و شروع کوچک‌داون طبق سیاست (۱۵.۵ و ۱۵.۱۲) ---
            now = NOW()
            self.d.downloads.set_status(rid, "completed", finished_at=now,
                                        file_paths=[str(f) for f in files])
            self.d.downloads.charge_quota(rid)
            point = {
                WAIT_FROM_DOWNLOAD_START: stage_download_start,
                WAIT_FROM_DOWNLOAD_END: stage_download_end,
                WAIT_FROM_SEND_SUCCESS: now,
            }.get(self.d.settings.get("downloads.wait_start_point"), now)
            if limits.wait_seconds > 0:
                self.d.downloads.set_cooldown(user_id, point + limits.wait_seconds)
            # اعلان تحویل موفق — قابل خاموش‌کردن از پنل (§۳۱ سند ۲)
            if self.d.settings.get("notify.on_success", True):
                await self._safe_notify(user_id, "✅ فایل شما آماده و ارسال شد.")
        except DownloadError as e:
            await self._handle_failure(request, e, str(dest))
        except SendError as e:
            await self._handle_failure(request, e, str(dest))
        except Exception as e:
            log.exception("خطای غیرمنتظره پردازش درخواست %s", rid)
            self.d.errors.log("internal", "process_request", str(e), request_id=rid,
                              user_id=user_id, stage="process", retry_status="handled")
            await self._handle_failure(request, DownloadError(str(e), stage="other"), str(dest))
        finally:
            shutil.rmtree(dest, ignore_errors=True)

    def _links_message(self, request, content_meta: dict) -> str | None:
        """پیام لینک‌های اینستاگرام بعد از تحویل محتوا.

        • لینک پست: همان URL اصلی که کاربر فرستاده (لینک خودِ پست — نه ساختهٔ ربات).
        • لینک سازنده: آدرس رسمی حساب اینستاگرامِ سازندهٔ پست.
        هر دو به‌صورت متن فارسی کلیک‌شدنی (HTML hyperlink) هستند.
        """
        try:
            post_url = str(request["media_url"] or "").strip()
        except (KeyError, IndexError):
            post_url = ""
        if not post_url:
            return None
        username = str(content_meta.get("username") or "").strip()
        lines = [f'🔗 <a href="{_html.escape(post_url, quote=True)}">بازکردن پست در اینستاگرام</a>']
        if username and username.lower() != "instagram":
            lines.append(
                f'👤 <a href="{_html.escape(f"https://www.instagram.com/{username}/", quote=True)}">'
                f"بازکردن صفحه سازنده پست</a>"
            )
        return "\n".join(lines)

    async def _handle_failure(self, request: dict, err: Exception, dest: str) -> None:
        """سیاست خطا و تلاش مجدد (بخش ۱۵.۱۱ و ۱۵.۱۲).

        تلاش مجدد همان درخواست است (attempts++) و سهمیه‌ای مصرف نمی‌کند.
        شکست دانلود و ارسال تفکیک می‌شوند.
        """
        rid = request["id"]
        user_id = request["user_id"]
        attempts = (request["attempts"] or 0) + 1
        max_retries = int(self.d.settings.get("downloads.max_retries"))
        category = "download" if isinstance(err, DownloadError) else "send"
        if attempts <= max_retries:
            self.d.downloads.set_status(rid, "queued", attempts=attempts,
                                        last_error=str(err), error_stage=err.stage if isinstance(err, EngineError) else category)
            self.d.errors.log(category, "attempt_failed", str(err), request_id=rid,
                              user_id=user_id, stage=getattr(err, "stage", category),
                              retry_status=f"retry {attempts}/{max_retries}")
            self._wake.set()
        else:
            self.d.downloads.set_status(rid, "failed", finished_at=NOW(), attempts=attempts,
                                        last_error=str(err),
                                        error_stage=getattr(err, "stage", category))
            self.d.errors.log(category, "final_failure", str(err), request_id=rid,
                              user_id=user_id, stage=getattr(err, "stage", category),
                              retry_status="exhausted")
            # فقط خط اول خطا به کاربر نشان داده می‌شود (خطاهای کتابخانه‌ای
            # چندخطی مثل اعتبارسنجی pydantic برای کاربر بی‌معنا و گی‌کننده‌اند)
            err_text = str(err).strip() or "خطای نامشخص"
            first_line = err_text.splitlines()[0]
            # اعلان شکست — قابل خاموش‌کردن از پنل (§۳۱ سند ۲)
            if self.d.settings.get("notify.on_failure", True):
                await self._safe_notify(
                    user_id,
                    f"❌ پردازش درخواست شما با خطا مواجه شد:\n{first_line}\n"
                    "در صورت اطمینان از عمومی‌بودن پست، بعداً دوباره بفرست."
                )

    async def _safe_notify(self, user_id: int, text: str) -> None:
        try:
            await self.d.notify(user_id, text)
        except Exception as e:
            log.warning("اطلاع‌رسانی به %s ناموفق: %s", user_id, e)

    # ============================ پاک‌سازی (بخش ۱۵.۱۳) ============================
    async def _cleanup_loop(self) -> None:
        retention = int(self.d.settings.get("downloads.cleanup_retention"))
        while self._running:
            try:
                await asyncio.sleep(max(60, retention))
                self._cleanup_temp(retention)
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.exception("خطای پاک‌ساز: %s", e)
                await asyncio.sleep(60)

    def _cleanup_temp(self, retention: int) -> int:
        """حذف پوشه‌های موقت قدیمی‌تر از دوره نگهداری (بخش ۲۲.۵: حذف امن)."""
        base = Path(self.d.temp_dir)
        if not base.exists():
            return 0
        removed = 0
        cutoff = time.time() - retention
        for child in base.iterdir():
            try:
                if child.is_dir() and child.name.startswith("req_") and \
                        child.stat().st_mtime < cutoff:
                    shutil.rmtree(child, ignore_errors=True)
                    removed += 1
            except OSError:
                continue
        return removed


class EngineError(Exception):
    """خطای موتور با مرحله مشخص."""

    def __init__(self, message: str, stage: str = "other"):
        super().__init__(message)
        self.stage = stage


class DownloadError(EngineError):
    pass


class SendError(EngineError):
    pass


class _NoLinks:
    """دال شکسته برای جلوگیری از وابستگی دور در UserService."""
    def get_active(self, _):
        return None
