"""Dry-run: ``__enter__`` raises ``AP2DryRunResult`` so the ``with`` body never runs.

Earlier versions returned a guard and let the body execute, which let real PSP calls move
money with no Cycles record. The new contract: dry-run is a policy probe, period — the
caller MUST catch the exception to read the decision.
"""

from __future__ import annotations

import pytest
from runcycles.response import CyclesResponse

from runcycles_ap2 import AP2DryRunResult, AP2GuardDenied, cycles_guard_payment
from tests.conftest import allow_response, deny_response


class TestDryRun:
    def test_allow_decision_raises_dry_run_result_and_body_does_not_run(self, mock_client, mandate) -> None:
        # Server returns ALLOW without a reservation_id (the dry_run signature).
        mock_client.create_reservation.return_value = CyclesResponse.success(
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
            with cycles_guard_payment(
                mock_client,
                mandate=mandate,
                run_id="r",
                tenant="acme",
                dry_run=True,
            ) as _:
                body_ran["value"] = True  # MUST NOT execute

        assert body_ran["value"] is False
        assert ei.value.decision == "ALLOW"
        body = mock_client.create_reservation.call_args[0][0]
        assert body["dry_run"] is True
        mock_client.commit_reservation.assert_not_called()
        mock_client.release_reservation.assert_not_called()

    def test_dry_run_deny_raises_guard_denied_not_dry_run_result(self, mock_client, mandate) -> None:
        # DENY is still DENY — that signal pre-empts the dry-run probe path.
        mock_client.create_reservation.return_value = deny_response("BUDGET_EXCEEDED")

        with pytest.raises(AP2GuardDenied) as ei:
            with cycles_guard_payment(
                mock_client,
                mandate=mandate,
                run_id="r",
                tenant="acme",
                dry_run=True,
            ):
                pass

        assert ei.value.reason_code == "BUDGET_EXCEEDED"

    def test_dry_run_result_carries_caps_and_scopes(self, mock_client, mandate) -> None:
        mock_client.create_reservation.return_value = CyclesResponse.success(
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
            with cycles_guard_payment(
                mock_client,
                mandate=mandate,
                run_id="r",
                tenant="acme",
                dry_run=True,
            ):
                pass

        assert ei.value.decision == "ALLOW_WITH_CAPS"
        assert ei.value.reason_code == "NEAR_LIMIT"
        assert ei.value.affected_scopes == ["tenant:acme", "agent:bot"]
        assert ei.value.caps is not None

    def test_non_dry_run_does_not_set_flag(self, mock_client, mandate) -> None:
        mock_client.create_reservation.return_value = allow_response()
        from tests.conftest import commit_success_response

        mock_client.commit_reservation.return_value = commit_success_response()

        with cycles_guard_payment(mock_client, mandate=mandate, run_id="r", tenant="acme"):
            pass

        body = mock_client.create_reservation.call_args[0][0]
        assert "dry_run" not in body
