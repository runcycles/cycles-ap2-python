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
- `AP2GuardCommitFailed` exception — raised after release when the server rejects a commit with an unrecognized code; surfaces PSP/Cycles reconciliation events that were previously only visible via `guard.committed == False`.
- USD-only enforcement; non-USD raises `AP2CurrencyError`. Rejects NaN, +/-Infinity, and amounts with more than 8 decimal places (sub-micro precision); wraps all `decimal` failures as `AP2MandateError`.
- 62 tests, ≥ 95% coverage, ruff + mypy strict.

### Planned for v0.2
- `AsyncGuardedPayment` (asyncio).
- Multi-currency.
- `payment.refund` helper.

### Planned for v0.3
- Server-verifiable runtime-authority receipt (requires `cycles-protocol` signed-receipt field).
