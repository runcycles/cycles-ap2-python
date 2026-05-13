"""Pure functions that map ``AP2Mandate`` into Cycles wire payloads.

Kept side-effect-free and network-free so the unit tests in ``tests/test_mapping.py``
are fully deterministic and run without a server.
"""

from __future__ import annotations

import hashlib
from typing import Any

from runcycles_ap2._constants import (
    CUSTOM_CURRENCY,
    CUSTOM_PAYMENT_PROTOCOL,
    CUSTOM_PAYMENT_PROTOCOL_VALUE,
    DEFAULT_ACTION_NAME,
    DIM_AP2_TRANSACTION_ID,
    DIM_CHECKOUT_HASH,
    DIM_OPEN_MANDATE_HASH,
    DIM_RUN_ID,
    IDEMPOTENCY_PREFIX,
    TRANSACTION_ID_HASH_LEN,
)
from runcycles_ap2._validation import validate_micros
from runcycles_ap2.models import AP2Mandate


def _hash_transaction_id(transaction_id: str) -> str:
    """SHA-256 of the raw ``transaction_id``, hex-encoded and truncated.

    Hashing guarantees:
      - fixed-length output regardless of input (avoids the 256-char header truncation
        that silently dropped the phase suffix in earlier versions);
      - header-safe characters only (lowercase hex) — no whitespace, no control bytes;
      - the same input always produces the same key on any platform.
    Truncating to 32 hex chars preserves 128 bits of collision resistance — more than
    enough for an idempotency-key namespace scoped to a single tenant.
    """
    digest = hashlib.sha256(transaction_id.encode("utf-8")).hexdigest()
    return digest[:TRANSACTION_ID_HASH_LEN]


def idempotency_key(transaction_id: str, phase: str, suffix: str | None = None) -> str:
    """Deterministic idempotency key: ``ap2:{sha256(tx)[:32]}:{phase}[:{suffix}]``.

    The phase suffix is ALWAYS preserved (fixed-length hash + short phase fit comfortably
    inside the 256-char protocol cap). The same ``transaction_id`` reaching the same phase
    from any retry, parallel worker, or process restart produces the same key — the
    consume-once defense.

    The raw ``transaction_id`` is still attached to ``Subject.dimensions["ap2_transaction_id"]``
    on the Cycles wire payload for debug/audit; only the idempotency key uses the hash.
    """
    base = f"{IDEMPOTENCY_PREFIX}:{_hash_transaction_id(transaction_id)}:{phase}"
    if suffix:
        # Header-safe charset: alphanumeric, underscore, hyphen, dot. Anything else
        # becomes ``_`` so the resulting key is always a valid HTTP header value.
        safe_suffix = "".join(c if c.isalnum() or c in ("_", "-", ".") else "_" for c in suffix)
        base = f"{base}:{safe_suffix[:64]}"
    return base


def build_subject(
    mandate: AP2Mandate,
    *,
    run_id: str,
    tenant: str | None,
    workspace: str | None,
    app: str | None,
    workflow: str | None,
    agent: str | None,
    toolset: str | None,
    extra_dimensions: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Construct the Subject portion of a reservation create body."""
    subject: dict[str, Any] = {}
    if tenant:
        subject["tenant"] = tenant
    if workspace:
        subject["workspace"] = workspace
    if app:
        subject["app"] = app
    if workflow:
        subject["workflow"] = workflow
    if agent:
        subject["agent"] = agent
    if toolset:
        subject["toolset"] = toolset

    dimensions: dict[str, str] = {
        DIM_RUN_ID: run_id,
        DIM_AP2_TRANSACTION_ID: mandate.transaction_id,
    }
    if mandate.checkout_hash:
        dimensions[DIM_CHECKOUT_HASH] = mandate.checkout_hash
    if mandate.open_mandate_hash:
        dimensions[DIM_OPEN_MANDATE_HASH] = mandate.open_mandate_hash
    if extra_dimensions:
        for key, value in extra_dimensions.items():
            if key in dimensions:
                continue  # AP2-canonical keys win over caller-supplied
            dimensions[key] = value

    subject["dimensions"] = dimensions
    return subject


def build_action(mandate: AP2Mandate, *, action_kind: str) -> dict[str, Any]:
    """Construct the Action portion (including ``policy_keys``) of a reservation create body."""
    return {
        "kind": action_kind,
        "name": DEFAULT_ACTION_NAME,
        "policy_keys": {
            "host": mandate.payee_website,
            "custom": {
                CUSTOM_PAYMENT_PROTOCOL: CUSTOM_PAYMENT_PROTOCOL_VALUE,
                CUSTOM_CURRENCY: mandate.currency.upper(),
            },
        },
    }


def build_estimate(mandate: AP2Mandate) -> dict[str, Any]:
    """Construct the Amount portion (USD micro-cents)."""
    return {"unit": "USD_MICROCENTS", "amount": mandate.amount_micros()}


def build_reservation_body(
    mandate: AP2Mandate,
    *,
    run_id: str,
    tenant: str | None,
    workspace: str | None,
    app: str | None,
    workflow: str | None,
    agent: str | None,
    toolset: str | None,
    action_kind: str,
    ttl_ms: int,
    overage_policy: str,
    dry_run: bool,
    metadata: dict[str, Any] | None = None,
    extra_dimensions: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Assemble the full reservation create request body, including deterministic idempotency key."""
    body: dict[str, Any] = {
        "idempotency_key": idempotency_key(mandate.transaction_id, "reserve"),
        "subject": build_subject(
            mandate,
            run_id=run_id,
            tenant=tenant,
            workspace=workspace,
            app=app,
            workflow=workflow,
            agent=agent,
            toolset=toolset,
            extra_dimensions=extra_dimensions,
        ),
        "action": build_action(mandate, action_kind=action_kind),
        "estimate": build_estimate(mandate),
        "ttl_ms": ttl_ms,
        "overage_policy": overage_policy,
    }
    if dry_run:
        body["dry_run"] = True
    if metadata:
        body["metadata"] = metadata
    return body


def build_commit_body(
    mandate: AP2Mandate,
    *,
    actual_micros: int | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Commit body with deterministic idempotency key derived from ``transaction_id``.

    ``actual_micros`` is validated when supplied (rejects ``bool``, ``float``, and
    out-of-range ints) so direct callers of this builder get the same protection as
    :meth:`GuardedPayment.set_actual_micros`.
    """
    if actual_micros is None:
        amount = mandate.amount_micros()
    else:
        amount = validate_micros(actual_micros, field="actual_micros")
    body: dict[str, Any] = {
        "idempotency_key": idempotency_key(mandate.transaction_id, "commit"),
        "actual": {"unit": "USD_MICROCENTS", "amount": amount},
    }
    if metadata:
        body["metadata"] = metadata
    return body


def build_release_body(
    mandate: AP2Mandate,
    *,
    reason: str,
    exception_type: str | None = None,
) -> dict[str, Any]:
    """Release body with deterministic idempotency key per exception type."""
    return {
        "idempotency_key": idempotency_key(mandate.transaction_id, "release", exception_type),
        "reason": reason[:256],
    }
