"""Sync context manager wrapping a single AP2 payment moment in a Cycles reservation."""

from __future__ import annotations

import logging
import time
from types import TracebackType
from typing import Any

from runcycles.client import CyclesClient
from runcycles.models import Decision, ReservationCreateResponse

from runcycles_ap2._constants import DEFAULT_ACTION_KIND, DEFAULT_OVERAGE_POLICY, DEFAULT_TTL_MS
from runcycles_ap2.exceptions import AP2DryRunResult, AP2GuardCommitFailed, AP2GuardDenied
from runcycles_ap2.mapping import (
    build_action,
    build_commit_body,
    build_release_body,
    build_reservation_body,
)
from runcycles_ap2.models import AP2Mandate, RuntimeAuthorityReceipt
from runcycles_ap2.receipt import build_runtime_authority_receipt

logger = logging.getLogger(__name__)


class GuardedPayment:
    """Sync context manager: reserve on ``__enter__``, commit/release on ``__exit__``.

    Decision rules:
      - Clean exit (no exception) → ``commit_reservation`` with ``ap2:{tx}:commit``.
      - Exception inside ``with`` block → ``release_reservation`` with reason
        ``ap2_guard_failed:{ExcType}`` and key ``ap2:{tx}:release:{ExcType}``.
      - Server ``Decision.DENY`` on enter → raises :class:`AP2GuardDenied`; real money
        never moves and no commit/release is issued.
      - Same ``transaction_id`` on a retry → server returns the original reservation
        (idempotent replay), so the second attempt cannot double-spend.
    """

    def __init__(
        self,
        client: CyclesClient,
        *,
        mandate: AP2Mandate,
        run_id: str,
        tenant: str | None = None,
        workspace: str | None = None,
        app: str | None = None,
        workflow: str | None = None,
        agent: str | None = None,
        toolset: str | None = None,
        action_kind: str = DEFAULT_ACTION_KIND,
        ttl_ms: int = DEFAULT_TTL_MS,
        overage_policy: str = DEFAULT_OVERAGE_POLICY,
        dry_run: bool = False,
        emit_receipt: bool = True,
        metadata: dict[str, Any] | None = None,
        extra_dimensions: dict[str, str] | None = None,
    ) -> None:
        self._client = client
        self._mandate = mandate
        self._run_id = run_id
        self._tenant = tenant
        self._workspace = workspace
        self._app = app
        self._workflow = workflow
        self._agent = agent
        self._toolset = toolset
        self._action_kind = action_kind
        self._ttl_ms = ttl_ms
        self._overage_policy = overage_policy
        self._dry_run = dry_run
        self._emit_receipt = emit_receipt
        self._metadata = metadata
        self._extra_dimensions = extra_dimensions

        self._reservation_id: str | None = None
        self._decision: Decision | None = None
        self._actual_micros: int | None = None
        self._commit_metadata: dict[str, Any] = {}
        self._receipt: RuntimeAuthorityReceipt | None = None
        self._committed = False
        self._aborted_reason: str | None = None

    # -- public properties ------------------------------------------------

    @property
    def reservation_id(self) -> str | None:
        return self._reservation_id

    @property
    def decision(self) -> Decision | None:
        return self._decision

    @property
    def receipt(self) -> RuntimeAuthorityReceipt | None:
        return self._receipt

    @property
    def committed(self) -> bool:
        return self._committed

    def set_actual_micros(self, amount: int) -> None:
        """Override the committed amount. Defaults to ``mandate.amount_micros()``."""
        if amount < 0:
            raise ValueError("actual amount must be non-negative")
        self._actual_micros = amount

    def attach_receipt_fields(self, **fields: Any) -> None:
        """Attach caller-supplied fields (e.g. PSP reference id) to the commit metadata."""
        self._commit_metadata.update(fields)

    def abort(self, reason: str) -> None:
        """Force a release on clean exit (instead of commit). Use for late-discovered failures."""
        self._aborted_reason = reason[:256]

    # -- context manager protocol ----------------------------------------

    def __enter__(self) -> GuardedPayment:
        body = build_reservation_body(
            self._mandate,
            run_id=self._run_id,
            tenant=self._tenant,
            workspace=self._workspace,
            app=self._app,
            workflow=self._workflow,
            agent=self._agent,
            toolset=self._toolset,
            action_kind=self._action_kind,
            ttl_ms=self._ttl_ms,
            overage_policy=self._overage_policy,
            dry_run=self._dry_run,
            metadata=self._metadata,
            extra_dimensions=self._extra_dimensions,
        )
        response = self._client.create_reservation(body)

        if not response.is_success:
            error = response.get_error_response()
            reason_code = error.error if error else None
            request_id = error.request_id if error else None
            raise AP2GuardDenied(
                f"AP2 reservation failed for transaction {self._mandate.transaction_id}",
                reason_code=reason_code,
                request_id=request_id,
            )

        result = ReservationCreateResponse.model_validate(response.body)
        self._decision = result.decision

        if result.is_denied():
            raise AP2GuardDenied(
                f"AP2 reservation denied for transaction {self._mandate.transaction_id}: "
                f"{result.reason_code or 'no reason'}",
                reason_code=result.reason_code,
            )

        if self._dry_run:
            # Dry-run is a policy probe — NEVER execute the `with` body. If we returned
            # `self` here, callers running a real PSP charge inside the block would move
            # money with no Cycles record. Raising blocks the body unconditionally; the
            # decision payload rides on the exception.
            logger.info(
                "AP2 dry-run evaluated: decision=%s, tx=%s",
                result.decision,
                self._mandate.transaction_id,
            )
            raise AP2DryRunResult(
                f"AP2 dry-run decision={result.decision.value} for transaction {self._mandate.transaction_id}",
                decision=result.decision.value,
                reason_code=result.reason_code,
                caps=result.caps,
                balances=result.balances,
                affected_scopes=result.affected_scopes,
            )

        if result.reservation_id is None:
            raise AP2GuardDenied(
                "AP2 reservation allowed but server returned no reservation_id",
                reason_code=result.reason_code,
            )

        self._reservation_id = result.reservation_id
        logger.info(
            "AP2 reservation created: id=%s, tx=%s, decision=%s",
            self._reservation_id,
            self._mandate.transaction_id,
            result.decision,
        )
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        # NOTE: dry-run no longer reaches __exit__ — __enter__ raises AP2DryRunResult
        # before returning, so the `with` body never executes.
        if self._reservation_id is None:
            return  # nothing to clean up (denial path already raised)

        if exc_type is not None:
            self._handle_release(reason=f"ap2_guard_failed:{exc_type.__name__}", exc_name=exc_type.__name__)
            return

        if self._aborted_reason is not None:
            self._handle_release(reason=f"ap2_guard_aborted:{self._aborted_reason}", exc_name="Aborted")
            return

        self._handle_commit()

    # -- internals -------------------------------------------------------

    def _handle_commit(self) -> None:
        assert self._reservation_id is not None
        body = build_commit_body(
            self._mandate,
            actual_micros=self._actual_micros,
            metadata=self._commit_metadata or None,
        )
        try:
            response = self._client.commit_reservation(self._reservation_id, body)
        except Exception:
            logger.exception("AP2 commit raised: tx=%s", self._mandate.transaction_id)
            raise

        if response.is_success:
            self._committed = True
            logger.info(
                "AP2 commit successful: id=%s, tx=%s",
                self._reservation_id,
                self._mandate.transaction_id,
            )
            if self._emit_receipt:
                self._build_receipt()
            return

        error = response.get_error_response()
        error_code = error.error_code.value if (error and error.error_code) else None
        request_id = error.request_id if error else None
        if error_code in ("RESERVATION_FINALIZED", "RESERVATION_EXPIRED", "IDEMPOTENCY_MISMATCH"):
            # Benign replay: a previous attempt already finalized the reservation. The
            # caller's payment record is correct from that prior attempt; nothing to do.
            logger.warning(
                "AP2 commit returned %s (no release): id=%s, tx=%s",
                error_code,
                self._reservation_id,
                self._mandate.transaction_id,
            )
            return

        # Unrecognized commit rejection. The PSP may already have moved money, so we
        # release the budget and raise — the caller MUST reconcile, and a silent
        # `guard.committed == False` was too easy to miss.
        logger.warning(
            "AP2 commit rejected (releasing): id=%s, tx=%s, code=%s",
            self._reservation_id,
            self._mandate.transaction_id,
            error_code,
        )
        released, release_error = self._handle_release(
            reason=f"ap2_commit_rejected:{error_code or 'UNKNOWN'}",
            exc_name="CommitRejected",
        )
        status_phrase = (
            "reservation released"
            if released
            else f"reservation release FAILED ({release_error or 'unknown'}); budget stranded until TTL"
        )
        raise AP2GuardCommitFailed(
            f"AP2 commit rejected for transaction {self._mandate.transaction_id} "
            f"(code={error_code}); {status_phrase}. PSP state may need reconciliation.",
            error_code=error_code,
            request_id=request_id,
            reservation_id=self._reservation_id,
            released=released,
            release_error=release_error,
        )

    def _handle_release(self, *, reason: str, exc_name: str) -> tuple[bool, str | None]:
        """Release the reservation. Returns ``(success, error_description)``.

        ``success`` is True only when the server returned a 2xx for the release. Any
        non-success response or raised transport error sets ``success=False`` and
        carries a short error description so the caller can surface it to operators.
        """
        assert self._reservation_id is not None
        body = build_release_body(self._mandate, reason=reason, exception_type=exc_name)
        try:
            response = self._client.release_reservation(self._reservation_id, body)
        except Exception as exc:
            logger.exception("AP2 release raised: id=%s", self._reservation_id)
            return False, f"{type(exc).__name__}: {exc}"

        if response.is_success:
            logger.info(
                "AP2 released: id=%s, tx=%s, reason=%s",
                self._reservation_id,
                self._mandate.transaction_id,
                reason,
            )
            return True, None

        error = response.get_error_response()
        error_code = error.error_code.value if (error and error.error_code) else None
        logger.warning(
            "AP2 release returned non-success: id=%s, status=%d, code=%s",
            self._reservation_id,
            response.status,
            error_code,
        )
        detail = f"status={response.status}"
        if error_code:
            detail = f"{detail}, code={error_code}"
        return False, detail

    def _build_receipt(self) -> None:
        assert self._reservation_id is not None
        assert self._decision is not None
        policy_keys = build_action(self._mandate, action_kind=self._action_kind)["policy_keys"]
        committed_micros = self._actual_micros if self._actual_micros is not None else self._mandate.amount_micros()
        raw_psp_ref = self._commit_metadata.get("psp_ref")
        psp_ref = raw_psp_ref if isinstance(raw_psp_ref, str) else None
        extra = {k: v for k, v in self._commit_metadata.items() if k != "psp_ref"} or None
        self._receipt = build_runtime_authority_receipt(
            self._mandate,
            decision=self._decision.value,
            reservation_id=self._reservation_id,
            tenant=self._tenant,
            agent=self._agent,
            action_kind=self._action_kind,
            policy_keys=policy_keys,
            amount_micros=committed_micros,
            committed=True,
            psp_ref=psp_ref,
            extra=extra,
            issued_at_ms=int(time.time() * 1000),
        )


def cycles_guard_payment(
    client: CyclesClient,
    *,
    mandate: AP2Mandate,
    run_id: str,
    tenant: str | None = None,
    workspace: str | None = None,
    app: str | None = None,
    workflow: str | None = None,
    agent: str | None = None,
    toolset: str | None = None,
    action_kind: str = DEFAULT_ACTION_KIND,
    ttl_ms: int = DEFAULT_TTL_MS,
    overage_policy: str = DEFAULT_OVERAGE_POLICY,
    dry_run: bool = False,
    emit_receipt: bool = True,
    metadata: dict[str, Any] | None = None,
    extra_dimensions: dict[str, str] | None = None,
) -> GuardedPayment:
    """Construct a :class:`GuardedPayment` context manager for a single AP2 payment moment."""
    return GuardedPayment(
        client,
        mandate=mandate,
        run_id=run_id,
        tenant=tenant,
        workspace=workspace,
        app=app,
        workflow=workflow,
        agent=agent,
        toolset=toolset,
        action_kind=action_kind,
        ttl_ms=ttl_ms,
        overage_policy=overage_policy,
        dry_run=dry_run,
        emit_receipt=emit_receipt,
        metadata=metadata,
        extra_dimensions=extra_dimensions,
    )
