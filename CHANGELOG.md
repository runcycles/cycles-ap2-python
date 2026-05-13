# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] — 2026-05-13

### Added
- `cycles_guard_payment(...)` sync context manager: `reserve` on enter, `commit` on clean exit, `release` on exception.
- `AP2Mandate` adapter type insulating the wrapper from upstream AP2 schema churn.
- `RuntimeAuthorityReceipt` (`runtime_authority.ap2.payment.charge.v1`) — client-side, informational.
- Deterministic idempotency keys (`ap2:{transaction_id}:reserve|commit|release:{...}`).
- USD-only enforcement; non-USD raises `ValueError`.
- 8 tests, ≥ 95% coverage, ruff + mypy strict.

### Planned for v0.2
- `AsyncGuardedPayment` (asyncio).
- Multi-currency.
- `payment.refund` helper.

### Planned for v0.3
- Server-verifiable runtime-authority receipt (requires `cycles-protocol` signed-receipt field).
