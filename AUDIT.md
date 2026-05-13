# AUDIT

Per `CLAUDE.md`: this file records material changes to the repo (server, admin, client). For a client package, that means public API, on-the-wire request shape, and protocol-conformance posture.

## 2026-05-13 — second-round review fixes (2×P2 + P3)

**Author:** code-review response on PR #1 (round 2)
**Scope:** payment-math correctness, exception fidelity, doc parity

1. **[P2] Decimal default-context rounding** — `amount_micros()` previously computed `value * 10**8` as a Decimal multiplication, which uses the default 28-digit decimal context and silently rounded inputs larger than the protocol cap (a malformed mandate could carry such a value, e.g. `123456789012345678901.12345678` produced `...680` instead of `...678`). **Fix:** rewrote conversion to operate directly on `Decimal.as_tuple()` digits — exact integer math, no context dependence. Removed the now-unused `USD_MICROCENTS_PER_DOLLAR` constant. New regression test in `TestAmountMicros::test_large_value_converts_exactly`.

2. **[P2] Release-failure obscured by `AP2GuardCommitFailed` message** — when a commit was rejected with an unrecognized code, the wrapper attempted to release the reservation and raised `AP2GuardCommitFailed` saying *"reservation released"* regardless of whether the release actually succeeded. If release transport-failed or returned 5xx, budget was stranded and the caller had no signal. **Fix:** `_handle_release()` now returns `(success: bool, error_detail: str | None)`. `AP2GuardCommitFailed` gained `.released` and `.release_error` attributes; the exception message says either "reservation released" or "reservation release FAILED ... budget stranded until TTL" based on the actual outcome. Two regression tests cover the transport-failure and non-success-response paths.

3. **[P3] Stale README lifecycle table rows** — the response-table rows still referenced raw `ap2:{transaction_id}:commit` / `:release:{ExcType}` shapes, contradicting the hashed shape documented in the *Deterministic idempotency keys* section. **Fix:** rewrote the table rows to reference the keys section and to reflect the new "release + raise" semantics for unrecognized commit rejections.

**Test posture after fixes:**
- 66 tests (up from 62), 97.87% coverage.

**Public API additions:**
- `AP2GuardCommitFailed.released: bool`
- `AP2GuardCommitFailed.release_error: str | None`

No protocol changes required.

## 2026-05-13 — pre-release review fixes (P1 + 3×P2)

**Author:** code-review response on PR #1
**Scope:** wire shape, public API surface, validation

Four findings from review addressed before v0.1.0 release:

1. **[P1] Idempotency key collision** — `idempotency_key()` previously appended the phase suffix to the raw `transaction_id` and sliced the whole string to 256 chars. Two 256-char transaction_ids sharing the first 252 chars produced the same reserve key; a single 256-char id produced identical reserve/commit keys (phase suffix stripped). The raw `transaction_id` could also include whitespace / control bytes that reached the `Idempotency-Key` header. **Fix:** key shape changed to `ap2:{sha256(transaction_id)[:32]}:{phase}[:{suffix}]`. Hash is fixed-length (32 hex chars, 128-bit collision resistance), header-safe (hex only), and phase is always preserved. The raw `transaction_id` is still attached to `Subject.dimensions["ap2_transaction_id"]` for debug. **This is a wire-shape change** — pre-release so no migration cost.

2. **[P2] Dry-run still executed body** — `__enter__` with `dry_run=True` previously returned a guard normally; only `__exit__` skipped commit/release. If a caller ran a real PSP charge inside the `with` body, money would move with no Cycles record. **Fix:** `__enter__` now raises `AP2DryRunResult` (new exception carrying decision payload) so the `with` body is unreachable in dry-run mode. Public API: added `AP2DryRunResult` to exports.

3. **[P2] Decimal validation gaps** — `AP2Mandate.amount_micros()` accepted `NaN` and `+/-Infinity` (Pydantic-validated through `Decimal()`), then raised raw `decimal.InvalidOperation` or `OverflowError` later. Sub-micro precision (>8 decimal places) was silently rounded. **Fix:** explicit `is_finite()` check; `as_tuple().exponent < -8` rejection; all decimal failures (`DecimalException`, `OverflowError`) wrapped as `AP2MandateError`.

4. **[P2] Commit failures invisible to caller** — unrecognized commit rejection (e.g., 400 INVALID_REQUEST after PSP charge) was logged + released and the context manager exited normally. Caller's only signal was `guard.committed == False`, easy to miss → unreconciled payment state. **Fix:** added `AP2GuardCommitFailed` (new exception carrying `error_code`, `request_id`, `reservation_id`); raised from `_handle_commit` after the release in the unrecognized-rejection branch. `RESERVATION_FINALIZED` / `RESERVATION_EXPIRED` / `IDEMPOTENCY_MISMATCH` still return silently — those are benign replays of a prior attempt.

**Test posture after fixes:**
- 62 tests (up from 53), 97.45% coverage.
- New regression tests cover: long tx_id phase preservation, distinct-but-similar tx_id collision avoidance, header-unsafe char sanitization, NaN / +/-Infinity rejection, sub-micro precision rejection, dry-run body unreachability, commit-failure exception surfacing.

**Public API additions:**
- `AP2DryRunResult` exception
- `AP2GuardCommitFailed` exception

No protocol changes required.

## 2026-05-13 — v0.1.0 initial scaffold

**Author:** initial commit
**Scope:** new repo, no protocol changes

- Created `runcycles_ap2` package: `cycles_guard_payment` sync context manager wrapping a Cycles `reserve / commit / release` lifecycle around an AP2 Payment Mandate.
- AP2 → Cycles wire mapping:
  - `Action.kind = "payment.charge"` (built-in high-risk kind from `cycles-action-kinds-v0.1.26.yaml:1562-1574`; **no custom kinds required, no protocol change**).
  - `Amount.unit = USD_MICROCENTS`; USD only in v0.1, non-USD raises `ValueError` client-side.
  - `Subject.dimensions` keys: `run_id`, `ap2_transaction_id`, `checkout_hash`, `open_mandate_hash` (all under 256 chars, ≤ 16 dims).
  - `action.policy_keys.host = payee_website`; `policy_keys.custom = {payment_protocol: "ap2", currency: "USD"}`. Attached at the reservation request body level (raw dict path through `CyclesClient.create_reservation`) since the v0.4.1 `runcycles.models.Action` does not yet surface `policy_keys` as a typed field.
- Idempotency policy: client computes deterministic keys per phase from `transaction_id` — `ap2:{txid}:reserve`, `ap2:{txid}:commit`, `ap2:{txid}:release:{ExcType}`. This is the consume-once defense against mandate reuse across retries / concurrent attempts.
- Runtime-authority receipt: client-side derivation only (schema `runtime_authority.ap2.payment.charge.v1`). **Not server-verifiable in protocol v0.1.26.** Promoted to a signed protocol field in v0.3.
- Test coverage: ≥ 95% enforced via `pyproject.toml: fail_under = 95`. CI workflow mirrors `cycles-client-python`.

**Risks acknowledged in this version:**
- PSP failure *after* clean `__exit__` will commit budget against a failed charge. Mitigation: README mandates raising inside the `with` block on PSP error.
- TTL timeout is server-side expiry, not a client release.
- AP2 SDK is not yet on PyPI; `AP2Mandate` is our adapter layer so upstream field renames touch only `models.py` + `mapping.py`.
