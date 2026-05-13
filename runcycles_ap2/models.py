"""AP2 adapter types and the client-side runtime-authority receipt.

These models intentionally do NOT depend on Google's AP2 SDK so the wrapper survives
upstream schema renames. Callers construct ``AP2Mandate`` either directly from their
own fields or via :meth:`AP2Mandate.from_ap2` if they hold AP2 SDK objects.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field

from runcycles_ap2._constants import USD_MICROCENTS_PER_DOLLAR
from runcycles_ap2.exceptions import AP2CurrencyError, AP2MandateError

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
    checkout_hash: Annotated[str, Field(max_length=256)] | None = None
    open_mandate_hash: Annotated[str, Field(max_length=256)] | None = None

    def amount_micros(self) -> int:
        """Convert ``amount_value`` (decimal string in major units) to USD micro-cents.

        v0.1 enforces USD only.
        """
        if self.currency.upper() != "USD":
            raise AP2CurrencyError(f"v0.1 supports USD only; got {self.currency!r}. Multi-currency lands in v0.2.")
        try:
            value = Decimal(self.amount_value)
        except (ArithmeticError, ValueError) as exc:
            raise AP2MandateError(f"amount_value {self.amount_value!r} is not a valid decimal") from exc
        if value < 0:
            raise AP2MandateError("amount_value must be non-negative")
        micros = int((value * USD_MICROCENTS_PER_DOLLAR).to_integral_value())
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
            checkout_hash = getattr(checkout_mandate, "hash", None) or getattr(checkout_mandate, "checkout_hash", None)

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
