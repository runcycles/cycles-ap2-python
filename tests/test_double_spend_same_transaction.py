"""Two parallel guards on the same AP2 transaction send identical idempotency keys.

The point is not to simulate Cycles' server-side replay logic in-process — that lives
on the server. The point is to prove the wire payload is deterministic, so the server
*can* dedupe. If two workers compute the same reserve key from the same mandate, the
server's `(tenant, endpoint, idempotency_key)` dedup will collapse them onto one
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
        expected_reserve = idempotency_key("ap2-tx-shared", "reserve")
        expected_commit = idempotency_key("ap2-tx-shared", "commit")
        assert body1["idempotency_key"] == body2["idempotency_key"] == expected_reserve
        # Commit keys also collide — the server collapses both onto one reservation.
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
