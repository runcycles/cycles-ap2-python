[![PyPI](https://img.shields.io/pypi/v/runcycles-ap2)](https://pypi.org/project/runcycles-ap2/)
[![PyPI Downloads](https://img.shields.io/pypi/dm/runcycles-ap2)](https://pypi.org/project/runcycles-ap2/)
[![CI](https://github.com/runcycles/cycles-ap2-python/actions/workflows/ci.yml/badge.svg)](https://github.com/runcycles/cycles-ap2-python/actions)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue)](LICENSE)
[![Coverage](https://img.shields.io/badge/coverage-97%25-brightgreen)](https://github.com/runcycles/cycles-ap2-python/actions)
[![OpenSSF Scorecard](https://api.scorecard.dev/projects/github.com/runcycles/cycles-ap2-python/badge)](https://scorecard.dev/viewer/?uri=github.com/runcycles/cycles-ap2-python)

# Cycles AP2 Guard — Runtime authority for AP2 agent payments

**Cycles runtime authority guard for [AP2](https://github.com/google-agentic-commerce/AP2) (Agent Payments Protocol) — wrap every AP2 payment moment in a `reserve / commit / release` lifecycle so a valid mandate cannot be over-exercised under retries, fan-out, or concurrent checkout attempts.** Works with Google's AP2 spec and any AP2-compatible SDK (Python samples today; A2A / MCP / UCP roadmap).

AP2 answers *"was this payment authorized?"* with signed Open/Closed mandates, Payment Mandates, and Checkout Mandates. Cycles answers the complementary question: *"should this agent be allowed to attempt this payment **right now**?"* — pre-execution authority over reservation, idempotency, quota, and consume-once semantics. Install via `pip install runcycles-ap2`.

## The problem AP2 itself flags

From the AP2 spec, human-not-present flows let the agent act autonomously using an open mandate and sign a closed mandate on the user's behalf. AP2 warns:

> "A shopping agent must avoid presenting subsequent open mandates without a rejection receipt to prevent multiple checkouts using the same open mandate."

That is a **runtime-state** problem: concurrency, retries, in-flight attempts, quota counters, consume-once. AP2 mandates are cryptographic *authorization*. Cycles adds the missing runtime enforcement.

## Installation

```bash
pip install runcycles-ap2
```

Needs a running Cycles server (see [`cycles-client-python`](https://github.com/runcycles/cycles-client-python) for setup) and a signed AP2 PaymentMandate.

## Quickstart

```python
from runcycles import CyclesClient, CyclesConfig
from runcycles_ap2 import AP2Mandate, cycles_guard_payment

config = CyclesConfig.from_env()  # CYCLES_BASE_URL, CYCLES_API_KEY, CYCLES_TENANT

with CyclesClient(config) as client:
    mandate = AP2Mandate(
        transaction_id="ap2-tx-9f3c",
        amount_value="199.00",
        currency="USD",
        payee_website="merchant.example",
        checkout_hash="ch_b1a9...",
    )
    with cycles_guard_payment(
        client,
        mandate=mandate,
        run_id="run_abc123",
        tenant="acme",
        agent="checkout-bot",
    ) as guard:
        # Real PSP call goes here — protected by reserve / commit / release.
        psp_receipt = psp.charge(mandate)
        guard.attach_receipt_fields(psp_ref=psp_receipt.id)

    print(guard.receipt)  # client-side runtime-authority receipt
```

## From an existing AP2 SDK object

If you already hold a `PaymentMandate` (and optional `CheckoutMandate`) from the AP2 Python SDK, build an `AP2Mandate` adapter in one line. Field renames in AP2 only touch this adapter — your guard code stays stable.

```python
from runcycles_ap2 import AP2Mandate

mandate = AP2Mandate.from_ap2(payment_mandate, checkout_mandate)
```

Required upstream attributes: `payment_mandate.transaction_id`, `payment_mandate.payment_amount.value`, `payment_mandate.payment_amount.currency`, `payment_mandate.payee.website` (or `.identifier`). Optional: `checkout_mandate.hash`.

## How the guard responds

| Scenario | Outcome | Detail |
|---|---|---|
| `Decision.ALLOW`, body completes | **Commit** | Server idempotency key derived from `transaction_id` — see *Deterministic idempotency keys* below |
| `Decision.ALLOW`, body raises | **Release** | Reason `ap2_guard_failed:{ExcType}`, idempotency key includes the exception type |
| `Decision.DENY` | **Neither** | `AP2GuardDenied` raised in `__enter__`; real money never moves |
| HTTP / transport error on reserve | **Neither** | `AP2GuardDenied` raised; caller can retry — same `transaction_id` ⇒ same reserve key |
| Commit returns `RESERVATION_FINALIZED` / `RESERVATION_EXPIRED` / `IDEMPOTENCY_MISMATCH` | **Neither** | Logged at warning; we never auto-release after these (a previous commit may already have charged) |
| Commit returns other 4xx | **Release + raise** | Reservation release attempted; `AP2GuardCommitFailed` raised with `released` + `release_error` so the caller cannot miss the reconciliation event |
| `guard.abort(reason)` called inside `with` | **Release** | Reason `ap2_guard_aborted:{reason}` |
| `dry_run=True` | **Neither** | `__enter__` raises `AP2DryRunResult` carrying the decision payload — the `with` body never runs, so a real PSP call cannot leak under a dry-run probe |

`AP2GuardDenied` carries `reason_code` and `request_id` for upstream logging.

## AP2 → Cycles wire mapping

| AP2 source | Cycles destination | Notes |
|---|---|---|
| `PaymentMandate.transaction_id` | `Subject.dimensions["ap2_transaction_id"]` | also feeds idempotency keys |
| `PaymentMandate.payment_amount.value` | `Amount.amount` | `int(round(value * 1e8))` USD micro-cents |
| `PaymentMandate.payment_amount.currency` | `Action.policy_keys.custom["currency"]` | MVP enforces `"USD"` |
| `PaymentMandate.payee.website` | `Action.policy_keys.host` | required for policy routing |
| `CheckoutMandate.hash` | `Subject.dimensions["checkout_hash"]` | optional |
| `sha256(open_mandate_canonical)` | `Subject.dimensions["open_mandate_hash"]` | optional, human-not-present |
| caller `run_id` | `Subject.dimensions["run_id"]` | required |
| const `"ap2"` | `Action.policy_keys.custom["payment_protocol"]` | marker |
| const `"payment.charge"` | `Action.kind` | built-in `high_risk` kind in `cycles-action-kinds-v0.1.26.yaml` |
| const `USD_MICROCENTS` | `Amount.unit` | single-unit per reservation |

No protocol changes required for v0.1 — `payment.charge` and `payment.refund` already exist as `high_risk` action kinds in the Cycles protocol registry.

## Deterministic idempotency keys

The wrapper computes idempotency keys from `transaction_id`; callers MUST NOT pass their own. This is the consume-once defense — retried `__enter__`s on the same mandate, from any process, return the original reservation:

| Phase | Key shape |
|---|---|
| Reserve | `ap2:{sha256(transaction_id)[:32]}:reserve` |
| Commit | `ap2:{sha256(transaction_id)[:32]}:commit` |
| Release | `ap2:{sha256(transaction_id)[:32]}:release:{ExcType}` |

The transaction_id is hashed (SHA-256, first 32 hex chars — 128 bits of collision resistance) so the key is fixed-length, header-safe, and the phase suffix is always preserved regardless of how long the upstream id is. The raw `transaction_id` is still attached to `Subject.dimensions["ap2_transaction_id"]` for debug/audit.

## Runtime authority receipt

After a successful commit, the guard exposes a client-side receipt that can be persisted alongside AP2 dispute evidence:

```json
{
  "schema": "runtime_authority.ap2.payment.charge.v1",
  "decision": "ALLOW",
  "reservation_id": "rsv_...",
  "tenant": "acme",
  "ap2_transaction_id": "ap2-tx-9f3c",
  "checkout_hash": "ch_b1a9...",
  "action_kind": "payment.charge",
  "amount_unit": "USD_MICROCENTS",
  "amount_micros": 19900000000,
  "policy_keys": {"host": "merchant.example", "custom": {"payment_protocol": "ap2", "currency": "USD"}},
  "issued_at_ms": 1715600000000,
  "committed": true,
  "psp_ref": "psp_abc"
}
```

> **Important.** The receipt is built client-side from the Cycles ALLOW + COMMIT responses. It is **not** signed by the Cycles server in protocol v0.1.26 and must not be relied on as cryptographic evidence by third parties. A server-verifiable variant lands in v0.3 once `cycles-protocol` adds a signed-receipt field.

Disable with `emit_receipt=False` if you don't need it.

## Error handling

```python
from runcycles_ap2 import AP2GuardDenied, AP2CurrencyError, AP2MandateError, cycles_guard_payment

try:
    with cycles_guard_payment(client, mandate=mandate, run_id="r", tenant="acme") as guard:
        psp.charge(mandate)
except AP2GuardDenied as e:
    # Cycles refused the attempt. Real money has NOT moved.
    log.warning("denied", reason_code=e.reason_code, request_id=e.request_id)
except AP2CurrencyError:
    # v0.1 supports USD only.
    log.error("non-usd mandate")
except AP2MandateError:
    # Adapter input is malformed (missing payee, non-decimal amount, etc.).
    log.error("malformed mandate")
```

Exception hierarchy:

| Exception | When |
|---|---|
| `AP2GuardError` | Base for all AP2-guard errors |
| `AP2GuardDenied` | Cycles returned `DENY` or the reserve POST failed |
| `AP2DryRunResult` | Raised from `__enter__` when `dry_run=True` — carries the decision payload; the `with` body never executes |
| `AP2GuardCommitFailed` | Commit was rejected with an unrecognized code after the body ran. Check `.released` (bool) and `.release_error` (string \| None) on the exception — `released=False` means budget is stranded until TTL; reconcile with PSP either way |
| `AP2CurrencyError` | Non-USD mandate in v0.1 (subclass of `ValueError`) |
| `AP2MandateError` | Adapter input is malformed — NaN, infinity, sub-micro precision, missing payee, etc. (subclass of `ValueError`) |

## Features

- **One context manager** — `cycles_guard_payment` wraps a single AP2 payment moment in reserve → commit / release.
- **Deterministic idempotency** — no caller-supplied keys; retries replay the same reservation.
- **Consume-once defense** — duplicate workers on the same mandate collapse onto one reservation server-side.
- **Built-in `payment.charge` action** — no custom action-kind registration, no protocol PR required.
- **Adapter layer** (`AP2Mandate`) insulates from upstream AP2 SDK churn.
- **Pydantic v2 models** with strict validation.
- **Client-side runtime-authority receipt** alongside AP2 dispute evidence (server-verifiable in v0.3).
- **Typed** (`py.typed`) and mypy-strict clean.
- **≥ 95% test coverage** enforced in CI.

## Scope of v0.1

| In scope | Out of scope (v0.2+) |
|---|---|
| Sync context manager | Async API (`AsyncGuardedPayment`) |
| USD payments | Multi-currency |
| `payment.charge`, with override for `payment.refund` | `payment.refund` convenience helper |
| Caller-passed signed mandates | Mandate signing or signature verification |
| Built-in action kinds | Custom action kinds requiring server registration |
| Single-charge flows | Partial capture, multi-shipment, split-tender |

## Example

End-to-end runnable sample in [`examples/ap2_human_not_present.py`](examples/ap2_human_not_present.py). Set the env vars and run:

```bash
CYCLES_BASE_URL=http://localhost:7878 \
CYCLES_API_KEY=test-key \
CYCLES_TENANT=acme \
python examples/ap2_human_not_present.py
```

Set `DRY_RUN=1` to evaluate the policy decision without creating a reservation. Run twice with the same `transaction_id` to see the idempotent replay (server returns the original reservation — the double-spend defense).

## Related packages

| Package | Purpose |
|---|---|
| [`runcycles`](https://github.com/runcycles/cycles-client-python) (PyPI: [`runcycles`](https://pypi.org/project/runcycles/)) | Underlying Cycles SDK — programmatic client, `@cycles` decorator, streaming context manager |
| [`cycles-protocol`](https://github.com/runcycles/cycles-protocol) | Authoritative YAML API specs |
| [`AP2`](https://github.com/google-agentic-commerce/AP2) | Google's Agent Payments Protocol (upstream) |

## Development

```bash
pip install -e ".[dev]"

# Lint + format
ruff check .
ruff format --check .

# Type check (strict mode)
mypy runcycles_ap2

# Run tests with coverage (95% threshold enforced in CI)
pytest --cov=runcycles_ap2 --cov-fail-under=95
```

CI runs all three checks on Python 3.10 and 3.12 for every push and pull request. See [`AUDIT.md`](AUDIT.md) for the protocol-conformance posture, [`CHANGELOG.md`](CHANGELOG.md) for the release log.

## Documentation

- [AP2 Protocol Spec](https://ap2-protocol.org/) — Google's upstream specification
- [AP2 Payment Mandate](https://ap2-protocol.org/ap2/payment_mandate/) — mandate constraints and field reference
- [Cycles Documentation](https://runcycles.io) — Cycles platform docs
- [Cycles Action Kinds Registry](https://github.com/runcycles/cycles-protocol) — authoritative list of built-in action kinds (`payment.charge`, `payment.refund`, etc.)

## Requirements

- Python 3.10+
- `runcycles >= 0.4.1`
- `pydantic >= 2.0`

## License

Apache-2.0 — see [LICENSE](LICENSE).
