"""AsyncGuardedPayment — mirrors the sync GuardedPayment test surface.

Same contract: reserve on __aenter__, commit on clean exit, release on exception,
AP2GuardDenied on DENY, AP2DryRunResult on dry-run, AP2GuardCommitUncertain on
post-PSP unknown outcomes, AP2GuardCommitFailed on 4xx unrecognized commit rejection.
"""

from __future__ import annotations

import pytest
from runcycles.response import CyclesResponse

from runcycles_ap2 import (
    AP2DryRunResult,
    AP2GuardCommitFailed,
    AP2GuardCommitUncertain,
    AP2GuardDenied,
    AP2MandateError,
    cycles_guard_payment_async,
)
from runcycles_ap2._constants import MAX_USD_MICROS
from runcycles_ap2.mapping import idempotency_key
from tests.conftest import (
    allow_response,
    commit_error_response,
    commit_success_response,
    deny_response,
    release_success_response,
)

# ---------------------------------------------------------------------------
# Clean commit
# ---------------------------------------------------------------------------


class TestAsyncCleanCommit:
    async def test_commit_called_with_ap2_idempotency_key(self, async_mock_client, mandate) -> None:
        async_mock_client.create_reservation.return_value = allow_response("rsv_async_clean")
        async_mock_client.commit_reservation.return_value = commit_success_response()

        async with cycles_guard_payment_async(
            async_mock_client, mandate=mandate, run_id="run_a", tenant="acme", agent="bot"
        ) as guard:
            assert guard.reservation_id == "rsv_async_clean"
            assert guard.decision is not None
            assert guard.decision.value == "ALLOW"

        assert guard.committed is True
        async_mock_client.commit_reservation.assert_awaited_once()
        called_id, called_body = async_mock_client.commit_reservation.call_args[0]
        assert called_id == "rsv_async_clean"
        assert called_body["idempotency_key"] == idempotency_key(mandate, "commit")
        assert called_body["actual"] == {"unit": "USD_MICROCENTS", "amount": 19_900_000_000}
        async_mock_client.release_reservation.assert_not_awaited()

    async def test_actual_micros_override(self, async_mock_client, mandate) -> None:
        async_mock_client.create_reservation.return_value = allow_response()
        async_mock_client.commit_reservation.return_value = commit_success_response()

        async with cycles_guard_payment_async(async_mock_client, mandate=mandate, run_id="r", tenant="acme") as guard:
            guard.set_actual_micros(5_000_000_000)

        body = async_mock_client.commit_reservation.call_args[0][1]
        assert body["actual"]["amount"] == 5_000_000_000

    async def test_attach_receipt_fields_lands_in_metadata_and_receipt(self, async_mock_client, mandate) -> None:
        async_mock_client.create_reservation.return_value = allow_response()
        async_mock_client.commit_reservation.return_value = commit_success_response()

        async with cycles_guard_payment_async(
            async_mock_client, mandate=mandate, run_id="r", tenant="acme", agent="bot"
        ) as guard:
            guard.attach_receipt_fields(psp_ref="psp_async_1", trace_id="trace-1")

        body = async_mock_client.commit_reservation.call_args[0][1]
        assert body["metadata"]["psp_ref"] == "psp_async_1"
        assert guard.receipt is not None
        assert guard.receipt.psp_ref == "psp_async_1"
        assert guard.receipt.extra == {"trace_id": "trace-1"}
        assert guard.receipt.committed is True

    async def test_emit_receipt_false_skips_receipt(self, async_mock_client, mandate) -> None:
        async_mock_client.create_reservation.return_value = allow_response()
        async_mock_client.commit_reservation.return_value = commit_success_response()

        async with cycles_guard_payment_async(
            async_mock_client, mandate=mandate, run_id="r", tenant="acme", emit_receipt=False
        ) as guard:
            pass

        assert guard.receipt is None
        assert guard.committed is True

    async def test_reserve_body_carries_policy_keys_and_consume_once_scope(self, async_mock_client) -> None:
        from tests.conftest import make_mandate

        async_mock_client.create_reservation.return_value = allow_response()
        async_mock_client.commit_reservation.return_value = commit_success_response()
        m = make_mandate(open_mandate_hash="omh_async")  # forces open_mandate scope

        async with cycles_guard_payment_async(async_mock_client, mandate=m, run_id="r", tenant="acme"):
            pass

        body = async_mock_client.create_reservation.call_args[0][0]
        assert body["idempotency_key"] == idempotency_key(m, "reserve")
        assert ":open_mandate:" in body["idempotency_key"]
        assert body["action"]["policy_keys"]["host"] == "merchant.example"

    async def test_set_actual_micros_above_int64_rejected(self, async_mock_client, mandate) -> None:
        async_mock_client.create_reservation.return_value = allow_response()
        async_mock_client.release_reservation.return_value = release_success_response()

        with pytest.raises(AP2MandateError, match="int64"):
            async with cycles_guard_payment_async(
                async_mock_client, mandate=mandate, run_id="r", tenant="acme"
            ) as guard:
                guard.set_actual_micros(MAX_USD_MICROS + 1)

        async_mock_client.commit_reservation.assert_not_awaited()
        async_mock_client.release_reservation.assert_awaited_once()

    async def test_set_actual_micros_negative_rejected(self, async_mock_client, mandate) -> None:
        async_mock_client.create_reservation.return_value = allow_response()
        async_mock_client.release_reservation.return_value = release_success_response()

        with pytest.raises(AP2MandateError, match="non-negative"):
            async with cycles_guard_payment_async(async_mock_client, mandate=mandate, run_id="r", tenant="acme") as g:
                g.set_actual_micros(-1)

        async_mock_client.commit_reservation.assert_not_awaited()
        async_mock_client.release_reservation.assert_awaited_once()

    @pytest.mark.parametrize("bad", [True, False, 1.5, 1.0, "100", None, [100]])
    async def test_set_actual_micros_rejects_non_int_types(self, async_mock_client, mandate, bad) -> None:
        # Mirrors the sync parametrized test: bool is an int subclass, float compares
        # numerically — both must be rejected up front before reaching the wire.
        async_mock_client.create_reservation.return_value = allow_response()
        async_mock_client.release_reservation.return_value = release_success_response()

        with pytest.raises(AP2MandateError, match="must be an int"):
            async with cycles_guard_payment_async(async_mock_client, mandate=mandate, run_id="r", tenant="acme") as g:
                g.set_actual_micros(bad)  # type: ignore[arg-type]

        async_mock_client.commit_reservation.assert_not_awaited()


# ---------------------------------------------------------------------------
# Release on exception
# ---------------------------------------------------------------------------


class TestAsyncReleaseOnException:
    async def test_runtime_error_triggers_release(self, async_mock_client, mandate) -> None:
        async_mock_client.create_reservation.return_value = allow_response("rsv_async_rel")
        async_mock_client.release_reservation.return_value = release_success_response()

        with pytest.raises(RuntimeError, match="psp failure"):
            async with cycles_guard_payment_async(async_mock_client, mandate=mandate, run_id="r", tenant="acme"):
                raise RuntimeError("psp failure")

        async_mock_client.release_reservation.assert_awaited_once()
        called_id, body = async_mock_client.release_reservation.call_args[0]
        assert called_id == "rsv_async_rel"
        assert body["idempotency_key"] == idempotency_key(mandate, "release", "RuntimeError")
        assert body["reason"].startswith("ap2_guard_failed:RuntimeError")
        async_mock_client.commit_reservation.assert_not_awaited()

    async def test_abort_releases_on_clean_exit(self, async_mock_client, mandate) -> None:
        async_mock_client.create_reservation.return_value = allow_response()
        async_mock_client.release_reservation.return_value = release_success_response()

        async with cycles_guard_payment_async(async_mock_client, mandate=mandate, run_id="r", tenant="acme") as guard:
            guard.abort("psp_returned_failure")

        async_mock_client.commit_reservation.assert_not_awaited()
        async_mock_client.release_reservation.assert_awaited_once()
        body = async_mock_client.release_reservation.call_args[0][1]
        assert "psp_returned_failure" in body["reason"]
        assert guard.committed is False

    async def test_release_failure_does_not_swallow_original_exception(self, async_mock_client, mandate) -> None:
        async_mock_client.create_reservation.return_value = allow_response()
        async_mock_client.release_reservation.side_effect = ConnectionError("transport down")

        with pytest.raises(RuntimeError, match="boom"):
            async with cycles_guard_payment_async(async_mock_client, mandate=mandate, run_id="r", tenant="acme"):
                raise RuntimeError("boom")

    async def test_value_error_uses_exception_type_in_key(self, async_mock_client, mandate) -> None:
        # Mirrors sync: the exception class name is sanitized and embedded in the
        # release idempotency key, so different exception types get different release
        # keys (preserves separate dedup buckets if multiple retries hit).
        async_mock_client.create_reservation.return_value = allow_response("rsv_ve")
        async_mock_client.release_reservation.return_value = release_success_response()

        with pytest.raises(ValueError):
            async with cycles_guard_payment_async(async_mock_client, mandate=mandate, run_id="r", tenant="acme"):
                raise ValueError("nope")

        body = async_mock_client.release_reservation.call_args[0][1]
        assert body["idempotency_key"] == idempotency_key(mandate, "release", "ValueError")


# ---------------------------------------------------------------------------
# Denial / dry-run
# ---------------------------------------------------------------------------


class TestAsyncDenial:
    async def test_deny_raises_before_psp_call(self, async_mock_client, mandate) -> None:
        async_mock_client.create_reservation.return_value = deny_response("BUDGET_EXCEEDED")
        sentinel = {"psp_called": False}

        with pytest.raises(AP2GuardDenied) as ei:
            async with cycles_guard_payment_async(async_mock_client, mandate=mandate, run_id="r", tenant="acme"):
                sentinel["psp_called"] = True

        assert sentinel["psp_called"] is False
        assert ei.value.reason_code == "BUDGET_EXCEEDED"
        async_mock_client.commit_reservation.assert_not_awaited()
        async_mock_client.release_reservation.assert_not_awaited()

    async def test_http_error_on_reserve_raises_guard_denied(self, async_mock_client, mandate) -> None:
        async_mock_client.create_reservation.return_value = CyclesResponse.http_error(
            400,
            error_message="bad request",
            body={"error": "INVALID_REQUEST", "message": "bad", "request_id": "req_1"},
        )

        with pytest.raises(AP2GuardDenied) as ei:
            async with cycles_guard_payment_async(async_mock_client, mandate=mandate, run_id="r", tenant="acme"):
                pass

        assert ei.value.reason_code == "INVALID_REQUEST"
        assert ei.value.request_id == "req_1"

    async def test_missing_reservation_id_raises_guard_denied(self, async_mock_client, mandate) -> None:
        # Protocol-violation path: server returns ALLOW with no reservation_id and
        # dry_run was NOT requested. The guard must raise rather than silently proceed.
        async_mock_client.create_reservation.return_value = CyclesResponse.success(
            200,
            {
                "decision": "ALLOW",
                "affected_scopes": ["tenant:acme"],
                "reserved": {"unit": "USD_MICROCENTS", "amount": 100},
            },
        )
        with pytest.raises(AP2GuardDenied):
            async with cycles_guard_payment_async(async_mock_client, mandate=mandate, run_id="r", tenant="acme"):
                pass


class TestAsyncDryRun:
    async def test_dry_run_raises_result_and_body_does_not_run(self, async_mock_client, mandate) -> None:
        async_mock_client.create_reservation.return_value = CyclesResponse.success(
            200,
            {
                "decision": "ALLOW",
                "affected_scopes": ["tenant:acme"],
                "scope_path": "tenant:acme",
                "reserved": {"unit": "USD_MICROCENTS", "amount": 19_900_000_000},
            },
        )
        body_ran = {"value": False}

        with pytest.raises(AP2DryRunResult) as ei:
            async with cycles_guard_payment_async(
                async_mock_client, mandate=mandate, run_id="r", tenant="acme", dry_run=True
            ):
                body_ran["value"] = True

        assert body_ran["value"] is False
        assert ei.value.decision == "ALLOW"
        body = async_mock_client.create_reservation.call_args[0][0]
        assert body["dry_run"] is True
        async_mock_client.commit_reservation.assert_not_awaited()
        async_mock_client.release_reservation.assert_not_awaited()

    async def test_dry_run_deny_raises_guard_denied_not_dry_run_result(self, async_mock_client, mandate) -> None:
        # DENY pre-empts the dry-run probe path. Caller sees AP2GuardDenied, not
        # AP2DryRunResult, even with dry_run=True — preserves the "deny is deny"
        # invariant across both sync and async surfaces.
        async_mock_client.create_reservation.return_value = deny_response("BUDGET_EXCEEDED")

        with pytest.raises(AP2GuardDenied) as ei:
            async with cycles_guard_payment_async(
                async_mock_client, mandate=mandate, run_id="r", tenant="acme", dry_run=True
            ):
                pass

        assert ei.value.reason_code == "BUDGET_EXCEEDED"

    async def test_dry_run_result_carries_caps_and_scopes(self, async_mock_client, mandate) -> None:
        # AP2DryRunResult exposes caps/balances/affected_scopes/reason_code so callers
        # can introspect the would-be decision without creating a reservation.
        async_mock_client.create_reservation.return_value = CyclesResponse.success(
            200,
            {
                "decision": "ALLOW_WITH_CAPS",
                "affected_scopes": ["tenant:acme", "agent:bot"],
                "scope_path": "tenant:acme",
                "reserved": {"unit": "USD_MICROCENTS", "amount": 1_000},
                "caps": {"max_tokens": 500},
                "reason_code": "NEAR_LIMIT",
            },
        )

        with pytest.raises(AP2DryRunResult) as ei:
            async with cycles_guard_payment_async(
                async_mock_client, mandate=mandate, run_id="r", tenant="acme", dry_run=True
            ):
                pass

        assert ei.value.decision == "ALLOW_WITH_CAPS"
        assert ei.value.reason_code == "NEAR_LIMIT"
        assert ei.value.affected_scopes == ["tenant:acme", "agent:bot"]
        assert ei.value.caps is not None

    async def test_non_dry_run_does_not_set_flag(self, async_mock_client, mandate) -> None:
        async_mock_client.create_reservation.return_value = allow_response()
        async_mock_client.commit_reservation.return_value = commit_success_response()

        async with cycles_guard_payment_async(async_mock_client, mandate=mandate, run_id="r", tenant="acme"):
            pass

        body = async_mock_client.create_reservation.call_args[0][0]
        assert "dry_run" not in body


# ---------------------------------------------------------------------------
# Commit-uncertain branches
# ---------------------------------------------------------------------------


class TestAsyncCommitUncertain:
    async def test_idempotency_mismatch_raises_uncertain_no_release(self, async_mock_client, mandate) -> None:
        async_mock_client.create_reservation.return_value = allow_response()
        async_mock_client.commit_reservation.return_value = commit_error_response("IDEMPOTENCY_MISMATCH", status=409)

        with pytest.raises(AP2GuardCommitUncertain) as ei:
            async with cycles_guard_payment_async(async_mock_client, mandate=mandate, run_id="r", tenant="acme"):
                pass

        assert ei.value.error_code == "IDEMPOTENCY_MISMATCH"
        async_mock_client.release_reservation.assert_not_awaited()

    async def test_reservation_finalized_raises_uncertain(self, async_mock_client, mandate) -> None:
        # Third terminal-status code on the same branch. The branch covers all three
        # (FINALIZED/EXPIRED/MISMATCH); the parity tests already cover EXPIRED and
        # MISMATCH — adding FINALIZED so a future code change can't break one
        # without surfacing in the suite.
        async_mock_client.create_reservation.return_value = allow_response()
        async_mock_client.commit_reservation.return_value = commit_error_response("RESERVATION_FINALIZED", status=409)

        with pytest.raises(AP2GuardCommitUncertain) as ei:
            async with cycles_guard_payment_async(async_mock_client, mandate=mandate, run_id="r", tenant="acme"):
                pass

        assert ei.value.error_code == "RESERVATION_FINALIZED"
        async_mock_client.release_reservation.assert_not_awaited()

    async def test_reservation_expired_raises_uncertain_no_release(self, async_mock_client, mandate) -> None:
        async_mock_client.create_reservation.return_value = allow_response()
        async_mock_client.commit_reservation.return_value = commit_error_response("RESERVATION_EXPIRED", status=409)

        with pytest.raises(AP2GuardCommitUncertain) as ei:
            async with cycles_guard_payment_async(async_mock_client, mandate=mandate, run_id="r", tenant="acme"):
                pass

        assert ei.value.error_code == "RESERVATION_EXPIRED"
        async_mock_client.release_reservation.assert_not_awaited()

    async def test_commit_5xx_raises_uncertain_no_release(self, async_mock_client, mandate) -> None:
        async_mock_client.create_reservation.return_value = allow_response()
        async_mock_client.commit_reservation.return_value = commit_error_response("INTERNAL_ERROR", status=500)

        with pytest.raises(AP2GuardCommitUncertain) as ei:
            async with cycles_guard_payment_async(async_mock_client, mandate=mandate, run_id="r", tenant="acme"):
                pass

        assert ei.value.error_code == "INTERNAL_ERROR"
        async_mock_client.release_reservation.assert_not_awaited()

    async def test_commit_5xx_without_body_uses_synthetic_code(self, async_mock_client, mandate) -> None:
        # 5xx with no parseable error body → synthesized error_code="SERVER_ERROR" so
        # callers branching on .error_code still get a stable discriminator.
        async_mock_client.create_reservation.return_value = allow_response()
        async_mock_client.commit_reservation.return_value = CyclesResponse.http_error(
            502,
            error_message="bad gateway",
            body=None,
        )

        with pytest.raises(AP2GuardCommitUncertain) as ei:
            async with cycles_guard_payment_async(async_mock_client, mandate=mandate, run_id="r", tenant="acme"):
                pass

        assert ei.value.error_code == "SERVER_ERROR"
        async_mock_client.release_reservation.assert_not_awaited()

    async def test_commit_transport_error_raises_uncertain_no_release(self, async_mock_client, mandate) -> None:
        async_mock_client.create_reservation.return_value = allow_response()
        async_mock_client.commit_reservation.return_value = CyclesResponse.transport_error(
            ConnectionError("network down")
        )

        with pytest.raises(AP2GuardCommitUncertain) as ei:
            async with cycles_guard_payment_async(async_mock_client, mandate=mandate, run_id="r", tenant="acme"):
                pass

        assert ei.value.error_code == "TRANSPORT_ERROR"
        async_mock_client.release_reservation.assert_not_awaited()

    async def test_commit_raises_surfaces_as_uncertain(self, async_mock_client, mandate) -> None:
        async_mock_client.create_reservation.return_value = allow_response()
        async_mock_client.commit_reservation.side_effect = ConnectionError("low-level boom")

        with pytest.raises(AP2GuardCommitUncertain) as ei:
            async with cycles_guard_payment_async(async_mock_client, mandate=mandate, run_id="r", tenant="acme"):
                pass

        assert ei.value.error_code == "COMMIT_RAISED"
        assert isinstance(ei.value.__cause__, ConnectionError)
        async_mock_client.release_reservation.assert_not_awaited()

    async def test_commit_cancellation_surfaces_as_uncertain(self, async_mock_client, mandate) -> None:
        # P1 regression: asyncio.CancelledError is a BaseException, so ``except
        # Exception`` (the COMMIT_RAISED branch) does NOT catch it. Without the
        # explicit CancelledError handler, an outer timeout/cancel mid-flight would
        # escape as raw CancelledError with no reservation_id or error_code, despite
        # the post-PSP unknown-outcome contract saying it must raise
        # AP2GuardCommitUncertain. Wrapping converts to the domain exception so the
        # caller's reconciliation handler still runs; the original CancelledError
        # rides on __cause__ for callers who want to detect it explicitly.
        import asyncio

        async_mock_client.create_reservation.return_value = allow_response("rsv_cancel")
        async_mock_client.commit_reservation.side_effect = asyncio.CancelledError()

        with pytest.raises(AP2GuardCommitUncertain) as ei:
            async with cycles_guard_payment_async(async_mock_client, mandate=mandate, run_id="r", tenant="acme"):
                pass

        assert ei.value.error_code == "COMMIT_CANCELLED"
        assert ei.value.reservation_id == "rsv_cancel"
        assert isinstance(ei.value.__cause__, asyncio.CancelledError)
        # CRITICAL: no release attempt — commit may have settled before cancel landed.
        async_mock_client.release_reservation.assert_not_awaited()


# ---------------------------------------------------------------------------
# Commit-failed (4xx unrecognized)
# ---------------------------------------------------------------------------


class TestAsyncCommitFailed:
    async def test_unrecognized_4xx_releases_and_raises(self, async_mock_client, mandate) -> None:
        async_mock_client.create_reservation.return_value = allow_response()
        async_mock_client.commit_reservation.return_value = commit_error_response("INVALID_REQUEST", status=400)
        async_mock_client.release_reservation.return_value = release_success_response()

        with pytest.raises(AP2GuardCommitFailed) as ei:
            async with cycles_guard_payment_async(async_mock_client, mandate=mandate, run_id="r", tenant="acme"):
                pass

        assert ei.value.error_code == "INVALID_REQUEST"
        assert ei.value.released is True
        async_mock_client.release_reservation.assert_awaited_once()

    async def test_commit_failed_records_release_transport_failure(self, async_mock_client, mandate) -> None:
        # Release transport-fails after a 4xx commit rejection. AP2GuardCommitFailed
        # must report released=False with the chained exception text in release_error,
        # so operators can distinguish "budget recovered" from "budget stranded".
        async_mock_client.create_reservation.return_value = allow_response()
        async_mock_client.commit_reservation.return_value = commit_error_response("INVALID_REQUEST", status=400)
        async_mock_client.release_reservation.side_effect = ConnectionError("network down")

        with pytest.raises(AP2GuardCommitFailed) as ei:
            async with cycles_guard_payment_async(async_mock_client, mandate=mandate, run_id="r", tenant="acme"):
                pass

        assert ei.value.error_code == "INVALID_REQUEST"
        assert ei.value.released is False
        assert ei.value.release_error is not None
        assert "ConnectionError" in ei.value.release_error
        assert "FAILED" in str(ei.value) and "stranded" in str(ei.value)

    async def test_commit_failed_records_release_non_success(self, async_mock_client, mandate) -> None:
        # Release returns 5xx after a 4xx commit rejection. released=False and the
        # response status code surfaces in release_error.
        async_mock_client.create_reservation.return_value = allow_response()
        async_mock_client.commit_reservation.return_value = commit_error_response("INVALID_REQUEST", status=400)
        async_mock_client.release_reservation.return_value = commit_error_response("INTERNAL_ERROR", status=500)

        with pytest.raises(AP2GuardCommitFailed) as ei:
            async with cycles_guard_payment_async(async_mock_client, mandate=mandate, run_id="r", tenant="acme"):
                pass

        assert ei.value.released is False
        assert ei.value.release_error is not None
        assert "500" in ei.value.release_error


# ---------------------------------------------------------------------------
# AP2-shape adapter end-to-end through AsyncGuardedPayment
# ---------------------------------------------------------------------------


class TestAsyncFromAp2EndToEnd:
    """Adapter → async guard → wire: confirms the AP2 sample-type shape flows through
    the full async stack (not just the sync one covered by test_ap2_shape_adapter.py)."""

    async def test_from_ap2_shape_flows_through_async_guard(self, async_mock_client) -> None:
        from dataclasses import dataclass

        from runcycles_ap2 import AP2Mandate

        @dataclass
        class _PaymentAmount:
            value: str
            currency: str

        @dataclass
        class _Payee:
            website: str

        @dataclass
        class _PaymentMandate:
            transaction_id: str
            payment_amount: _PaymentAmount
            payee: _Payee

        @dataclass
        class _CheckoutMandate:
            hash: str

        async_mock_client.create_reservation.return_value = allow_response("rsv_async_e2e")
        async_mock_client.commit_reservation.return_value = commit_success_response()

        pm = _PaymentMandate(
            transaction_id="ap2-tx-async-e2e",
            payment_amount=_PaymentAmount(value="12.50", currency="USD"),
            payee=_Payee(website="merchant.example"),
        )
        cm = _CheckoutMandate(hash="ch_async_e2e")
        mandate = AP2Mandate.from_ap2(pm, cm, open_mandate_hash="omh_async_e2e")

        async with cycles_guard_payment_async(async_mock_client, mandate=mandate, run_id="r", tenant="acme") as guard:
            assert guard.reservation_id == "rsv_async_e2e"

        body = async_mock_client.create_reservation.call_args[0][0]
        assert body["subject"]["dimensions"]["ap2_transaction_id"] == "ap2-tx-async-e2e"
        assert body["subject"]["dimensions"]["checkout_hash"] == "ch_async_e2e"
        assert body["subject"]["dimensions"]["open_mandate_hash"] == "omh_async_e2e"
        # open_mandate_hash present → lock scope shifts to open_mandate (AP2 §6).
        assert ":open_mandate:" in body["idempotency_key"]
