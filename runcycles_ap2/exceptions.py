"""Exceptions raised by the AP2 guard."""

from __future__ import annotations

from typing import Any


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


class AP2DryRunResult(AP2GuardError):
    """Raised from ``__enter__`` when ``dry_run=True``.

    The body of the ``with`` block does NOT execute — this is deliberate. A dry-run is
    a policy *probe*, not a payment attempt; if the body ran and you swapped in a real
    PSP, money would move with no Cycles record. Catching this exception is the only
    way to read the decision in dry-run mode.

    The exception carries the decision payload that a non-dry-run call would have
    produced (``decision``, ``reason_code``, ``caps``, ``balances``).
    """

    def __init__(
        self,
        message: str,
        *,
        decision: str,
        reason_code: str | None = None,
        caps: Any = None,
        balances: Any = None,
        affected_scopes: list[str] | None = None,
    ) -> None:
        super().__init__(message)
        self.decision = decision
        self.reason_code = reason_code
        self.caps = caps
        self.balances = balances
        self.affected_scopes = affected_scopes


class AP2GuardCommitFailed(AP2GuardError):
    """Cycles rejected the commit AFTER the body ran (PSP may already have charged).

    The reservation has been released to prevent stranding budget, but the caller MUST
    treat this as a reconciliation event: payment state on the PSP side may be out of
    sync with Cycles' view. We raise instead of returning silently so the caller cannot
    miss the condition.

    This exception is NOT raised for ``RESERVATION_FINALIZED`` / ``RESERVATION_EXPIRED``
    / ``IDEMPOTENCY_MISMATCH`` — those indicate a prior attempt already finalized the
    reservation and the current call is a benign replay.
    """

    def __init__(
        self,
        message: str,
        *,
        error_code: str | None = None,
        request_id: str | None = None,
        reservation_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.request_id = request_id
        self.reservation_id = reservation_id


class AP2CurrencyError(AP2GuardError, ValueError):
    """Currency is unsupported in this version (v0.1 is USD-only)."""


class AP2MandateError(AP2GuardError, ValueError):
    """AP2 mandate failed client-side validation before being sent to Cycles."""
