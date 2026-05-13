"""Adapter test against object shapes mirroring the AP2 public sample types.

The upstream AP2 Python SDK isn't pinned on PyPI yet, so we don't import from it.
Instead this test exercises `AP2Mandate.from_ap2()` against object shapes that
match the current `google-agentic-commerce/AP2` sample-types layout — same field
names and nesting, just without the protobuf/pydantic machinery. If upstream
renames a field, this test fails and we fix the adapter in one place.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from runcycles_ap2 import AP2Mandate, cycles_guard_payment
from runcycles_ap2.mapping import idempotency_key
from tests.conftest import allow_response, commit_success_response

# Mirrors AP2 sample types: `PaymentAmount`, `Payee`, `PaymentMandate`, `CheckoutMandate`.
# Field names are taken from the AP2 spec's payment_mandate / checkout_mandate sections.


@dataclass
class _PaymentAmount:
    value: str
    currency: str


@dataclass
class _Payee:
    website: str
    identifier: str | None = None


@dataclass
class _PaymentMandate:
    transaction_id: str
    payment_amount: _PaymentAmount
    payee: _Payee


@dataclass
class _CheckoutMandate:
    hash: str
    cart: list[str] = field(default_factory=list)  # extra field — should be ignored


class TestAP2ShapeAdapter:
    def test_from_ap2_minimal_human_present_flow(self) -> None:
        pm = _PaymentMandate(
            transaction_id="ap2-tx-real-001",
            payment_amount=_PaymentAmount(value="49.99", currency="USD"),
            payee=_Payee(website="shop.example"),
        )
        mandate = AP2Mandate.from_ap2(pm)

        assert mandate.transaction_id == "ap2-tx-real-001"
        assert mandate.amount_value == "49.99"
        assert mandate.currency == "USD"
        assert mandate.payee_website == "shop.example"
        assert mandate.checkout_hash is None
        assert mandate.open_mandate_hash is None
        # Conversion still exact through the adapter.
        assert mandate.amount_micros() == 4_999_000_000

    def test_from_ap2_human_not_present_with_checkout(self) -> None:
        pm = _PaymentMandate(
            transaction_id="ap2-tx-real-002",
            payment_amount=_PaymentAmount(value="199.00", currency="USD"),
            payee=_Payee(website="merchant.example"),
        )
        cm = _CheckoutMandate(hash="ch_real_002", cart=["item-1", "item-2"])
        mandate = AP2Mandate.from_ap2(pm, cm, open_mandate_hash="omh_real_xyz")

        assert mandate.checkout_hash == "ch_real_002"
        assert mandate.open_mandate_hash == "omh_real_xyz"
        # When open_mandate_hash is present, the consume-once lock keys on it.
        assert ":open_mandate:" in idempotency_key(mandate, "reserve")

    def test_from_ap2_empty_checkout_hash_is_rejected_not_silently_dropped(self) -> None:
        # P3 regression: previously, an upstream `CheckoutMandate(hash="")` would be
        # short-circuited to None via the `or` in from_ap2(), masking the bad data
        # and bypassing the model's min_length=1 check. Now the empty string is
        # preserved and the AP2Mandate constructor raises.
        from pydantic import ValidationError

        pm = _PaymentMandate(
            transaction_id="tx-empty-ch",
            payment_amount=_PaymentAmount(value="1.00", currency="USD"),
            payee=_Payee(website="shop.example"),
        )
        cm = _CheckoutMandate(hash="")  # empty — upstream data corruption

        import pytest

        with pytest.raises(ValidationError):
            AP2Mandate.from_ap2(pm, cm)

    def test_from_ap2_checkout_hash_alt_naming_still_falls_through(self) -> None:
        # When the first attribute is genuinely missing (no `hash` attr at all), the
        # alternate naming (`checkout_hash`) is used — that's the intended fallback.
        @dataclass
        class _CheckoutMandateAltName:
            checkout_hash: str

        pm = _PaymentMandate(
            transaction_id="tx-alt",
            payment_amount=_PaymentAmount(value="1.00", currency="USD"),
            payee=_Payee(website="shop.example"),
        )
        cm = _CheckoutMandateAltName(checkout_hash="ch_via_alt_name")

        mandate = AP2Mandate.from_ap2(pm, cm)
        assert mandate.checkout_hash == "ch_via_alt_name"

    def test_from_ap2_payee_identifier_fallback(self) -> None:
        # Some AP2 sample shapes use `identifier` when no website is set.
        pm = _PaymentMandate(
            transaction_id="ap2-tx-real-003",
            payment_amount=_PaymentAmount(value="1.00", currency="USD"),
            payee=_Payee(website="", identifier="merchant-id-xyz"),  # type: ignore[arg-type]
        )
        # website="" + identifier="merchant-id-xyz" → falls back to identifier
        mandate = AP2Mandate.from_ap2(pm)
        assert mandate.payee_website == "merchant-id-xyz"

    def test_end_to_end_through_guard(self, mock_client) -> None:
        # Sanity: an AP2-shape mandate flows through the guard like any other.
        mock_client.create_reservation.return_value = allow_response("rsv_ap2_real")
        mock_client.commit_reservation.return_value = commit_success_response()

        pm = _PaymentMandate(
            transaction_id="ap2-tx-real-e2e",
            payment_amount=_PaymentAmount(value="12.50", currency="USD"),
            payee=_Payee(website="merchant.example"),
        )
        cm = _CheckoutMandate(hash="ch_e2e")
        mandate = AP2Mandate.from_ap2(pm, cm, open_mandate_hash="omh_e2e")

        with cycles_guard_payment(mock_client, mandate=mandate, run_id="r", tenant="acme") as guard:
            assert guard.reservation_id == "rsv_ap2_real"

        body = mock_client.create_reservation.call_args[0][0]
        assert body["subject"]["dimensions"]["ap2_transaction_id"] == "ap2-tx-real-e2e"
        assert body["subject"]["dimensions"]["checkout_hash"] == "ch_e2e"
        assert body["subject"]["dimensions"]["open_mandate_hash"] == "omh_e2e"
        # Lock scope shifted because open_mandate_hash was present.
        assert ":open_mandate:" in body["idempotency_key"]
