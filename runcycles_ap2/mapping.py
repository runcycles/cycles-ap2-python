"""Pure functions that map ``AP2Mandate`` into Cycles wire payloads.

Kept side-effect-free and network-free so the unit tests in ``tests/test_mapping.py``
are fully deterministic and run without a server.
"""

from __future__ import annotations

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
)
from runcycles_ap2.models import AP2Mandate


def idempotency_key(transaction_id: str, phase: str, suffix: str | None = None) -> str:
    """Deterministic idempotency key: ``ap2:{tx}:{phase}[:{suffix}]``.

    The same ``transaction_id`` reaching the same phase from any retry, parallel worker,
    or process restart will produce the same key — that's the server-side replay defense.
    """
    base = f"{IDEMPOTENCY_PREFIX}:{transaction_id}:{phase}"
    if suffix:
        # Strip characters that would push us over the 256-char limit or break the key shape.
        safe_suffix = "".join(c if c.isalnum() or c in ("_", "-", ".") else "_" for c in suffix)
        base = f"{base}:{safe_suffix[:64]}"
    return base[:256]


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
    """Commit body with deterministic idempotency key derived from ``transaction_id``."""
    amount = actual_micros if actual_micros is not None else mandate.amount_micros()
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
