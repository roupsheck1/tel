"""کانتینر سرویس‌ها — مونتاژ نهایی وابستگی‌ها (تفکیک مسئولیت‌ها، بخش ۴.۱ سند)."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..database import Database
from ..database.repositories import (
    AdminsRepo, AuditRepo, BackupRepo, BroadcastRepo, CampaignsRepo, DownloadsRepo,
    ErrorsRepo, ExceptionsRepo, GroupsRepo, LinksRepo, OrdersRepo, PaymentMethodsRepo,
    PlansRepo, PromoCodesRepo, SettingsRepo, SubscriptionsRepo, TrialRepo, UsersRepo,
)
from ..admin.permissions import PermissionService
from .download_engine import DownloadEngine, EngineDeps
from .orders import OrderService
from .plans import PlanService
from .settings import SettingsService
from .subscriptions import SubscriptionService
from .user_groups import GroupService
from .users import UserService


@dataclass
class Services:
    """دسترسی یکتا به همه سرویس‌ها برای هندلرها و زمان‌بندها."""
    db: Database
    users_repo: UsersRepo
    links_repo: LinksRepo
    admins_repo: AdminsRepo
    settings_repo: SettingsRepo
    plans_repo: PlansRepo
    subs_repo: SubscriptionsRepo
    orders_repo: OrdersRepo
    payments_repo: PaymentMethodsRepo
    codes_repo: PromoCodesRepo
    campaigns_repo: CampaignsRepo
    groups_repo: GroupsRepo
    exceptions_repo: ExceptionsRepo
    downloads_repo: DownloadsRepo
    audit_repo: AuditRepo
    errors_repo: ErrorsRepo
    broadcast_repo: BroadcastRepo
    trial_repo: TrialRepo
    backup_repo: BackupRepo

    settings: SettingsService
    plan_service: PlanService
    user_service: UserService
    sub_service: SubscriptionService
    order_service: OrderService
    group_service: GroupService
    permissions: PermissionService
    engine: DownloadEngine = field(default=None)

    # ---------- راه‌اندازی اولیه ----------
    def bootstrap(self, owner_ids: list[int]) -> None:
        """مهاجرت + نقش‌ها + مدیران اولیه + تنظیمات پیش‌فرض + پلن پیش‌فرض."""
        self.db.migrate()
        self.admins_repo.seed(owner_ids)
        self.settings.seed_defaults()
        self._seed_default_payment_method()
        self._seed_default_plans()

    def _seed_default_payment_method(self) -> None:
        if not self.payments_repo.list():
            self.payments_repo.create(
                title="کارت به کارت",
                card_number="6037-99XX-XXXX-XXXX",
                holder_name="مدیر سرویس",
                bank_name="بانک ملی",
                instructions="پس از واریز، رسید را برای ربات بفرستید.",
            )

    def _seed_default_plans(self) -> None:
        """پلن‌های نمونه اولیه؛ از پنل قابل ویرایش/بایگانی هستند (بخش ۸)."""
        if self.plans_repo.list():
            return
        from .plans import PlanConfig
        owner = self.admins_repo.list_admins()
        created_by = owner[0]["telegram_id"] if owner else 0
        seeds = [
            ("free_trial", "آزمایشی ۱ روزه", "دوره آزمایشی با محدودیت",
             PlanConfig(price=0, duration_value=1, duration_unit="day",
                        concurrent_downloads=1, wait_seconds=60, queue_limit=2,
                        hourly_quota=3, daily_quota=5, max_file_mb=50,
                        is_trial=True, is_sellable=False)),
            ("basic_month", "پایه یک‌ماهه", "دسترسی استاندارد",
             PlanConfig(price=99000, duration_value=1, duration_unit="month",
                        concurrent_downloads=1, wait_seconds=20, queue_limit=10,
                        hourly_quota=None, daily_quota=None, max_file_mb=50)),
            ("pro_month", "حرفه‌ای یک‌ماهه", "دانلود هم‌زمان بیشتر",
             PlanConfig(price=199000, duration_value=1, duration_unit="month",
                        concurrent_downloads=3, wait_seconds=5, queue_limit=20,
                        hourly_quota=None, daily_quota=None, max_file_mb=50, priority=1)),
        ]
        for name, title, desc, cfg in seeds:
            ok, res = self.plan_service.create_plan(name, title, desc, cfg, created_by)
            if ok:
                # انتشار نسخه اول برای قابل‌فروش‌شدن
                plan = self.plans_repo.get_by_name(name)
                version = self.plans_repo.current_version(plan["id"])
                self.plans_repo.publish_version(version["id"], "future")

    # ---------- موتور دانلود ----------
    def attach_engine(self, engine: DownloadEngine) -> None:
        self.engine = engine


def build_services(db: Database, owner_ids: list[int]) -> Services:
    """ساخت کانتینر با همه سرویس‌ها (بدون موتور — موتور در main سیم‌کشی می‌شود)."""
    users = UsersRepo(db)
    links = LinksRepo(db)
    admins = AdminsRepo(db)
    settings_repo = SettingsRepo(db)
    plans = PlansRepo(db)
    subs = SubscriptionsRepo(db)
    orders = OrdersRepo(db)
    payments = PaymentMethodsRepo(db)
    codes = PromoCodesRepo(db)
    campaigns = CampaignsRepo(db)
    groups = GroupsRepo(db)
    exceptions = ExceptionsRepo(db)
    downloads = DownloadsRepo(db)
    audit = AuditRepo(db)
    errors = ErrorsRepo(db)
    broadcast = BroadcastRepo(db)
    trials = TrialRepo(db)
    backups = BackupRepo(db)

    settings = SettingsService(settings_repo)
    plan_service = PlanService(plans, subs)
    user_service = UserService(users, links, subs, settings)
    sub_service = SubscriptionService(subs, plans, plan_service, trials, campaigns, codes, settings)
    order_service = OrderService(orders, payments, plans, plan_service, settings)
    group_service = GroupService(groups, subs, plans)
    permissions = PermissionService(admins)

    svc = Services(
        db=db, users_repo=users, links_repo=links, admins_repo=admins,
        settings_repo=settings_repo, plans_repo=plans, subs_repo=subs, orders_repo=orders,
        payments_repo=payments, codes_repo=codes, campaigns_repo=campaigns,
        groups_repo=groups, exceptions_repo=exceptions, downloads_repo=downloads,
        audit_repo=audit, errors_repo=errors, broadcast_repo=broadcast, trial_repo=trials,
        backup_repo=backups,
        settings=settings, plan_service=plan_service, user_service=user_service,
        sub_service=sub_service, order_service=order_service, group_service=group_service,
        permissions=permissions,
    )
    svc.bootstrap(owner_ids)
    return svc


def build_engine(svc: Services, downloader, sender, notify, temp_dir: Path) -> DownloadEngine:
    """ساخت و اتصال موتور دانلود به سرویس‌ها."""
    deps = EngineDeps(
        downloads=svc.downloads_repo, users=svc.users_repo, subs=svc.subs_repo,
        plans=svc.plans_repo, exceptions=svc.exceptions_repo, errors=svc.errors_repo,
        settings=svc.settings, downloader=downloader, sender=sender, notify=notify,
        temp_dir=Path(temp_dir),
        plan_config_of_version=lambda vid: svc.plan_service.config_of_version(
            svc.plans_repo.version(vid)),
    )
    engine = DownloadEngine(deps)
    svc.attach_engine(engine)
    return engine
