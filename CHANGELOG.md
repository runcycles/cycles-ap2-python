# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] — 2026-05-13

### Added
- `cycles_guard_payment(...)` sync context manager: `reserve` on enter, `commit` on clean exit, `release` on exception.
- `AP2Mandate` adapter type insulating the wrapper from upstream AP2 schema churn.
- `RuntimeAuthorityReceipt` (`runtime_authority.ap2.payment.charge.v1`) — client-side, informational.
- Deterministic idempotency keys: `ap2:{sha256(transaction_id)[:32]}:{phase}[:{suffix}]`. Hash is fixed-length (128-bit collision resistance), header-safe, and the phase suffix is always preserved regardless of upstream id length.
- `AP2DryRunResult` exception — raised from `__enter__` when `dry_run=True` so the `with` body cannot execute (prevents real PSP calls under dry-run from moving money off the books).
- `AP2GuardCommitFailed` exception — raised after a release attempt when the server rejects a commit with an unrecognized code; carries `released: bool` and `release_error: str | None` so the caller can distinguish "budget recovered" from "budget stranded until TTL".
- USD-only enforcement; non-USD raises `AP2CurrencyError`. Rejects NaN, +/-Infinity, and amounts with more than 8 decimal places (sub-micro precision); wraps all `decimal` failures as `AP2MandateError`.
- Exact integer-tuple conversion in `amount_micros()` — does not depend on the default decimal context, so large valid inputs convert exactly instead of being silently rounded.
- Pre-allocation digit-count cap (≤ 19 integer digits, the int64 USD_MICROCENTS ceiling) blocks exponent-notation DoS like `Decimal("1E+1000000000000")` that would otherwise hang allocating a trillion-digit scaling factor.
- Post-conversion int64 boundary check (`micros <= 2**63 - 1`) rejects amounts one over int64.max client-side instead of relying on the server.
- 72 tests, ≥ 95% coverage, ruff + mypy strict.

### Planned for v0.2
- `AsyncGuardedPayment` (asyncio).
- Multi-currency.
- `payment.refund` helper.

### Planned for v0.3
- Server-verifiable runtime-authority receipt (requires `cycles-protocol` signed-receipt field).
