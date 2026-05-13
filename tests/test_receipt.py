"""Pure unit tests for receipt.py."""

from __future__ import annotations

from runcycles_ap2._constants import RECEIPT_SCHEMA
from runcycles_ap2.receipt import build_runtime_authority_receipt
from tests.conftest import make_mandate


class TestBuildReceipt:
    def test_minimum_fields(self) -> None:
        r = build_runtime_authority_receipt(
            make_mandate(),
            decision="ALLOW",
            reservation_id="rsv_1",
            tenant="acme",
            agent="bot",
            action_kind="payment.charge",
            policy_keys={"host": "merchant.example"},
            amount_micros=19_900_000_000,
            committed=True,
            issued_at_ms=1_700_000_000_000,
        )
        assert r.schema_name == RECEIPT_SCHEMA
        assert r.decision == "ALLOW"
        assert r.reservation_id == "rsv_1"
        assert r.tenant == "acme"
        assert r.agent == "bot"
        assert r.ap2_transaction_id == "ap2-tx-001"
        assert r.amount_unit == "USD_MICROCENTS"
        assert r.amount_micros == 19_900_000_000
        assert r.committed is True
        assert r.issued_at_ms == 1_700_000_000_000

    def test_serialization_uses_schema_alias(self) -> None:
        r = build_runtime_authority_receipt(
            make_mandate(),
            decision="ALLOW",
            reservation_id="rsv_1",
            tenant="t",
            agent=None,
            action_kind="payment.charge",
            policy_keys={"host": "x"},
            amount_micros=1,
            committed=True,
            issued_at_ms=1,
        )
        dumped = r.model_dump(by_alias=True)
        assert dumped["schema"] == RECEIPT_SCHEMA
        assert "schema_name" not in dumped

    def test_psp_ref_and_extra(self) -> None:
        r = build_runtime_authority_receipt(
            make_mandate(),
            decision="ALLOW",
            reservation_id="rsv_1",
            tenant="t",
            agent=None,
            action_kind="payment.charge",
            policy_keys={"host": "x"},
            amount_micros=1,
            committed=True,
            psp_ref="psp_abc",
            extra={"trace": "t1"},
            issued_at_ms=1,
        )
        assert r.psp_ref == "psp_abc"
        assert r.extra == {"trace": "t1"}

    def test_issued_at_ms_defaults_to_now(self) -> None:
        r = build_runtime_authority_receipt(
            make_mandate(),
            decision="ALLOW",
            reservation_id="rsv_1",
            tenant="t",
            agent=None,
            action_kind="payment.charge",
            policy_keys={"host": "x"},
            amount_micros=1,
            committed=True,
        )
        assert r.issued_at_ms > 1_000_000_000_000  # plausibly a real timestamp
