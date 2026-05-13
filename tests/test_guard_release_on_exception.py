"""Exception inside ``with`` → release_reservation with deterministic key."""

from __future__ import annotations

import pytest

from runcycles_ap2 import cycles_guard_payment
from runcycles_ap2.mapping import idempotency_key
from tests.conftest import allow_response, release_success_response


class TestReleaseOnException:
    def test_runtime_error_triggers_release(self, mock_client, mandate) -> None:
        mock_client.create_reservation.return_value = allow_response("rsv_rel")
        mock_client.release_reservation.return_value = release_success_response()

        with pytest.raises(RuntimeError, match="psp failure"):
            with cycles_guard_payment(mock_client, mandate=mandate, run_id="r", tenant="acme") as _:
                raise RuntimeError("psp failure")

        mock_client.release_reservation.assert_called_once()
        called_id, body = mock_client.release_reservation.call_args[0]
        assert called_id == "rsv_rel"
        assert body["idempotency_key"] == idempotency_key("ap2-tx-001", "release", "RuntimeError")
        assert body["reason"].startswith("ap2_guard_failed:RuntimeError")
        mock_client.commit_reservation.assert_not_called()

    def test_value_error_uses_exception_type_in_key(self, mock_client, mandate) -> None:
        mock_client.create_reservation.return_value = allow_response("rsv_ve")
        mock_client.release_reservation.return_value = release_success_response()

        with pytest.raises(ValueError):
            with cycles_guard_payment(mock_client, mandate=mandate, run_id="r", tenant="acme") as _:
                raise ValueError("nope")

        body = mock_client.release_reservation.call_args[0][1]
        assert body["idempotency_key"] == idempotency_key("ap2-tx-001", "release", "ValueError")

    def test_abort_releases_on_clean_exit(self, mock_client, mandate) -> None:
        mock_client.create_reservation.return_value = allow_response("rsv_abort")
        mock_client.release_reservation.return_value = release_success_response()

        with cycles_guard_payment(mock_client, mandate=mandate, run_id="r", tenant="acme") as guard:
            guard.abort("psp_returned_failure")

        mock_client.commit_reservation.assert_not_called()
        mock_client.release_reservation.assert_called_once()
        body = mock_client.release_reservation.call_args[0][1]
        assert "psp_returned_failure" in body["reason"]
        assert guard.committed is False

    def test_release_failure_is_swallowed(self, mock_client, mandate) -> None:
        mock_client.create_reservation.return_value = allow_response()
        mock_client.release_reservation.side_effect = ConnectionError("transport down")

        # The original exception must propagate even if release itself fails.
        with pytest.raises(RuntimeError, match="boom"):
            with cycles_guard_payment(mock_client, mandate=mandate, run_id="r", tenant="acme") as _:
                raise RuntimeError("boom")
