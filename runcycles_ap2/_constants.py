"""Internal constants for AP2 → Cycles mapping."""

from __future__ import annotations

from typing import Final

# Default Cycles action kind for an AP2 payment moment.
# `payment.charge` is a built-in high_risk kind in cycles-action-kinds-v0.1.26.yaml
# (no server-side registration required).
DEFAULT_ACTION_KIND: Final[str] = "payment.charge"
DEFAULT_ACTION_NAME: Final[str] = "ap2.payment_mandate.present"

# Subject.dimensions keys (lower-case, [a-z0-9_.-], <=256 chars per value, max 16 keys).
DIM_RUN_ID: Final[str] = "run_id"
DIM_AP2_TRANSACTION_ID: Final[str] = "ap2_transaction_id"
DIM_CHECKOUT_HASH: Final[str] = "checkout_hash"
DIM_OPEN_MANDATE_HASH: Final[str] = "open_mandate_hash"

# Action.policy_keys.custom keys.
CUSTOM_PAYMENT_PROTOCOL: Final[str] = "payment_protocol"
CUSTOM_CURRENCY: Final[str] = "currency"
CUSTOM_PAYMENT_PROTOCOL_VALUE: Final[str] = "ap2"

# Lifecycle defaults — payments must NOT overspend, so DENY (not ALLOW_IF_AVAILABLE).
DEFAULT_TTL_MS: Final[int] = 60_000
DEFAULT_OVERAGE_POLICY: Final[str] = "REJECT"

# Receipt schema identifier (client-side; not server-verifiable in protocol v0.1.26).
RECEIPT_SCHEMA: Final[str] = "runtime_authority.ap2.payment.charge.v1"

# Idempotency key prefix used for all phases.
IDEMPOTENCY_PREFIX: Final[str] = "ap2"

# Length of the hex-encoded SHA-256 prefix used in idempotency keys.
# 32 hex chars == 128 bits of collision resistance. Keys stay well inside the 256-char
# protocol cap, so the phase suffix (`reserve`/`commit`/`release:{ExcType}`) is always
# preserved — which is what protects the consume-once defense.
TRANSACTION_ID_HASH_LEN: Final[int] = 32
