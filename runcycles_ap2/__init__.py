"""Cycles runtime authority guard for Google AP2 (Agent Payments Protocol)."""

from __future__ import annotations

from runcycles_ap2.exceptions import (
    AP2CurrencyError,
    AP2DryRunResult,
    AP2GuardCommitFailed,
    AP2GuardCommitUncertain,
    AP2GuardDenied,
    AP2GuardError,
    AP2MandateError,
)
from runcycles_ap2.guard import (
    AsyncGuardedPayment,
    GuardedPayment,
    cycles_guard_payment,
    cycles_guard_payment_async,
)
from runcycles_ap2.models import AP2Mandate, RuntimeAuthorityReceipt

__version__ = "0.3.0"

__all__ = [
    "AP2CurrencyError",
    "AP2DryRunResult",
    "AP2GuardCommitFailed",
    "AP2GuardCommitUncertain",
    "AP2GuardDenied",
    "AP2GuardError",
    "AP2Mandate",
    "AP2MandateError",
    "AsyncGuardedPayment",
    "GuardedPayment",
    "RuntimeAuthorityReceipt",
    "cycles_guard_payment",
    "cycles_guard_payment_async",
    "__version__",
]
