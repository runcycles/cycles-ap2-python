# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] — 2026-05-13

### Added
- `AsyncGuardedPayment` and `cycles_guard_payment_async(...)` — async-context-manager variant for asyncio runtimes (FastAPI, anyio, OpenAI async SDK, etc.). Same exception contract, same idempotency-key derivation (including the open-mandate consume-once scope), and same commit-uncertainty handling as the sync `GuardedPayment`.
- New example `examples/ap2_human_not_present_async.py`.
- README "Async variant (v0.2+)" quickstart snippet.
- 37 tests in `tests/test_async_guard.py` mirroring the sync test surface (clean commit, dry-run, denial, release on exception, commit-uncertain branches incl. cancellation, commit-failed branches incl. release-failure recording, AP2 sample-type adapter end-to-end). Total 147 tests, 99.20% coverage. ruff + mypy strict.

### Unchanged
- No wire-shape changes. No exception or validation changes. Existing v0.1.x sync callers see the API entirely unchanged.

### Still planned for v0.3
- Multi-currency.
- `payment.refund` helper.
- Server-verifiable runtime-authority receipt (requires `cycles-protocol` signed-receipt field).

## [0.1.0] — 2026-05-13

### Added
- `cycles_guard_payment(...)` sync context manager: `reserve` on enter, `commit` on clean exit, `release` on exception.
- `AP2Mandate` adapter type insulating the wrapper from upstream AP2 schema churn.
- `RuntimeAuthorityReceipt` (`runtime_authority.ap2.payment.charge.v1`) — client-side, informational.
- Deterministic idempotency keys with automatic consume-once scope selection — `ap2:open_mandate:{sha256(open_mandate_hash)[:32]}:{phase}` when the mandate carries an open mandate hash (AP2 spec §6 normative consume-once defense for human-not-present flows), `ap2:tx:{sha256(transaction_id)[:32]}:{phase}` otherwise. Hash is fixed-length (128-bit collision resistance), header-safe, and the phase suffix is always preserved.
- `AP2DryRunResult` exception — raised from `__enter__` when `dry_run=True` so the `with` body cannot execute (prevents real PSP calls under dry-run from moving money off the books).
- `AP2GuardCommitFailed` exception — raised after a release attempt when the server rejects a commit with an unrecognized code; carries `released: bool` and `release_error: str | None` so the caller can distinguish "budget recovered" from "budget stranded until TTL".
- `AP2GuardCommitUncertain` exception — raised whenever the commit outcome is unknown after the PSP body ran: terminal codes (`RESERVATION_FINALIZED` / `RESERVATION_EXPIRED` / `IDEMPOTENCY_MISMATCH`), transport-level failures (`error_code="TRANSPORT_ERROR"`), 5xx server errors (`SERVER_ERROR` or specific code), uncaught exceptions during commit (`COMMIT_RAISED`, original chained via `__cause__`), and — async only — `asyncio.CancelledError` mid-flight (`COMMIT_CANCELLED`, original chained via `__cause__`). No auto-release in any of these cases — the POST may have mutated Cycles before the failure.
- USD-only enforcement; non-USD raises `AP2CurrencyError`. Rejects NaN, +/-Infinity, and amounts with more than 8 decimal places (sub-micro precision); wraps all `decimal` failures as `AP2MandateError`.
- Exact integer-tuple conversion in `amount_micros()` — does not depend on the default decimal context, so large valid inputs convert exactly instead of being silently rounded.
- Pre-allocation digit-count cap (≤ 19 integer digits, the int64 USD_MICROCENTS ceiling) blocks exponent-notation DoS like `Decimal("1E+1000000000000")` that would otherwise hang allocating a trillion-digit scaling factor.
- Post-conversion int64 boundary check (`micros <= 2**63 - 1`) rejects amounts one over int64.max client-side instead of relying on the server. Applied symmetrically to `AP2Mandate.amount_micros()` and `GuardedPayment.set_actual_micros()` so a caller-supplied commit override cannot bypass the cap.
- `set_actual_micros()` raises `AP2MandateError` (a `ValueError` subclass) for both negative and over-int64 inputs, plus any non-int type (notably `bool` and `float`, which numerical comparisons would otherwise let slip through).
- Direct callers of `mapping.build_commit_body(actual_micros=...)` get the same input validation via a shared private validator.
- `AP2Mandate.checkout_hash` and `open_mandate_hash` require `min_length=1` when present — empty strings would otherwise silently fall back to the transaction-id lock scope.
- Idempotency-key suffix filter is ASCII-only (`isascii() and isalnum()` — defends against Unicode-aware `isalnum()` letting non-ASCII chars reach the HTTP header).
- `AP2Mandate.from_ap2()` preserves an empty upstream `checkout_hash` so the model's `min_length=1` constraint can reject it (was previously masked to `None` by a falsy-`or` short-circuit).
- 110 tests, ≥ 95% coverage, ruff + mypy strict.

### Planned for v0.3
- Multi-currency.
- `payment.refund` helper.
- Server-verifiable runtime-authority receipt (requires `cycles-protocol` signed-receipt field).
