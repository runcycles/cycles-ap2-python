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
