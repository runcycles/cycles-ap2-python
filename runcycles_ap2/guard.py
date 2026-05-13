"""Context managers (sync and async) wrapping a single AP2 payment moment in a Cycles reservation.

:class:`GuardedPayment` is the synchronous variant; :class:`AsyncGuardedPayment` is its
asyncio counterpart. The two classes share idempotency, validation, mapping, and
receipt logic via module-level helpers — only their I/O paths differ.
"""

from __future__ import annotations

import logging
import time
from types import TracebackType
from typing import Any

from runcycles.client import AsyncCyclesClient, CyclesClient
from runcycles.models import Decision, ReservationCreateResponse

from runcycles_ap2._constants import DEFAULT_ACTION_KIND, DEFAULT_OVERAGE_POLICY, DEFAULT_TTL_MS
from runcycles_ap2._validation import validate_micros
from runcycles_ap2.exceptions import AP2DryRunResult, AP2GuardCommitFailed, AP2GuardCommitUncertain, AP2GuardDenied
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
      - Clean exit (no exception) → ``commit_reservation`` with the deterministic AP2
        commit idempotency key (see :func:`runcycles_ap2.mapping.idempotency_key`).
      - Exception inside ``with`` block → ``release_reservation`` with reason
        ``ap2_guard_failed:{ExcType}`` and the matching release idempotency key.
      - Server ``Decision.DENY`` on enter → raises :class:`AP2GuardDenied`; real money
        never moves and no commit/release is issued.
      - ``dry_run=True`` → raises :class:`AP2DryRunResult` from ``__enter__`` so the
        ``with`` body never executes (a body-level PSP call would otherwise move money
        with no Cycles record).
      - Commit rejected with unrecognized code → raises :class:`AP2GuardCommitFailed`
        after attempting a release; check the exception's ``released`` /
        ``release_error`` attributes to know whether budget was recovered.
      - Same consume-once key on a retry (``open_mandate_hash`` when present,
        otherwise ``transaction_id``) → both attempts hit the same Cycles idempotency
        bucket. Same payload replays the original reservation; divergent payload is
        rejected by the server with ``IDEMPOTENCY_MISMATCH`` (surfaced as
        :class:`AP2GuardDenied`).
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
        """Override the committed amount. Defaults to ``mandate.amount_micros()``.

        Raises :class:`AP2MandateError` if ``amount`` is not a plain ``int`` (``bool``
        and ``float`` are explicitly rejected — they would otherwise reach the wire
        payload and only surface as a post-commit pydantic failure during receipt
        construction), is negative, or exceeds the int64 USD_MICROCENTS ceiling.
        """
        self._actual_micros = validate_micros(amount, field="actual amount")

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
        except Exception as exc:
            # Post-PSP exception during the commit POST itself. The request may have
            # reached Cycles and mutated state before the exception was raised, so the
            # commit outcome is unknown. NEVER auto-release — that would risk undoing
            # a successful settle. Surface as commit-uncertain so the caller cannot
            # miss the reconciliation event; chain the original via `from exc`.
            logger.exception("AP2 commit raised: tx=%s", self._mandate.transaction_id)
            raise AP2GuardCommitUncertain(
                f"AP2 commit raised before a response was received for transaction "
                f"{self._mandate.transaction_id}: {type(exc).__name__}: {exc}. "
                "Commit outcome is unknown; no release was attempted.",
                error_code="COMMIT_RAISED",
                reservation_id=self._reservation_id,
            ) from exc

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

        # Transport-level or server-side (5xx) failure. The commit POST may have
        # reached the server and mutated state before the response was lost or the
        # server errored out. We CANNOT tell whether the commit succeeded, so we
        # MUST NOT auto-release — that risks undoing a successful settle. Same
        # uncertain semantics as the terminal-status codes below.
        if response.is_transport_error or response.is_server_error:
            synthetic_code = error_code or ("TRANSPORT_ERROR" if response.is_transport_error else "SERVER_ERROR")
            logger.warning(
                "AP2 commit %s failure (raising uncertain): id=%s, tx=%s, status=%d, code=%s",
                "transport" if response.is_transport_error else "server",
                self._reservation_id,
                self._mandate.transaction_id,
                response.status,
                synthetic_code,
            )
            raise AP2GuardCommitUncertain(
                f"AP2 commit failed at the transport/server layer for transaction "
                f"{self._mandate.transaction_id} (status={response.status}, code={synthetic_code}). "
                "Commit may have reached Cycles before the failure; no release was attempted.",
                error_code=synthetic_code,
                request_id=request_id,
                reservation_id=self._reservation_id,
            )

        if error_code in ("RESERVATION_FINALIZED", "RESERVATION_EXPIRED", "IDEMPOTENCY_MISMATCH"):
            # The reservation is in a terminal state on the server. We do NOT auto-release
            # (that would undo a prior successful commit), but we also do NOT return
            # silently — after the PSP body has run, any of these is a reconciliation
            # event the caller MUST see. RESERVATION_EXPIRED in particular is dangerous:
            # the PSP may have charged while Cycles reclaimed the budget on TTL.
            logger.warning(
                "AP2 commit returned %s (terminal, raising uncertain): id=%s, tx=%s",
                error_code,
                self._reservation_id,
                self._mandate.transaction_id,
            )
            raise AP2GuardCommitUncertain(
                f"AP2 commit returned {error_code} for transaction {self._mandate.transaction_id}. "
                "Reservation is in a terminal state; PSP state may need reconciliation. "
                "No release was attempted (a prior commit may already have settled).",
                error_code=error_code,
                request_id=request_id,
                reservation_id=self._reservation_id,
            )

        # 4xx with an unrecognized error code: the server explicitly rejected the
        # commit request itself (malformed, forbidden, etc.). Releasing is safe here
        # — the server saw the request and refused, so no mutation occurred on its
        # side. The PSP may still have moved money, hence we raise AP2GuardCommitFailed
        # for explicit reconciliation.
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


# ---------------------------------------------------------------------------
# Async variant
# ---------------------------------------------------------------------------
#
# Mirrors :class:`GuardedPayment` exactly. The only differences are:
#   - ``client`` is :class:`runcycles.client.AsyncCyclesClient`
#   - ``__enter__``/``__exit__`` become ``__aenter__``/``__aexit__``
#   - the three I/O call sites (``create_reservation``, ``commit_reservation``,
#     ``release_reservation``) are ``await``ed
#
# All non-I/O state, validation, mapping, and receipt-building helpers are shared
# via module-level functions (`build_reservation_body`, `validate_micros`, etc.),
# so the two classes stay structurally aligned and the same bugs cannot regress in
# one without showing up in the other's tests.


class AsyncGuardedPayment:
    """Async context manager: reserve on ``__aenter__``, commit/release on ``__aexit__``.

    Behaviour matches :class:`GuardedPayment` exactly; use this when your transport
    layer is :class:`AsyncCyclesClient` (FastAPI, asyncio agents, anyio).

    Decision rules:
      - Clean exit (no exception) → ``commit_reservation`` with the deterministic AP2
        commit idempotency key (see :func:`runcycles_ap2.mapping.idempotency_key`).
      - Exception inside ``async with`` block → ``release_reservation`` with reason
        ``ap2_guard_failed:{ExcType}`` and the matching release idempotency key.
      - Server ``Decision.DENY`` on enter → raises :class:`AP2GuardDenied`; real money
        never moves and no commit/release is issued.
      - ``dry_run=True`` → raises :class:`AP2DryRunResult` from ``__aenter__`` so the
        ``async with`` body never executes (a body-level PSP call would otherwise move
        money with no Cycles record).
      - Post-PSP commit unknown-outcome (terminal codes, transport, 5xx, uncaught) →
        raises :class:`AP2GuardCommitUncertain`. **No auto-release** — the commit POST
        may have reached and settled Cycles before the failure was observed.
      - Commit rejected with unrecognized 4xx code → raises
        :class:`AP2GuardCommitFailed` after attempting a release; check the exception's
        ``released`` / ``release_error`` attributes to know whether budget was recovered.
      - Same consume-once key on a retry (``open_mandate_hash`` when present,
        otherwise ``transaction_id``) → both attempts hit the same Cycles idempotency
        bucket. Same payload replays the original reservation; divergent payload is
        rejected by the server with ``IDEMPOTENCY_MISMATCH`` (surfaced as
        :class:`AP2GuardDenied`).
    """

    def __init__(
        self,
        client: AsyncCyclesClient,
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

    # -- public properties (identical to the sync class) ------------------

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
        """Override the committed amount. Same validation contract as the sync class."""
        self._actual_micros = validate_micros(amount, field="actual amount")

    def attach_receipt_fields(self, **fields: Any) -> None:
        """Attach caller-supplied fields (e.g. PSP reference id) to the commit metadata."""
        self._commit_metadata.update(fields)

    def abort(self, reason: str) -> None:
        """Force a release on clean exit (instead of commit). Use for late-discovered failures."""
        self._aborted_reason = reason[:256]

    # -- async context manager protocol ----------------------------------

    async def __aenter__(self) -> AsyncGuardedPayment:
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
        response = await self._client.create_reservation(body)

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
            logger.info(
                "AP2 dry-run evaluated (async): decision=%s, tx=%s",
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
            "AP2 reservation created (async): id=%s, tx=%s, decision=%s",
            self._reservation_id,
            self._mandate.transaction_id,
            result.decision,
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        # NOTE: dry-run never reaches __aexit__ — __aenter__ raises AP2DryRunResult
        # before returning, so the `async with` body never executes.
        if self._reservation_id is None:
            return  # denial path already raised; nothing to clean up

        if exc_type is not None:
            await self._handle_release(
                reason=f"ap2_guard_failed:{exc_type.__name__}",
                exc_name=exc_type.__name__,
            )
            return

        if self._aborted_reason is not None:
            await self._handle_release(
                reason=f"ap2_guard_aborted:{self._aborted_reason}",
                exc_name="Aborted",
            )
            return

        await self._handle_commit()

    # -- async internals -------------------------------------------------

    async def _handle_commit(self) -> None:
        assert self._reservation_id is not None
        body = build_commit_body(
            self._mandate,
            actual_micros=self._actual_micros,
            metadata=self._commit_metadata or None,
        )
        try:
            response = await self._client.commit_reservation(self._reservation_id, body)
        except Exception as exc:
            logger.exception("AP2 commit raised (async): tx=%s", self._mandate.transaction_id)
            raise AP2GuardCommitUncertain(
                f"AP2 commit raised before a response was received for transaction "
                f"{self._mandate.transaction_id}: {type(exc).__name__}: {exc}. "
                "Commit outcome is unknown; no release was attempted.",
                error_code="COMMIT_RAISED",
                reservation_id=self._reservation_id,
            ) from exc

        if response.is_success:
            self._committed = True
            logger.info(
                "AP2 commit successful (async): id=%s, tx=%s",
                self._reservation_id,
                self._mandate.transaction_id,
            )
            if self._emit_receipt:
                self._build_receipt()
            return

        error = response.get_error_response()
        error_code = error.error_code.value if (error and error.error_code) else None
        request_id = error.request_id if error else None

        if response.is_transport_error or response.is_server_error:
            synthetic_code = error_code or ("TRANSPORT_ERROR" if response.is_transport_error else "SERVER_ERROR")
            logger.warning(
                "AP2 commit %s failure (async, raising uncertain): id=%s, tx=%s, status=%d, code=%s",
                "transport" if response.is_transport_error else "server",
                self._reservation_id,
                self._mandate.transaction_id,
                response.status,
                synthetic_code,
            )
            raise AP2GuardCommitUncertain(
                f"AP2 commit failed at the transport/server layer for transaction "
                f"{self._mandate.transaction_id} (status={response.status}, code={synthetic_code}). "
                "Commit may have reached Cycles before the failure; no release was attempted.",
                error_code=synthetic_code,
                request_id=request_id,
                reservation_id=self._reservation_id,
            )

        if error_code in ("RESERVATION_FINALIZED", "RESERVATION_EXPIRED", "IDEMPOTENCY_MISMATCH"):
            logger.warning(
                "AP2 commit returned %s (async, terminal, raising uncertain): id=%s, tx=%s",
                error_code,
                self._reservation_id,
                self._mandate.transaction_id,
            )
            raise AP2GuardCommitUncertain(
                f"AP2 commit returned {error_code} for transaction {self._mandate.transaction_id}. "
                "Reservation is in a terminal state; PSP state may need reconciliation. "
                "No release was attempted (a prior commit may already have settled).",
                error_code=error_code,
                request_id=request_id,
                reservation_id=self._reservation_id,
            )

        logger.warning(
            "AP2 commit rejected (async, releasing): id=%s, tx=%s, code=%s",
            self._reservation_id,
            self._mandate.transaction_id,
            error_code,
        )
        released, release_error = await self._handle_release(
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

    async def _handle_release(self, *, reason: str, exc_name: str) -> tuple[bool, str | None]:
        """Async release. Same return contract as the sync variant."""
        assert self._reservation_id is not None
        body = build_release_body(self._mandate, reason=reason, exception_type=exc_name)
        try:
            response = await self._client.release_reservation(self._reservation_id, body)
        except Exception as exc:
            logger.exception("AP2 release raised (async): id=%s", self._reservation_id)
            return False, f"{type(exc).__name__}: {exc}"

        if response.is_success:
            logger.info(
                "AP2 released (async): id=%s, tx=%s, reason=%s",
                self._reservation_id,
                self._mandate.transaction_id,
                reason,
            )
            return True, None

        error = response.get_error_response()
        error_code = error.error_code.value if (error and error.error_code) else None
        logger.warning(
            "AP2 release returned non-success (async): id=%s, status=%d, code=%s",
            self._reservation_id,
            response.status,
            error_code,
        )
        detail = f"status={response.status}"
        if error_code:
            detail = f"{detail}, code={error_code}"
        return False, detail

    def _build_receipt(self) -> None:
        # Pure logic — identical to the sync variant. Kept as a method (not a free
        # function) so attribute access stays clean and the two classes look symmetric.
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


def cycles_guard_payment_async(
    client: AsyncCyclesClient,
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
) -> AsyncGuardedPayment:
    """Construct an :class:`AsyncGuardedPayment` for a single AP2 payment moment.

    Use ``async with`` to enter the lifecycle::

        async with cycles_guard_payment_async(client, mandate=..., run_id=..., tenant=...):
            receipt = await psp.charge(mandate)
    """
    return AsyncGuardedPayment(
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
