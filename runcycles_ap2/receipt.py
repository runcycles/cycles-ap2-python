"""Construct the client-side runtime-authority receipt."""

from __future__ import annotations

import time
from typing import Any

from runcycles_ap2._constants import RECEIPT_SCHEMA
from runcycles_ap2.models import AP2Mandate, RuntimeAuthorityReceipt


def build_runtime_authority_receipt(
    mandate: AP2Mandate,
    *,
    decision: str,
    reservation_id: str,
    tenant: str | None,
    agent: str | None,
    action_kind: str,
    policy_keys: dict[str, Any],
    amount_micros: int,
    committed: bool,
    psp_ref: str | None = None,
    extra: dict[str, Any] | None = None,
    issued_at_ms: int | None = None,
) -> RuntimeAuthorityReceipt:
    """Build a receipt object that callers can persist alongside AP2 dispute evidence."""
    return RuntimeAuthorityReceipt(
        schema=RECEIPT_SCHEMA,
        decision=decision,
        reservation_id=reservation_id,
        tenant=tenant,
        agent=agent,
        ap2_transaction_id=mandate.transaction_id,
        checkout_hash=mandate.checkout_hash,
        action_kind=action_kind,
        amount_unit="USD_MICROCENTS",
        amount_micros=amount_micros,
        policy_keys=policy_keys,
        issued_at_ms=issued_at_ms if issued_at_ms is not None else int(time.time() * 1000),
        committed=committed,
        psp_ref=psp_ref,
        extra=extra,
    )
