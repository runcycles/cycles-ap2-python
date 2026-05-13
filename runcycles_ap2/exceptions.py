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


class AP2GuardCommitUncertain(AP2GuardError):
    """The commit POST ran after the PSP body but the outcome is unknown.

    Raised when ANY of these conditions hits the commit phase — in every case the
    server may have mutated state before the failure was observed, so auto-release
    is unsafe (it could undo a successful settle):

      - ``RESERVATION_FINALIZED`` — a prior attempt already finalized the reservation;
        usually a benign replay, but we can't verify "matching actuals" client-side.
      - ``RESERVATION_EXPIRED`` — the TTL elapsed before we could commit. The server
        reclaimed the budget, but the PSP body already ran, so the payment may have
        moved without a Cycles settlement.
      - ``IDEMPOTENCY_MISMATCH`` — a prior commit under this idempotency key carried
        a different payload (often: different actuals across retries).
      - **Transport error** (``error_code="TRANSPORT_ERROR"``) — the commit POST
        never got a response (connection lost, timeout, DNS failure, etc.). Cycles
        may have received and applied the commit before the wire died.
      - **5xx server error** (``error_code="SERVER_ERROR"`` or the specific code) —
        the server failed *after* possibly mutating state.
      - **Uncaught exception during commit** (``error_code="COMMIT_RAISED"``) — the
        client code raised before a response was processed; the chained ``__cause__``
        is the original exception. May or may not have reached the server.
      - **Cancellation mid-commit** (``error_code="COMMIT_CANCELLED"``, async only) —
        an outer ``asyncio.CancelledError`` landed while the commit POST was in
        flight. Because ``asyncio.CancelledError`` is a ``BaseException``, it must
        be handled separately from the ``COMMIT_RAISED`` path; semantics are
        otherwise identical. The chained ``__cause__`` is the original
        ``CancelledError``.

    The caller MUST handle this exception — silently returning would let
    unreconciled payment state propagate. Use ``error_code`` to distinguish the
    flavors if you want different reconciliation paths.
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


class AP2GuardCommitFailed(AP2GuardError):
    """Cycles **explicitly rejected** the commit request itself AFTER the body ran.

    Used only for 4xx responses with an unrecognized error code (e.g. malformed
    request, forbidden, etc.). The server saw the request and refused; releasing
    the reservation is safe and is attempted before this exception is raised. The
    PSP may still have moved money, so the caller MUST reconcile regardless.

    - ``released = True``: budget was returned, but PSP-side capture may still need
      reconciliation against your books.
    - ``released = False``: budget is stranded inside the reservation until TTL expiry
      (server-side rollback) AND PSP-side capture may need reconciliation. This is
      the worst case — escalate.

    This is NOT the right exception for unknown-outcome failures. Anything where the
    commit POST might have reached and mutated Cycles before the failure — transport
    errors, 5xx, terminal reservation statuses (``RESERVATION_FINALIZED`` /
    ``RESERVATION_EXPIRED`` / ``IDEMPOTENCY_MISMATCH``), and uncaught exceptions —
    is raised as :class:`AP2GuardCommitUncertain` with **no auto-release**.
    """

    def __init__(
        self,
        message: str,
        *,
        error_code: str | None = None,
        request_id: str | None = None,
        reservation_id: str | None = None,
        released: bool = False,
        release_error: str | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.request_id = request_id
        self.reservation_id = reservation_id
        self.released = released
        self.release_error = release_error


class AP2CurrencyError(AP2GuardError, ValueError):
    """Currency is unsupported in this version (v0.1 is USD-only)."""


class AP2MandateError(AP2GuardError, ValueError):
    """AP2 mandate failed client-side validation before being sent to Cycles."""
