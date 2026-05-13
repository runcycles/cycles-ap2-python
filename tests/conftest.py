"""Shared test fixtures."""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import MagicMock

import pytest
from runcycles.client import CyclesClient
from runcycles.response import CyclesResponse

from runcycles_ap2.models import AP2Mandate


def make_mandate(
    *,
    transaction_id: str = "ap2-tx-001",
    amount_value: str = "199.00",
    currency: str = "USD",
    payee_website: str = "merchant.example",
    checkout_hash: str | None = "ch_demo",
    open_mandate_hash: str | None = None,
) -> AP2Mandate:
    return AP2Mandate(
        transaction_id=transaction_id,
        amount_value=amount_value,
        currency=currency,
        payee_website=payee_website,
        checkout_hash=checkout_hash,
        open_mandate_hash=open_mandate_hash,
    )


def allow_response(reservation_id: str = "rsv_ap2_001") -> CyclesResponse:
    return CyclesResponse.success(
        200,
        {
            "decision": "ALLOW",
            "reservation_id": reservation_id,
            "expires_at_ms": int(time.time() * 1000) + 60_000,
            "affected_scopes": ["tenant:acme"],
            "scope_path": "tenant:acme",
            "reserved": {"unit": "USD_MICROCENTS", "amount": 19_900_000_000},
        },
    )


def deny_response(reason_code: str = "BUDGET_EXCEEDED") -> CyclesResponse:
    return CyclesResponse.success(
        200,
        {"decision": "DENY", "affected_scopes": ["tenant:acme"], "reason_code": reason_code},
    )


def commit_success_response(amount: int = 19_900_000_000) -> CyclesResponse:
    return CyclesResponse.success(
        200,
        {"status": "COMMITTED", "charged": {"unit": "USD_MICROCENTS", "amount": amount}},
    )


def release_success_response(amount: int = 19_900_000_000) -> CyclesResponse:
    return CyclesResponse.success(
        200,
        {"status": "RELEASED", "released": {"unit": "USD_MICROCENTS", "amount": amount}},
    )


def commit_error_response(error_code: str, status: int = 409) -> CyclesResponse:
    return CyclesResponse.http_error(
        status,
        error_message=error_code,
        body={"error": error_code, "message": error_code, "request_id": "req_test"},
    )


@pytest.fixture
def mock_client() -> MagicMock:
    """A MagicMock matching the CyclesClient surface used by GuardedPayment."""
    mock = MagicMock(spec=CyclesClient)
    return mock


@pytest.fixture
def mandate() -> AP2Mandate:
    return make_mandate()


@pytest.fixture
def captured() -> dict[str, list[Any]]:
    """Generic dict to capture call bodies across helpers in a test."""
    return {"reserve": [], "commit": [], "release": []}
