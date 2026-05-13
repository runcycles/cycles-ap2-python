"""AP2 adapter types and the client-side runtime-authority receipt.

These models intentionally do NOT depend on Google's AP2 SDK so the wrapper survives
upstream schema renames. Callers construct ``AP2Mandate`` either directly from their
own fields or via :meth:`AP2Mandate.from_ap2` if they hold AP2 SDK objects.
"""

from __future__ import annotations

from decimal import Decimal, DecimalException
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field

from runcycles_ap2._constants import MAX_USD_MICROS
from runcycles_ap2.exceptions import AP2CurrencyError, AP2MandateError

# USD_MICROCENTS is 10^-8 USD; anything finer than 8 decimal places in the source string
# would be silently rounded. We reject it explicitly so callers don't lose precision.
_MAX_DECIMAL_PLACES = 8

# Cycles `Amount.amount` is int64; max is 9_223_372_036_854_775_807 (19 digits ≈ $92.2B).
# We bound the source decimal's significant-plus-integer-position digit count BEFORE doing
# any `10**shift` math — otherwise a short string like "1E+1000000000000" (a finite Decimal
# with a 10^12 exponent) would allocate a trillion-digit integer and hang the process.
# The digit cap is a coarse pre-allocation guard; the precise int64 boundary is enforced
# AFTER conversion via `MAX_USD_MICROS`.
_MAX_INTEGER_DIGITS = 19

_CONFIG = ConfigDict(populate_by_name=True, extra="forbid", str_strip_whitespace=True)


class AP2Mandate(BaseModel):
    """Adapter view over an AP2 PaymentMandate (+ optional CheckoutMandate / open mandate).

    Only the fields needed to construct a Cycles reservation are surfaced. Anything else
    on the upstream mandate is irrelevant to runtime authority and is ignored on purpose.
    """

    model_config = _CONFIG

    transaction_id: Annotated[str, Field(min_length=1, max_length=256)]
    amount_value: Annotated[str, Field(min_length=1, max_length=64)]
    currency: Annotated[str, Field(min_length=3, max_length=3)]
    payee_website: Annotated[str, Field(min_length=1, max_length=253)]
    # ``checkout_hash`` and ``open_mandate_hash`` MUST be non-empty when present.
    # An empty string would silently fall back to the transaction-id lock scope (the
    # falsy check in :func:`consume_once_input`), which is data corruption disguised
    # as the default. Pydantic enforces min_length=1; callers should pass ``None`` to
    # mean "not present".
    checkout_hash: Annotated[str, Field(min_length=1, max_length=256)] | None = None
    open_mandate_hash: Annotated[str, Field(min_length=1, max_length=256)] | None = None

    def amount_micros(self) -> int:
        """Convert ``amount_value`` (decimal string in major units) to USD micro-cents.

        v0.1 enforces USD only. Rejects:
          - non-decimals, NaN, +/-Infinity, negative values;
          - inputs with more than 8 decimal places (sub-micro precision would be lost);
          - inputs whose total magnitude would exceed the protocol's int64 cap
            (this also blocks the ``1E+1000000000000`` exponent-notation DoS vector
            that would otherwise allocate a trillion-digit scaling factor).

        All ``decimal`` failure modes are wrapped as :class:`AP2MandateError`.
        """
        if self.currency.upper() != "USD":
            raise AP2CurrencyError(f"v0.1 supports USD only; got {self.currency!r}. Multi-currency lands in v0.2.")
        try:
            value = Decimal(self.amount_value)
        except (DecimalException, ValueError) as exc:
            raise AP2MandateError(f"amount_value {self.amount_value!r} is not a valid decimal") from exc
        if not value.is_finite():
            raise AP2MandateError(f"amount_value must be finite; got {self.amount_value!r}")
        if value < 0:
            raise AP2MandateError("amount_value must be non-negative")
        # `as_tuple().exponent` is the negative of the number of decimal places for
        # finite values (e.g. "1.234" → exponent -3). It can also be a string sentinel
        # ("n", "N", "F") for NaN/Infinity, but is_finite() above rules those out.
        sign, digits, exponent = value.as_tuple()
        if not isinstance(exponent, int):
            # Defensive: is_finite() should have caught this, but the Decimal API permits
            # special exponent sentinels on inputs we treat as out-of-domain.
            raise AP2MandateError(f"amount_value {self.amount_value!r} is not a finite decimal")
        if exponent < -_MAX_DECIMAL_PLACES:
            raise AP2MandateError(
                f"amount_value {self.amount_value!r} has more than {_MAX_DECIMAL_PLACES} decimal places; "
                "sub-micro precision would be lost"
            )

        # Bound the eventual integer size BEFORE computing 10**shift. ``len(digits)``
        # counts significant digits; ``max(0, exponent)`` counts the integer-position
        # padding zeros a positive exponent implies. Their sum is the digit count of
        # the unscaled integer value; capping it at 19 (int64.max digit count) blocks
        # exponent-notation DoS like ``Decimal("1E+1000000000000")`` and also rejects
        # legitimate-but-too-large amounts (the server would reject them anyway).
        total_integer_digits = len(digits) + max(0, exponent)
        if total_integer_digits > _MAX_INTEGER_DIGITS:
            raise AP2MandateError(
                f"amount_value {self.amount_value!r} exceeds int64 USD micro-cent range "
                f"({total_integer_digits} integer digits > {_MAX_INTEGER_DIGITS} max)"
            )

        # Exact integer conversion via `as_tuple()`. We deliberately do NOT compute
        # `value * 10**8` as Decimals: that uses the default 28-digit decimal context
        # and silently rounds inputs larger than the protocol cap (which a malformed
        # mandate could carry). Working off `digits` is unconditional — any input
        # that survived the validation above produces an exact int.
        if sign:
            # Negative was already rejected above; keep the branch for defensive clarity.
            raise AP2MandateError("amount_value must be non-negative")
        int_value = 0
        for d in digits:
            int_value = int_value * 10 + d
        # `value` equals int_value * 10**exponent, so micros = int_value * 10**(exponent+8).
        # `exponent + _MAX_DECIMAL_PLACES` is always >= 0 here because the validation
        # above rejected `exponent < -_MAX_DECIMAL_PLACES`. The bounded-digits check
        # above guarantees `shift <= _MAX_INTEGER_DIGITS + _MAX_DECIMAL_PLACES` so the
        # `10**shift` allocation is small.
        shift = exponent + _MAX_DECIMAL_PLACES
        scale: int = int(10**shift)
        micros = int_value * scale
        # The 19-digit cap above is necessary but not sufficient — it permits values
        # one over int64.max (e.g. "92233720368.54775808" → 9_223_372_036_854_775_808).
        # Enforce the exact protocol ceiling here so we fail-fast client-side rather
        # than waiting for the server to reject.
        if micros > MAX_USD_MICROS:
            raise AP2MandateError(
                f"amount_value {self.amount_value!r} exceeds int64 USD micro-cent max ({micros} > {MAX_USD_MICROS})"
            )
        return micros

    @classmethod
    def from_ap2(
        cls,
        payment_mandate: Any,
        checkout_mandate: Any | None = None,
        *,
        open_mandate_hash: str | None = None,
    ) -> AP2Mandate:
        """Build an ``AP2Mandate`` from upstream AP2 SDK objects (duck-typed).

        Required attributes:
          - ``payment_mandate.transaction_id``
          - ``payment_mandate.payment_amount.value`` and ``.currency``
          - ``payment_mandate.payee.website`` (or ``.identifier``)
        Optional:
          - ``checkout_mandate.hash`` (or ``.checkout_hash``)
        """
        try:
            transaction_id = str(payment_mandate.transaction_id)
            amount = payment_mandate.payment_amount
            amount_value = str(amount.value)
            currency = str(amount.currency)
            payee = payment_mandate.payee
            payee_website = getattr(payee, "website", None) or getattr(payee, "identifier", None)
            if not payee_website:
                raise AP2MandateError("AP2 payee must expose `website` or `identifier`")
        except AttributeError as exc:
            raise AP2MandateError(f"upstream PaymentMandate is missing a required field: {exc}") from exc

        checkout_hash: str | None = None
        if checkout_mandate is not None:
            # Two upstream naming variants point at the same field, so fall back only
            # when the first is genuinely missing (``None``) — NOT when it's an empty
            # string. Empty-string `hash` is bad data; let it propagate so the model's
            # ``min_length=1`` rejects it instead of silently masking the corruption
            # via the falsy-``or`` short-circuit.
            checkout_hash = getattr(checkout_mandate, "hash", None)
            if checkout_hash is None:
                checkout_hash = getattr(checkout_mandate, "checkout_hash", None)

        return cls(
            transaction_id=transaction_id,
            amount_value=amount_value,
            currency=currency,
            payee_website=str(payee_website),
            checkout_hash=checkout_hash,
            open_mandate_hash=open_mandate_hash,
        )


class RuntimeAuthorityReceipt(BaseModel):
    """Client-side runtime-authority receipt produced after a successful guarded payment.

    **Important:** This receipt is constructed by the wrapper from the Cycles ALLOW + COMMIT
    responses. It is NOT signed by the Cycles server in protocol v0.1.26 and therefore must
    not be relied on as cryptographic evidence by third parties. A server-verifiable variant
    is planned for v0.3.
    """

    model_config = ConfigDict(populate_by_name=True)

    schema_name: str = Field(alias="schema")
    decision: str
    reservation_id: str
    tenant: str | None = None
    agent: str | None = None
    ap2_transaction_id: str
    checkout_hash: str | None = None
    action_kind: str
    amount_unit: str
    amount_micros: int
    policy_keys: dict[str, Any]
    issued_at_ms: int
    committed: bool = False
    psp_ref: str | None = None
    extra: dict[str, Any] | None = None
