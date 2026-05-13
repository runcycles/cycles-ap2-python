"""Pure unit tests for mapping.py — no network, no client."""

from __future__ import annotations

import hashlib

import pytest

from runcycles_ap2._constants import (
    CUSTOM_CURRENCY,
    CUSTOM_PAYMENT_PROTOCOL,
    DIM_AP2_TRANSACTION_ID,
    DIM_CHECKOUT_HASH,
    DIM_OPEN_MANDATE_HASH,
    DIM_RUN_ID,
    TRANSACTION_ID_HASH_LEN,
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


def _expected_hash(tx: str) -> str:
    return hashlib.sha256(tx.encode("utf-8")).hexdigest()[:TRANSACTION_ID_HASH_LEN]


class TestIdempotencyKey:
    """Default scope: transaction-id (when no open_mandate_hash)."""

    def test_reserve_phase_tx_scope(self) -> None:
        h = _expected_hash("ap2-tx-001")
        assert idempotency_key(make_mandate(), "reserve") == f"ap2:tx:{h}:reserve"

    def test_commit_phase_tx_scope(self) -> None:
        h = _expected_hash("ap2-tx-001")
        assert idempotency_key(make_mandate(), "commit") == f"ap2:tx:{h}:commit"

    def test_release_with_suffix_tx_scope(self) -> None:
        h = _expected_hash("ap2-tx-001")
        assert idempotency_key(make_mandate(), "release", "RuntimeError") == f"ap2:tx:{h}:release:RuntimeError"

    def test_release_sanitizes_unsafe_chars(self) -> None:
        # Whitespace, slashes, and other header-unsafe chars get replaced with `_`.
        m = make_mandate(transaction_id="tx")
        h = _expected_hash("tx")
        key = idempotency_key(m, "release", "some/bad value")
        assert key == f"ap2:tx:{h}:release:some_bad_value"

    def test_long_transaction_id_does_not_drop_phase(self) -> None:
        # P1 regression: a 256-char tx_id used to overflow the cap and have the phase
        # suffix sliced off. The fixed-length hash keeps phases distinct.
        m = make_mandate(transaction_id="x" * 256)
        reserve = idempotency_key(m, "reserve")
        commit = idempotency_key(m, "commit")
        release = idempotency_key(m, "release", "RuntimeError")
        assert reserve.endswith(":reserve")
        assert commit.endswith(":commit")
        assert release.endswith(":release:RuntimeError")
        assert reserve != commit != release

    def test_distinct_long_tx_ids_yield_distinct_keys(self) -> None:
        # P1 regression: two tx_ids sharing the first 252 chars used to collide on reserve.
        a = make_mandate(transaction_id=("y" * 252) + "AAAA")
        b = make_mandate(transaction_id=("y" * 252) + "BBBB")
        assert idempotency_key(a, "reserve") != idempotency_key(b, "reserve")

    def test_unsafe_transaction_id_chars_do_not_leak_to_header(self) -> None:
        # P1 regression: tx_ids with whitespace / control bytes used to reach the header.
        m = make_mandate(transaction_id="tx with\nnewline\tand\r\x00null")
        key = idempotency_key(m, "reserve")
        assert "\n" not in key and "\t" not in key and "\r" not in key and "\x00" not in key
        assert all(c.isalnum() or c in (":", "_", "-", ".") for c in key)

    def test_non_ascii_suffix_is_sanitized_to_ascii(self) -> None:
        # P2 regression: Python's str.isalnum() is Unicode-aware, so "É".isalnum() is
        # True. An earlier filter that used `c.isalnum() or c in (...)` alone would
        # let non-ASCII chars reach the Idempotency-Key header — RFC 7230 requires
        # ASCII tokens, so httpx would reject the request. The tightened predicate
        # `c.isascii() and c.isalnum()` keeps the key strictly ASCII.
        m = make_mandate()
        key = idempotency_key(m, "release", "Échec")  # French "Failure"
        assert key.isascii()
        # The non-ASCII "É" must have been replaced (with "_"); "chec" survives.
        assert "É" not in key
        assert key.endswith(":release:_chec")

    def test_key_length_bounded(self) -> None:
        # Hash + phase + suffix always fits inside the protocol's 256-char cap, even
        # at the longest possible transaction_id (model max 256) and a max-length
        # exception-type suffix.
        m = make_mandate(transaction_id="x" * 256)
        assert len(idempotency_key(m, "release", "x" * 256)) <= 256


class TestIdempotencyKeyOpenMandateScope:
    """P0-A: when ``open_mandate_hash`` is present, the lock scope shifts to it.

    This is the AP2-spec consume-once boundary for human-not-present flows: every
    checkout derived from the same open mandate must collapse onto one reservation,
    even when their transaction_ids differ.
    """

    def test_open_mandate_scope_used_when_hash_present(self) -> None:
        m = make_mandate(open_mandate_hash="omh_abc")
        h = _expected_hash("omh_abc")
        # Scope namespace is `open_mandate`, hash input is the open_mandate_hash —
        # the transaction_id is NOT mixed in here (it'd defeat the whole point).
        assert idempotency_key(m, "reserve") == f"ap2:open_mandate:{h}:reserve"
        assert idempotency_key(m, "commit") == f"ap2:open_mandate:{h}:commit"

    def test_different_transactions_same_open_mandate_share_key(self) -> None:
        # Two checkouts spawned from one open mandate must collide on the reserve key
        # so the server sees both attempts in one (tenant, endpoint, key) bucket. The
        # server then either replays the original response (if payload matches) or
        # returns 409 IDEMPOTENCY_MISMATCH — either way, no second valid reservation.
        a = make_mandate(transaction_id="ap2-tx-AAA", open_mandate_hash="omh_shared")
        b = make_mandate(transaction_id="ap2-tx-BBB", open_mandate_hash="omh_shared")
        assert idempotency_key(a, "reserve") == idempotency_key(b, "reserve")
        assert idempotency_key(a, "commit") == idempotency_key(b, "commit")

    def test_tx_and_open_mandate_namespaces_never_collide(self) -> None:
        # Even if some pathological caller arranged for transaction_id and
        # open_mandate_hash to hash to the same value, the scope namespace ("tx" vs
        # "open_mandate") embedded in the key keeps them in distinct dedup buckets.
        tx_only = make_mandate(transaction_id="same-input")
        omh_only = make_mandate(transaction_id="other-tx", open_mandate_hash="same-input")
        assert idempotency_key(tx_only, "reserve") != idempotency_key(omh_only, "reserve")

    def test_open_mandate_scope_takes_precedence_over_transaction_id(self) -> None:
        # When both are present, open_mandate wins — that's the consume-once boundary.
        m = make_mandate(transaction_id="ap2-tx-001", open_mandate_hash="omh_xyz")
        assert "open_mandate" in idempotency_key(m, "reserve")
        assert _expected_hash("ap2-tx-001") not in idempotency_key(m, "reserve")

    def test_empty_open_mandate_hash_rejected_at_model_level(self) -> None:
        # An empty string would otherwise silently fall back to the tx scope (the
        # falsy check in consume_once_input). Model-level min_length=1 catches it.
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            AP2Mandate(
                transaction_id="tx",
                amount_value="1.00",
                currency="USD",
                payee_website="x.example",
                open_mandate_hash="",
            )

    def test_empty_checkout_hash_rejected_at_model_level(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            AP2Mandate(
                transaction_id="tx",
                amount_value="1.00",
                currency="USD",
                payee_website="x.example",
                checkout_hash="",
            )


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

    def test_nan_raises(self) -> None:
        m = AP2Mandate(transaction_id="tx", amount_value="NaN", currency="USD", payee_website="x.example")
        with pytest.raises(AP2MandateError):
            m.amount_micros()

    def test_infinity_raises(self) -> None:
        m = AP2Mandate(transaction_id="tx", amount_value="Infinity", currency="USD", payee_website="x.example")
        with pytest.raises(AP2MandateError):
            m.amount_micros()

    def test_negative_infinity_raises(self) -> None:
        m = AP2Mandate(transaction_id="tx", amount_value="-Infinity", currency="USD", payee_website="x.example")
        with pytest.raises(AP2MandateError):
            m.amount_micros()

    def test_sub_micro_precision_raises(self) -> None:
        # 9 decimal places — below USD_MICROCENTS resolution.
        m = AP2Mandate(transaction_id="tx", amount_value="1.123456789", currency="USD", payee_website="x.example")
        with pytest.raises(AP2MandateError):
            m.amount_micros()

    def test_exactly_eight_decimals_accepted(self) -> None:
        m = AP2Mandate(transaction_id="tx", amount_value="1.12345678", currency="USD", payee_website="x.example")
        # 1.12345678 * 1e8 = 112345678 micro-cents — exact, no precision loss.
        assert m.amount_micros() == 112_345_678

    def test_large_value_converts_exactly_within_int64_cap(self) -> None:
        # P2 regression: even at the int64 boundary, the conversion stays exact (no
        # silent rounding via the default decimal context that the earlier `value *
        # 10**8` path would have introduced for >28-digit results).
        m = AP2Mandate(
            transaction_id="tx",
            amount_value="92233720368.54775807",
            currency="USD",
            payee_website="x.example",
        )
        # int64.max == 9_223_372_036_854_775_807. Exact, no rounding.
        assert m.amount_micros() == 9_223_372_036_854_775_807

    def test_integer_value_converts_exactly(self) -> None:
        m = AP2Mandate(transaction_id="tx", amount_value="100", currency="USD", payee_website="x.example")
        assert m.amount_micros() == 10_000_000_000

    def test_huge_exponent_notation_rejected_short_input(self) -> None:
        # P2 DoS regression: `Decimal("1E+1000000000000")` is short (16 chars), finite,
        # and positive — it used to pass every validation and then try to allocate a
        # trillion-digit int via 10**shift. The digit-count cap catches it cleanly.
        m = AP2Mandate(
            transaction_id="tx",
            amount_value="1E+1000000000000",
            currency="USD",
            payee_website="x.example",
        )
        with pytest.raises(AP2MandateError, match="int64"):
            m.amount_micros()

    def test_zero_with_huge_exponent_rejected(self) -> None:
        # P2 DoS regression: even a zero value with a huge exponent would have
        # triggered the allocation before the multiplication zeroed the result.
        m = AP2Mandate(
            transaction_id="tx",
            amount_value="0E+1000000000000",
            currency="USD",
            payee_website="x.example",
        )
        with pytest.raises(AP2MandateError, match="int64"):
            m.amount_micros()

    def test_too_many_digits_rejected(self) -> None:
        # A legitimate-shaped but out-of-range amount: 20 significant digits. The
        # 19-digit cap (int64.max digit count) catches it before any allocation.
        m = AP2Mandate(
            transaction_id="tx",
            amount_value="123456789012.12345678",
            currency="USD",
            payee_website="x.example",
        )
        with pytest.raises(AP2MandateError, match="int64"):
            m.amount_micros()

    def test_int64_max_exact_accepted(self) -> None:
        # Exact int64.max in USD_MICROCENTS — must still be accepted.
        m = AP2Mandate(
            transaction_id="tx",
            amount_value="92233720368.54775807",
            currency="USD",
            payee_website="x.example",
        )
        assert m.amount_micros() == 9_223_372_036_854_775_807

    def test_int64_max_plus_one_rejected(self) -> None:
        # P2 regression: the 19-digit cap permits this (also 19 digits), so the
        # post-conversion int64 check is what catches it. Previous versions would
        # have shipped 9_223_372_036_854_775_808 to the server.
        m = AP2Mandate(
            transaction_id="tx",
            amount_value="92233720368.54775808",
            currency="USD",
            payee_website="x.example",
        )
        with pytest.raises(AP2MandateError, match="int64"):
            m.amount_micros()

    def test_19_digit_amount_above_int64_rejected(self) -> None:
        # P2 regression: 11 integer 9s + 8 fractional 9s == 19 total digits — within
        # the digit cap, but the resulting micros (9_999_999_999_999_999_999 ≈ 1e19)
        # exceed int64.max (≈ 9.22e18). The post-conversion check rejects it.
        m = AP2Mandate(
            transaction_id="tx",
            amount_value="99999999999.99999999",
            currency="USD",
            payee_website="x.example",
        )
        with pytest.raises(AP2MandateError, match="int64"):
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
        assert body["idempotency_key"] == f"ap2:tx:{_expected_hash('ap2-tx-001')}:reserve"
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
        assert body["idempotency_key"] == f"ap2:tx:{_expected_hash('ap2-tx-001')}:commit"

    def test_actual_override(self) -> None:
        body = build_commit_body(make_mandate(), actual_micros=5_000_000_000)
        assert body["actual"]["amount"] == 5_000_000_000

    def test_metadata_attached(self) -> None:
        body = build_commit_body(make_mandate(), metadata={"psp_ref": "psp_1"})
        assert body["metadata"]["psp_ref"] == "psp_1"

    @pytest.mark.parametrize("bad", [True, False, 1.5, 1.0, "100", [100]])
    def test_actual_micros_rejects_non_int_types(self, bad) -> None:
        # P2 regression: build_commit_body is called directly by some callers (and by
        # the guard); it must reject bool/float/str up front rather than letting the
        # bad value reach the wire and only fail at receipt-construction time.
        with pytest.raises(AP2MandateError, match="must be an int"):
            build_commit_body(make_mandate(), actual_micros=bad)

    def test_actual_micros_negative_rejected(self) -> None:
        with pytest.raises(AP2MandateError, match="non-negative"):
            build_commit_body(make_mandate(), actual_micros=-1)

    def test_actual_micros_above_int64_rejected(self) -> None:
        with pytest.raises(AP2MandateError, match="int64"):
            build_commit_body(make_mandate(), actual_micros=2**63)


class TestBuildReleaseBody:
    def test_with_exception_type(self) -> None:
        body = build_release_body(make_mandate(), reason="fail", exception_type="RuntimeError")
        assert body["idempotency_key"] == f"ap2:tx:{_expected_hash('ap2-tx-001')}:release:RuntimeError"
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
