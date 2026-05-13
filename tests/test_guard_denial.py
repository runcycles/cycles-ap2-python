"""Server Decision.DENY on enter → AP2GuardDenied raised; real money never moves."""

from __future__ import annotations

import pytest
from runcycles.response import CyclesResponse

from runcycles_ap2 import AP2GuardDenied, cycles_guard_payment
from tests.conftest import deny_response


class TestDenial:
    def test_deny_decision_raises_before_psp_call(self, mock_client, mandate) -> None:
        mock_client.create_reservation.return_value = deny_response("BUDGET_EXCEEDED")
        sentinel = {"psp_called": False}

        with pytest.raises(AP2GuardDenied) as ei:
            with cycles_guard_payment(mock_client, mandate=mandate, run_id="r", tenant="acme") as _:
                sentinel["psp_called"] = True  # must NOT run

        assert sentinel["psp_called"] is False
        assert ei.value.reason_code == "BUDGET_EXCEEDED"
        mock_client.commit_reservation.assert_not_called()
        mock_client.release_reservation.assert_not_called()

    def test_http_error_response_raises_guard_denied(self, mock_client, mandate) -> None:
        mock_client.create_reservation.return_value = CyclesResponse.http_error(
            400,
            error_message="bad request",
            body={"error": "INVALID_REQUEST", "message": "bad", "request_id": "req_1"},
        )

        with pytest.raises(AP2GuardDenied) as ei:
            with cycles_guard_payment(mock_client, mandate=mandate, run_id="r", tenant="acme") as _:
                pass

        assert ei.value.reason_code == "INVALID_REQUEST"
        assert ei.value.request_id == "req_1"

    def test_missing_reservation_id_raises_guard_denied(self, mock_client, mandate) -> None:
        # ALLOW decision but no reservation_id — protocol violation.
        mock_client.create_reservation.return_value = CyclesResponse.success(
            200,
            {
                "decision": "ALLOW",
                "affected_scopes": ["tenant:acme"],
                "reserved": {"unit": "USD_MICROCENTS", "amount": 100},
            },
        )
        with pytest.raises(AP2GuardDenied):
            with cycles_guard_payment(mock_client, mandate=mandate, run_id="r", tenant="acme") as _:
                pass
