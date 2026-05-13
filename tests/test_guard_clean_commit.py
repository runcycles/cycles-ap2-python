"""Clean exit → commit_reservation with the deterministic AP2 commit key."""

from __future__ import annotations

import pytest

from runcycles_ap2 import AP2MandateError, cycles_guard_payment
from runcycles_ap2._constants import MAX_USD_MICROS
from runcycles_ap2.mapping import idempotency_key
from tests.conftest import allow_response, commit_success_response


class TestCleanCommit:
    def test_commit_called_with_ap2_idempotency_key(self, mock_client, mandate) -> None:
        mock_client.create_reservation.return_value = allow_response("rsv_clean_001")
        mock_client.commit_reservation.return_value = commit_success_response()

        with cycles_guard_payment(mock_client, mandate=mandate, run_id="run_1", tenant="acme", agent="bot") as guard:
            assert guard.reservation_id == "rsv_clean_001"
            assert guard.decision is not None
            assert guard.decision.value == "ALLOW"

        assert guard.committed is True
        mock_client.commit_reservation.assert_called_once()
        called_id, called_body = mock_client.commit_reservation.call_args[0]
        assert called_id == "rsv_clean_001"
        assert called_body["idempotency_key"] == idempotency_key(mandate, "commit")
        assert called_body["actual"] == {"unit": "USD_MICROCENTS", "amount": 19_900_000_000}
        mock_client.release_reservation.assert_not_called()

    def test_actual_micros_override_is_committed(self, mock_client, mandate) -> None:
        mock_client.create_reservation.return_value = allow_response()
        mock_client.commit_reservation.return_value = commit_success_response()

        with cycles_guard_payment(mock_client, mandate=mandate, run_id="r", tenant="acme") as guard:
            guard.set_actual_micros(5_000_000_000)

        body = mock_client.commit_reservation.call_args[0][1]
        assert body["actual"]["amount"] == 5_000_000_000

    def test_attach_receipt_fields_lands_in_commit_metadata_and_receipt(self, mock_client, mandate) -> None:
        mock_client.create_reservation.return_value = allow_response()
        mock_client.commit_reservation.return_value = commit_success_response()

        with cycles_guard_payment(mock_client, mandate=mandate, run_id="r", tenant="acme", agent="bot") as guard:
            guard.attach_receipt_fields(psp_ref="psp_abc", trace_id="t1")

        body = mock_client.commit_reservation.call_args[0][1]
        assert body["metadata"]["psp_ref"] == "psp_abc"
        assert body["metadata"]["trace_id"] == "t1"
        assert guard.receipt is not None
        assert guard.receipt.psp_ref == "psp_abc"
        assert guard.receipt.extra == {"trace_id": "t1"}
        assert guard.receipt.committed is True
        assert guard.receipt.action_kind == "payment.charge"

    def test_emit_receipt_false_skips_receipt(self, mock_client, mandate) -> None:
        mock_client.create_reservation.return_value = allow_response()
        mock_client.commit_reservation.return_value = commit_success_response()

        with cycles_guard_payment(
            mock_client,
            mandate=mandate,
            run_id="r",
            tenant="acme",
            emit_receipt=False,
        ) as guard:
            pass

        assert guard.receipt is None
        assert guard.committed is True

    def test_set_actual_micros_at_int64_max_accepted(self, mock_client, mandate) -> None:
        # Boundary: int64.max is a legal commit amount.
        mock_client.create_reservation.return_value = allow_response()
        mock_client.commit_reservation.return_value = commit_success_response()

        with cycles_guard_payment(mock_client, mandate=mandate, run_id="r", tenant="acme") as guard:
            guard.set_actual_micros(MAX_USD_MICROS)

        body = mock_client.commit_reservation.call_args[0][1]
        assert body["actual"]["amount"] == MAX_USD_MICROS

    def test_set_actual_micros_above_int64_rejected(self, mock_client, mandate) -> None:
        # P2 regression: AP2Mandate.amount_micros enforces the int64 cap on
        # mandate-derived amounts, but set_actual_micros() used to only reject
        # negatives. A caller could pass 2**63 and we'd ship it.
        from tests.conftest import release_success_response

        mock_client.create_reservation.return_value = allow_response()
        mock_client.release_reservation.return_value = release_success_response()

        with pytest.raises(AP2MandateError, match="int64"):
            with cycles_guard_payment(mock_client, mandate=mandate, run_id="r", tenant="acme") as guard:
                guard.set_actual_micros(MAX_USD_MICROS + 1)

        # Raise inside the body → release on exit, no commit.
        mock_client.commit_reservation.assert_not_called()
        mock_client.release_reservation.assert_called_once()

    def test_set_actual_micros_negative_rejected(self, mock_client, mandate) -> None:
        from tests.conftest import release_success_response

        mock_client.create_reservation.return_value = allow_response()
        mock_client.release_reservation.return_value = release_success_response()

        with pytest.raises(AP2MandateError, match="non-negative"):
            with cycles_guard_payment(mock_client, mandate=mandate, run_id="r", tenant="acme") as guard:
                guard.set_actual_micros(-1)

    @pytest.mark.parametrize("bad", [True, False, 1.5, 1.0, "100", None, [100]])
    def test_set_actual_micros_rejects_non_int_types(self, mock_client, mandate, bad) -> None:
        # P2 regression: bool is a subclass of int and float compares numerically, so
        # the bound check passes for True/1.5 and the value would flow to the wire.
        # set_actual_micros must reject anything that isn't a plain int up front.
        from tests.conftest import release_success_response

        mock_client.create_reservation.return_value = allow_response()
        mock_client.release_reservation.return_value = release_success_response()

        with pytest.raises(AP2MandateError, match="must be an int"):
            with cycles_guard_payment(mock_client, mandate=mandate, run_id="r", tenant="acme") as guard:
                guard.set_actual_micros(bad)  # type: ignore[arg-type]

        mock_client.commit_reservation.assert_not_called()

    def test_reserve_body_carries_policy_keys(self, mock_client, mandate) -> None:
        mock_client.create_reservation.return_value = allow_response()
        mock_client.commit_reservation.return_value = commit_success_response()

        with cycles_guard_payment(mock_client, mandate=mandate, run_id="r", tenant="acme") as _:
            pass

        body = mock_client.create_reservation.call_args[0][0]
        assert body["idempotency_key"] == idempotency_key(mandate, "reserve")
        assert body["action"]["policy_keys"]["host"] == "merchant.example"
        assert body["action"]["policy_keys"]["custom"]["payment_protocol"] == "ap2"
        assert body["subject"]["dimensions"]["ap2_transaction_id"] == "ap2-tx-001"
        assert body["overage_policy"] == "REJECT"
