"""Exceptions raised by the AP2 guard."""

from __future__ import annotations


class AP2GuardError(Exception):
    """Base class for all AP2 guard errors."""


class AP2GuardDenied(AP2GuardError):
    """Cycles returned Decision.DENY for this AP2 payment attempt.

    Real money has NOT moved. The agent must not retry without resolving the underlying
    cause (budget exhaustion, quota, mandate already consumed, etc.).
    """

    def __init__(self, message: str, *, reason_code: str | None = None, request_id: str | None = None) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.request_id = request_id


class AP2CurrencyError(AP2GuardError, ValueError):
    """Currency is unsupported in this version (v0.1 is USD-only)."""


class AP2MandateError(AP2GuardError, ValueError):
    """AP2 mandate failed client-side validation before being sent to Cycles."""
