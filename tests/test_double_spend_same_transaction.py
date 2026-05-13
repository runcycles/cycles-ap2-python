"""Two parallel guards on the same AP2 transaction send identical idempotency keys.

The point is not to simulate Cycles' server-side replay logic in-process — that lives
on the server. The point is to prove the wire payload is deterministic, so the server
sees both attempts under the same idempotency bucket. Per Cycles semantics: same key
+ same payload returns the original response; same key + different payload returns
409 IDEMPOTENCY_MISMATCH. Either way the second attempt cannot create a second valid
reservation — that is the consume-once defense.
"""

from __future__ import annotations

from runcycles_ap2 import cycles_guard_payment
from runcycles_ap2.mapping import idempotency_key
from tests.conftest import allow_response, commit_success_response, make_mandate


class TestDoubleSpend:
    def test_two_guards_same_transaction_send_same_reserve_key(self, mock_client) -> None:
        mock_client.create_reservation.return_value = allow_response("rsv_shared")
        mock_client.commit_reservation.return_value = commit_success_response()

        m1 = make_mandate(transaction_id="ap2-tx-shared")
        m2 = make_mandate(transaction_id="ap2-tx-shared")

        with cycles_guard_payment(mock_client, mandate=m1, run_id="run_a", tenant="acme"):
            pass
        with cycles_guard_payment(mock_client, mandate=m2, run_id="run_b", tenant="acme"):
            pass

        body1 = mock_client.create_reservation.call_args_list[0][0][0]
        body2 = mock_client.create_reservation.call_args_list[1][0][0]
        expected_reserve = idempotency_key(make_mandate(transaction_id="ap2-tx-shared"), "reserve")
        expected_commit = idempotency_key(make_mandate(transaction_id="ap2-tx-shared"), "commit")
        assert body1["idempotency_key"] == body2["idempotency_key"] == expected_reserve
        # Commit keys also match — the server sees both under one bucket and either
        # replays the original commit response or returns IDEMPOTENCY_MISMATCH.
        commit1 = mock_client.commit_reservation.call_args_list[0][0][1]
        commit2 = mock_client.commit_reservation.call_args_list[1][0][1]
        assert commit1["idempotency_key"] == commit2["idempotency_key"] == expected_commit

    def test_different_transactions_produce_different_keys(self, mock_client) -> None:
        mock_client.create_reservation.return_value = allow_response()
        mock_client.commit_reservation.return_value = commit_success_response()

        with cycles_guard_payment(mock_client, mandate=make_mandate(transaction_id="t1"), run_id="r", tenant="acme"):
            pass
        with cycles_guard_payment(mock_client, mandate=make_mandate(transaction_id="t2"), run_id="r", tenant="acme"):
            pass

        body1 = mock_client.create_reservation.call_args_list[0][0][0]
        body2 = mock_client.create_reservation.call_args_list[1][0][0]
        assert body1["idempotency_key"] != body2["idempotency_key"]

    def test_open_mandate_divergent_payload_reserve_mismatch_raises_denied(self, mock_client) -> None:
        # The "or 409 IDEMPOTENCY_MISMATCH" half of the consume-once contract.
        # First reserve succeeds. Second guard, same open_mandate_hash but different
        # transaction_id, will hit the same idempotency bucket — and because the
        # request payload differs, the server rejects with IDEMPOTENCY_MISMATCH.
        # Our guard surfaces that as AP2GuardDenied with reason_code propagated.
        from runcycles.response import CyclesResponse

        from runcycles_ap2 import AP2GuardDenied
        from tests.conftest import commit_success_response

        first_allow = allow_response("rsv_omh_first")
        mismatch_409 = CyclesResponse.http_error(
            409,
            error_message="IDEMPOTENCY_MISMATCH",
            body={"error": "IDEMPOTENCY_MISMATCH", "message": "payload differs", "request_id": "req_omh"},
        )
        mock_client.create_reservation.side_effect = [first_allow, mismatch_409]
        mock_client.commit_reservation.return_value = commit_success_response()

        m1 = make_mandate(transaction_id="ap2-tx-FIRST", open_mandate_hash="omh_shared")
        m2 = make_mandate(transaction_id="ap2-tx-SECOND", open_mandate_hash="omh_shared")

        # First attempt: normal commit path.
        with cycles_guard_payment(mock_client, mandate=m1, run_id="r1", tenant="acme") as g1:
            assert g1.reservation_id == "rsv_omh_first"

        # Second attempt: server sees same key + divergent payload → 409 → denied.
        import pytest

        with pytest.raises(AP2GuardDenied) as ei:
            with cycles_guard_payment(mock_client, mandate=m2, run_id="r2", tenant="acme"):
                # __enter__ raises before this body runs — real money never moves.
                raise AssertionError("body must not execute")

        assert ei.value.reason_code == "IDEMPOTENCY_MISMATCH"
        assert ei.value.request_id == "req_omh"
        # No commit/release on the second attempt — we never created a reservation.
        assert mock_client.commit_reservation.call_count == 1  # only the first guard

    def test_open_mandate_overuse_shares_idempotency_bucket_across_transactions(self, mock_client) -> None:
        # P0-A: this is the AP2 spec consume-once defense (specification §6). Two
        # distinct transaction_ids spawned from one open mandate must share the same
        # reserve key, so the server's (tenant, endpoint, idempotency_key) dedup sees
        # both attempts in one bucket. Identical payloads replay; divergent payloads
        # hit IDEMPOTENCY_MISMATCH (see test_open_mandate_divergent_payload_rejected
        # in test_mapping.py). Either way, no second valid reservation.
        mock_client.create_reservation.return_value = allow_response("rsv_omh_shared")
        mock_client.commit_reservation.return_value = commit_success_response()

        m1 = make_mandate(transaction_id="ap2-tx-CHECKOUT-1", open_mandate_hash="omh_hnp_shared")
        m2 = make_mandate(transaction_id="ap2-tx-CHECKOUT-2", open_mandate_hash="omh_hnp_shared")

        with cycles_guard_payment(mock_client, mandate=m1, run_id="r1", tenant="acme"):
            pass
        with cycles_guard_payment(mock_client, mandate=m2, run_id="r2", tenant="acme"):
            pass

        body1 = mock_client.create_reservation.call_args_list[0][0][0]
        body2 = mock_client.create_reservation.call_args_list[1][0][0]
        # Same reserve key despite different transaction_ids — that's the bucket.
        assert body1["idempotency_key"] == body2["idempotency_key"]
        assert ":open_mandate:" in body1["idempotency_key"]
        # Transaction ids stay distinct in dimensions for audit.
        assert body1["subject"]["dimensions"]["ap2_transaction_id"] == "ap2-tx-CHECKOUT-1"
        assert body2["subject"]["dimensions"]["ap2_transaction_id"] == "ap2-tx-CHECKOUT-2"
