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
    IDEMPOTENCY_SCOPE_OPEN_MANDATE,
    IDEMPOTENCY_SCOPE_TRANSACTION,
    TRANSACTION_ID_HASH_LEN,
)
from runcycles_ap2._validation import validate_micros
from runcycles_ap2.models import AP2Mandate


def _hash_input(value: str) -> str:
    """SHA-256 of the input string, hex-encoded and truncated to 32 chars.

    Hashing guarantees:
      - fixed-length output regardless of input (avoids the 256-char header truncation
        that silently dropped the phase suffix in earlier versions);
      - header-safe characters only (lowercase hex) — no whitespace, no control bytes;
      - the same input always produces the same key on any platform.
    Truncating to 32 hex chars preserves 128 bits of collision resistance — more than
    enough for an idempotency-key namespace scoped to a single tenant.
    """
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return digest[:TRANSACTION_ID_HASH_LEN]


def consume_once_input(mandate: AP2Mandate) -> tuple[str, str]:
    """Pick the lock scope + raw input for this mandate.

    Returns ``(scope, raw_value)`` where:
      - ``scope == "open_mandate"`` and ``raw_value == mandate.open_mandate_hash`` when
        the mandate carries an open mandate hash. This makes every checkout derived from
        the same open mandate collide on the same idempotency key — the server's
        ``(tenant, endpoint, idempotency_key)`` dedup then collapses them onto one
        reservation. **This is the AP2 spec's normative defense** against an autonomous
        agent presenting subsequent open mandates without a rejection receipt
        (specification §6).
      - ``scope == "tx"`` and ``raw_value == mandate.transaction_id`` otherwise. One
        transaction == one payment attempt (the human-present / no-open-mandate case).

    The scope is embedded in the idempotency key so the two namespaces cannot collide
    server-side.
    """
    if mandate.open_mandate_hash:
        return (IDEMPOTENCY_SCOPE_OPEN_MANDATE, mandate.open_mandate_hash)
    return (IDEMPOTENCY_SCOPE_TRANSACTION, mandate.transaction_id)


def idempotency_key(mandate: AP2Mandate, phase: str, suffix: str | None = None) -> str:
    """Deterministic idempotency key: ``ap2:{scope}:{sha256(input)[:32]}:{phase}[:{suffix}]``.

    The scope (``open_mandate`` or ``tx``) is picked automatically from the mandate —
    see :func:`consume_once_input` for the rules. The phase suffix is ALWAYS preserved
    (the hash is fixed-length and short scope/phase tokens fit comfortably inside the
    256-char protocol cap).

    The raw ``transaction_id`` and ``open_mandate_hash`` are still attached to
    ``Subject.dimensions`` on the Cycles wire payload for debug/audit; only the
    idempotency key uses the hash.
    """
    scope, raw = consume_once_input(mandate)
    base = f"{IDEMPOTENCY_PREFIX}:{scope}:{_hash_input(raw)}:{phase}"
    if suffix:
        # Header-safe charset: ASCII alphanumeric, underscore, hyphen, dot. We use
        # ``isascii() and isalnum()`` rather than ``isalnum()`` alone because Python's
        # ``str.isalnum`` is Unicode-aware ("É".isalnum() is True), and HTTP header
        # tokens per RFC 7230 must be ASCII. Non-ASCII chars (e.g. a localized
        # exception class name like "Échec") would otherwise reach the wire and
        # httpx would reject the request. Anything outside the safe set becomes "_".
        safe_suffix = "".join(c if (c.isascii() and c.isalnum()) or c in ("_", "-", ".") else "_" for c in suffix)
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
        "idempotency_key": idempotency_key(mandate, "reserve"),
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
    """Commit body with the deterministic idempotency key for this mandate.

    The key is derived from the consume-once scope: ``open_mandate_hash`` when the
    mandate carries one (HNP flows), otherwise ``transaction_id`` — see
    :func:`idempotency_key` and :func:`consume_once_input`.

    ``actual_micros`` is validated when supplied (rejects ``bool``, ``float``, and
    out-of-range ints) so direct callers of this builder get the same protection as
    :meth:`GuardedPayment.set_actual_micros`.
    """
    if actual_micros is None:
        amount = mandate.amount_micros()
    else:
        amount = validate_micros(actual_micros, field="actual_micros")
    body: dict[str, Any] = {
        "idempotency_key": idempotency_key(mandate, "commit"),
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
        "idempotency_key": idempotency_key(mandate, "release", exception_type),
        "reason": reason[:256],
    }
