"""بسته repository ها — قرارداد یکتای دسترسی به داده (بخش ۴.۲ سند)."""
from .core import AdminsRepo, LinksRepo, SettingsRepo, UsersRepo
from .commerce import (
    CampaignsRepo,
    OrdersRepo,
    PaymentMethodsRepo,
    PlansRepo,
    PromoCodesRepo,
    SubscriptionsRepo,
)
from .operations import (
    AuditRepo,
    BackupRepo,
    BroadcastRepo,
    DownloadsRepo,
    ErrorsRepo,
    ExceptionsRepo,
    GroupsRepo,
    TrialRepo,
)

__all__ = [
    "UsersRepo", "LinksRepo", "AdminsRepo", "SettingsRepo",
    "PlansRepo", "SubscriptionsRepo", "OrdersRepo", "PaymentMethodsRepo",
    "PromoCodesRepo", "CampaignsRepo",
    "DownloadsRepo", "AuditRepo", "ErrorsRepo", "BroadcastRepo",
    "GroupsRepo", "ExceptionsRepo", "TrialRepo", "BackupRepo",
]
