"""Idempotency: same mandate sent to server reuses the deterministic AP2 key."""

from __future__ import annotations

import pytest

from runcycles_ap2 import AP2GuardCommitUncertain, cycles_guard_payment
from runcycles_ap2.mapping import idempotency_key
from tests.conftest import allow_response, commit_error_response, commit_success_response


class TestIdempotency:
    def test_reserve_key_is_deterministic_per_transaction(self, mock_client, mandate) -> None:
        mock_client.create_reservation.return_value = allow_response()
        mock_client.commit_reservation.return_value = commit_success_response()

        with cycles_guard_payment(mock_client, mandate=mandate, run_id="r", tenant="acme") as _:
            pass

        body = mock_client.create_reservation.call_args[0][0]
        assert body["idempotency_key"] == idempotency_key(mandate, "reserve")

    def test_commit_idempotency_mismatch_raises_uncertain(self, mock_client, mandate) -> None:
        # P0-B: previously silent. After the PSP body has run, IDEMPOTENCY_MISMATCH means
        # a prior commit under this key carried a different payload — that's a
        # reconciliation event the caller MUST handle. We still don't auto-release
        # (a prior successful commit may exist).
        mock_client.create_reservation.return_value = allow_response()
        mock_client.commit_reservation.return_value = commit_error_response("IDEMPOTENCY_MISMATCH", status=409)

        with pytest.raises(AP2GuardCommitUncertain) as ei:
            with cycles_guard_payment(mock_client, mandate=mandate, run_id="r", tenant="acme") as _:
                pass

        assert ei.value.error_code == "IDEMPOTENCY_MISMATCH"
        assert ei.value.reservation_id == "rsv_ap2_001"
        mock_client.release_reservation.assert_not_called()

    def test_reservation_finalized_raises_uncertain(self, mock_client, mandate) -> None:
        # P0-B: previously silent. Usually a benign replay, but we can't verify "benign"
        # client-side — and the caller may have run a PSP charge before the replay.
        # Surface it; the caller decides whether to ignore.
        mock_client.create_reservation.return_value = allow_response()
        mock_client.commit_reservation.return_value = commit_error_response("RESERVATION_FINALIZED", status=409)

        with pytest.raises(AP2GuardCommitUncertain) as ei:
            with cycles_guard_payment(mock_client, mandate=mandate, run_id="r", tenant="acme") as _:
                pass

        assert ei.value.error_code == "RESERVATION_FINALIZED"
        mock_client.release_reservation.assert_not_called()

    def test_reservation_expired_raises_uncertain(self, mock_client, mandate) -> None:
        # P0-B: the worst case. The PSP body has run; server reclaimed budget on TTL.
        # The caller MUST reconcile — we never want this to be a silent log line.
        mock_client.create_reservation.return_value = allow_response()
        mock_client.commit_reservation.return_value = commit_error_response("RESERVATION_EXPIRED", status=409)

        with pytest.raises(AP2GuardCommitUncertain) as ei:
            with cycles_guard_payment(mock_client, mandate=mandate, run_id="r", tenant="acme") as _:
                pass

        assert ei.value.error_code == "RESERVATION_EXPIRED"
        mock_client.release_reservation.assert_not_called()

    def test_unrecognized_commit_error_releases_and_raises(self, mock_client, mandate) -> None:
        # An unrecognized commit rejection means the PSP may already have charged. We
        # release the reservation AND raise AP2GuardCommitFailed so the caller cannot
        # miss the reconciliation event (the silent `guard.committed == False` signal
        # from earlier versions was too easy to overlook).
        from runcycles_ap2 import AP2GuardCommitFailed
        from tests.conftest import release_success_response

        mock_client.create_reservation.return_value = allow_response()
        mock_client.commit_reservation.return_value = commit_error_response("INVALID_REQUEST", status=400)
        mock_client.release_reservation.return_value = release_success_response()

        with pytest.raises(AP2GuardCommitFailed) as ei:
            with cycles_guard_payment(mock_client, mandate=mandate, run_id="r", tenant="acme") as _:
                pass

        assert ei.value.error_code == "INVALID_REQUEST"
        assert ei.value.reservation_id == "rsv_ap2_001"
        assert ei.value.released is True
        assert ei.value.release_error is None
        assert "released" in str(ei.value)
        mock_client.release_reservation.assert_called_once()
        body = mock_client.release_reservation.call_args[0][1]
        assert "INVALID_REQUEST" in body["reason"]

    def test_commit_failed_records_release_failure(self, mock_client, mandate) -> None:
        # P2 regression: when release fails after a commit rejection, the exception
        # must report `released=False` and surface the release error in its message.
        # Earlier versions said "reservation released" regardless, misleading callers
        # about whether budget had actually been recovered.
        from runcycles_ap2 import AP2GuardCommitFailed

        mock_client.create_reservation.return_value = allow_response()
        mock_client.commit_reservation.return_value = commit_error_response("INVALID_REQUEST", status=400)
        mock_client.release_reservation.side_effect = ConnectionError("network down")

        with pytest.raises(AP2GuardCommitFailed) as ei:
            with cycles_guard_payment(mock_client, mandate=mandate, run_id="r", tenant="acme") as _:
                pass

        assert ei.value.error_code == "INVALID_REQUEST"
        assert ei.value.released is False
        assert ei.value.release_error is not None
        assert "ConnectionError" in ei.value.release_error
        assert "FAILED" in str(ei.value) and "stranded" in str(ei.value)

    def test_commit_failed_records_release_non_success(self, mock_client, mandate) -> None:
        # Same as above but the release POST returned a 5xx — server didn't release.
        from runcycles_ap2 import AP2GuardCommitFailed

        mock_client.create_reservation.return_value = allow_response()
        mock_client.commit_reservation.return_value = commit_error_response("INVALID_REQUEST", status=400)
        mock_client.release_reservation.return_value = commit_error_response("INTERNAL_ERROR", status=500)

        with pytest.raises(AP2GuardCommitFailed) as ei:
            with cycles_guard_payment(mock_client, mandate=mandate, run_id="r", tenant="acme") as _:
                pass

        assert ei.value.released is False
        assert ei.value.release_error is not None
        assert "500" in ei.value.release_error
