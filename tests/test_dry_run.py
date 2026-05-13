"""Dry-run: reserve request carries dry_run=true; no commit, no release on exit."""

from __future__ import annotations

import pytest
from runcycles.response import CyclesResponse

from runcycles_ap2 import AP2GuardDenied, cycles_guard_payment
from tests.conftest import allow_response, deny_response


class TestDryRun:
    def test_dry_run_flag_sent_and_no_commit_called(self, mock_client, mandate) -> None:
        # Server can return an ALLOW with no reservation_id for dry-runs.
        mock_client.create_reservation.return_value = CyclesResponse.success(
            200,
            {
                "decision": "ALLOW",
                "affected_scopes": ["tenant:acme"],
                "scope_path": "tenant:acme",
                "reserved": {"unit": "USD_MICROCENTS", "amount": 19_900_000_000},
            },
        )

        with cycles_guard_payment(
            mock_client,
            mandate=mandate,
            run_id="r",
            tenant="acme",
            dry_run=True,
        ) as guard:
            assert guard.decision is not None
            assert guard.decision.value == "ALLOW"
            assert guard.reservation_id is None

        body = mock_client.create_reservation.call_args[0][0]
        assert body["dry_run"] is True
        mock_client.commit_reservation.assert_not_called()
        mock_client.release_reservation.assert_not_called()

    def test_dry_run_deny_raises_guard_denied(self, mock_client, mandate) -> None:
        mock_client.create_reservation.return_value = deny_response("BUDGET_EXCEEDED")

        with pytest.raises(AP2GuardDenied):
            with cycles_guard_payment(
                mock_client,
                mandate=mandate,
                run_id="r",
                tenant="acme",
                dry_run=True,
            ):
                pass

    def test_non_dry_run_does_not_set_flag(self, mock_client, mandate) -> None:
        mock_client.create_reservation.return_value = allow_response()
        from tests.conftest import commit_success_response

        mock_client.commit_reservation.return_value = commit_success_response()

        with cycles_guard_payment(mock_client, mandate=mandate, run_id="r", tenant="acme"):
            pass

        body = mock_client.create_reservation.call_args[0][0]
        assert "dry_run" not in body
