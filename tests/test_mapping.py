"""Pure unit tests for mapping.py — no network, no client."""

from __future__ import annotations

import pytest

from runcycles_ap2._constants import (
    CUSTOM_CURRENCY,
    CUSTOM_PAYMENT_PROTOCOL,
    DIM_AP2_TRANSACTION_ID,
    DIM_CHECKOUT_HASH,
    DIM_OPEN_MANDATE_HASH,
    DIM_RUN_ID,
)
from runcycles_ap2.exceptions import AP2CurrencyError, AP2MandateError
from runcycles_ap2.mapping import (
    build_action,
    build_commit_body,
    build_estimate,
    build_release_body,
    build_reservation_body,
    build_subject,
    idempotency_key,
)
from runcycles_ap2.models import AP2Mandate
from tests.conftest import make_mandate


class TestIdempotencyKey:
    def test_reserve_phase(self) -> None:
        assert idempotency_key("ap2-tx-001", "reserve") == "ap2:ap2-tx-001:reserve"

    def test_commit_phase(self) -> None:
        assert idempotency_key("ap2-tx-001", "commit") == "ap2:ap2-tx-001:commit"

    def test_release_with_suffix(self) -> None:
        assert idempotency_key("ap2-tx-001", "release", "RuntimeError") == "ap2:ap2-tx-001:release:RuntimeError"

    def test_release_sanitizes_unsafe_chars(self) -> None:
        # Spaces and slashes must be replaced so the key stays a valid header value.
        key = idempotency_key("tx", "release", "some/bad value")
        assert key == "ap2:tx:release:some_bad_value"

    def test_total_length_capped(self) -> None:
        long_tx = "x" * 400
        key = idempotency_key(long_tx, "commit")
        assert len(key) <= 256


class TestBuildSubject:
    def test_minimal(self) -> None:
        m = make_mandate(checkout_hash=None)
        s = build_subject(
            m,
            run_id="run1",
            tenant="acme",
            workspace=None,
            app=None,
            workflow=None,
            agent=None,
            toolset=None,
        )
        assert s["tenant"] == "acme"
        assert s["dimensions"][DIM_RUN_ID] == "run1"
        assert s["dimensions"][DIM_AP2_TRANSACTION_ID] == "ap2-tx-001"
        assert DIM_CHECKOUT_HASH not in s["dimensions"]

    def test_full(self) -> None:
        m = make_mandate(checkout_hash="ch_x", open_mandate_hash="omh_y")
        s = build_subject(
            m,
            run_id="run1",
            tenant="acme",
            workspace="prod",
            app="checkout",
            workflow="ap2-hnp",
            agent="bot",
            toolset="psp",
            extra_dimensions={"customer_segment": "vip"},
        )
        assert s["agent"] == "bot"
        assert s["dimensions"][DIM_CHECKOUT_HASH] == "ch_x"
        assert s["dimensions"][DIM_OPEN_MANDATE_HASH] == "omh_y"
        assert s["dimensions"]["customer_segment"] == "vip"

    def test_canonical_dimensions_win_over_extras(self) -> None:
        m = make_mandate()
        s = build_subject(
            m,
            run_id="r",
            tenant="t",
            workspace=None,
            app=None,
            workflow=None,
            agent=None,
            toolset=None,
            extra_dimensions={DIM_RUN_ID: "hijacked"},
        )
        assert s["dimensions"][DIM_RUN_ID] == "r"


class TestBuildAction:
    def test_default_kind(self) -> None:
        a = build_action(make_mandate(), action_kind="payment.charge")
        assert a["kind"] == "payment.charge"
        assert a["policy_keys"]["host"] == "merchant.example"
        assert a["policy_keys"]["custom"][CUSTOM_PAYMENT_PROTOCOL] == "ap2"
        assert a["policy_keys"]["custom"][CUSTOM_CURRENCY] == "USD"

    def test_action_kind_override(self) -> None:
        a = build_action(make_mandate(), action_kind="payment.refund")
        assert a["kind"] == "payment.refund"


class TestBuildEstimate:
    def test_usd_micros(self) -> None:
        e = build_estimate(make_mandate(amount_value="1.50"))
        assert e == {"unit": "USD_MICROCENTS", "amount": 150_000_000}

    def test_zero_allowed(self) -> None:
        e = build_estimate(make_mandate(amount_value="0"))
        assert e["amount"] == 0


class TestAmountMicros:
    def test_non_usd_raises(self) -> None:
        m = AP2Mandate(
            transaction_id="tx",
            amount_value="1.00",
            currency="EUR",
            payee_website="x.example",
        )
        with pytest.raises(AP2CurrencyError):
            m.amount_micros()

    def test_negative_raises(self) -> None:
        m = AP2Mandate(
            transaction_id="tx",
            amount_value="-1.00",
            currency="USD",
            payee_website="x.example",
        )
        with pytest.raises(AP2MandateError):
            m.amount_micros()

    def test_garbage_value_raises(self) -> None:
        m = AP2Mandate(
            transaction_id="tx",
            amount_value="not-a-number",
            currency="USD",
            payee_website="x.example",
        )
        with pytest.raises(AP2MandateError):
            m.amount_micros()


class TestBuildReservationBody:
    def test_full_shape(self) -> None:
        body = build_reservation_body(
            make_mandate(),
            run_id="run1",
            tenant="acme",
            workspace=None,
            app=None,
            workflow=None,
            agent="bot",
            toolset=None,
            action_kind="payment.charge",
            ttl_ms=60_000,
            overage_policy="REJECT",
            dry_run=False,
        )
        assert body["idempotency_key"] == "ap2:ap2-tx-001:reserve"
        assert body["subject"]["tenant"] == "acme"
        assert body["action"]["kind"] == "payment.charge"
        assert body["estimate"] == {"unit": "USD_MICROCENTS", "amount": 19_900_000_000}
        assert body["ttl_ms"] == 60_000
        assert body["overage_policy"] == "REJECT"
        assert "dry_run" not in body

    def test_dry_run_flag(self) -> None:
        body = build_reservation_body(
            make_mandate(),
            run_id="run1",
            tenant="acme",
            workspace=None,
            app=None,
            workflow=None,
            agent=None,
            toolset=None,
            action_kind="payment.charge",
            ttl_ms=60_000,
            overage_policy="REJECT",
            dry_run=True,
        )
        assert body["dry_run"] is True

    def test_metadata_passthrough(self) -> None:
        body = build_reservation_body(
            make_mandate(),
            run_id="run1",
            tenant="acme",
            workspace=None,
            app=None,
            workflow=None,
            agent=None,
            toolset=None,
            action_kind="payment.charge",
            ttl_ms=60_000,
            overage_policy="REJECT",
            dry_run=False,
            metadata={"trace_id": "abc"},
        )
        assert body["metadata"] == {"trace_id": "abc"}


class TestBuildCommitBody:
    def test_defaults_to_mandate_amount(self) -> None:
        body = build_commit_body(make_mandate())
        assert body["actual"]["amount"] == 19_900_000_000
        assert body["idempotency_key"] == "ap2:ap2-tx-001:commit"

    def test_actual_override(self) -> None:
        body = build_commit_body(make_mandate(), actual_micros=5_000_000_000)
        assert body["actual"]["amount"] == 5_000_000_000

    def test_metadata_attached(self) -> None:
        body = build_commit_body(make_mandate(), metadata={"psp_ref": "psp_1"})
        assert body["metadata"]["psp_ref"] == "psp_1"


class TestBuildReleaseBody:
    def test_with_exception_type(self) -> None:
        body = build_release_body(make_mandate(), reason="fail", exception_type="RuntimeError")
        assert body["idempotency_key"] == "ap2:ap2-tx-001:release:RuntimeError"
        assert body["reason"] == "fail"

    def test_reason_truncated(self) -> None:
        body = build_release_body(make_mandate(), reason="x" * 500, exception_type=None)
        assert len(body["reason"]) == 256


class TestFromAp2:
    def test_minimum_fields(self) -> None:
        class FakeAmount:
            value = "12.34"
            currency = "USD"

        class FakePayee:
            website = "shop.example"

        class FakePM:
            transaction_id = "tx-xyz"
            payment_amount = FakeAmount()
            payee = FakePayee()

        m = AP2Mandate.from_ap2(FakePM())
        assert m.transaction_id == "tx-xyz"
        assert m.amount_value == "12.34"
        assert m.payee_website == "shop.example"

    def test_payee_identifier_fallback(self) -> None:
        class FakeAmount:
            value = "1"
            currency = "USD"

        class FakePayee:
            website = None
            identifier = "merchant_abc"

        class FakePM:
            transaction_id = "tx"
            payment_amount = FakeAmount()
            payee = FakePayee()

        m = AP2Mandate.from_ap2(FakePM())
        assert m.payee_website == "merchant_abc"

    def test_missing_payee_raises(self) -> None:
        class FakeAmount:
            value = "1"
            currency = "USD"

        class FakePayee:
            website = None
            identifier = None

        class FakePM:
            transaction_id = "tx"
            payment_amount = FakeAmount()
            payee = FakePayee()

        with pytest.raises(AP2MandateError):
            AP2Mandate.from_ap2(FakePM())

    def test_missing_field_raises(self) -> None:
        class FakePM:
            pass

        with pytest.raises(AP2MandateError):
            AP2Mandate.from_ap2(FakePM())

    def test_checkout_mandate_hash(self) -> None:
        class FakeAmount:
            value = "1"
            currency = "USD"

        class FakePayee:
            website = "x.example"

        class FakePM:
            transaction_id = "tx"
            payment_amount = FakeAmount()
            payee = FakePayee()

        class FakeCM:
            hash = "ch_test"

        m = AP2Mandate.from_ap2(FakePM(), FakeCM())
        assert m.checkout_hash == "ch_test"
