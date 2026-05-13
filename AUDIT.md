# AUDIT

Per `CLAUDE.md`: this file records material changes to the repo (server, admin, client). For a client package, that means public API, on-the-wire request shape, and protocol-conformance posture.

## 2026-05-13 — live integration smoke tests (post-v0.2.0)

**Author:** post-release hygiene
**Scope:** test surface only; no public API change, no wire change, no version bump

Added `tests/integration/test_live_ap2_guard.py` — five smoke tests (four sync + one async) exercising the AP2 wrapper end-to-end against a real Cycles server. Pattern mirrors `cycles-client-python/tests/integration/test_live_server.py`: module-level `pytest.mark.skipif(not CYCLES_BASE_URL)` so the whole file is skipped at collection time when env vars are unset. Default `pytest` runs and CI ignore it. Run locally with `CYCLES_BASE_URL=... CYCLES_API_KEY=... CYCLES_TENANT=... pytest tests/integration -v`.

Each test uses a fresh UUID-based `transaction_id` and a `0.00000001` USD amount so the suite is idempotent across runs and doesn't consume meaningful budget. Covers: sync clean commit, sync exception→release, sync dry-run, async clean commit with `open_mandate_hash` scope, sync idempotent replay.

This catches wire-shape regressions that the existing 147 mock-based tests can't (e.g., a server-side rename of a `Subject.dimensions` key, a field that flips from string to enum, etc.).

## 2026-05-13 — v0.2.0 — AsyncGuardedPayment

**Author:** v0.2.0 release
**Scope:** new public API surface (async). No wire-shape, exception, or validation changes.

- Added `runcycles_ap2.AsyncGuardedPayment` — async context manager (`async with`) that mirrors `GuardedPayment` exactly.
- Added `runcycles_ap2.cycles_guard_payment_async(...)` factory.
- The async variant uses `runcycles.AsyncCyclesClient` for I/O; everything else (idempotency keying, mapping, validation, exceptions, receipt construction) is shared via the existing module-level helpers.
- Behaviour parity with the sync variant on every documented contract: same `AP2GuardDenied` on DENY / failed reserve, same `AP2DryRunResult` on dry-run, same `AP2GuardCommitUncertain` on post-PSP unknown outcomes (terminal codes / transport / 5xx / uncaught), same `AP2GuardCommitFailed` on 4xx unrecognized commit rejection, same auto-release on exception inside the body, same idempotency-key derivation including the open-mandate consume-once scope.
- New example: `examples/ap2_human_not_present_async.py`.
- README quickstart now includes an "Async variant (v0.2+)" snippet.

**Public API additions:**
- `AsyncGuardedPayment` class
- `cycles_guard_payment_async(...)` factory

**Test posture after addition (including the audit follow-up commit and the CancelledError fix):**
- 147 tests (up from 110), 99.20% coverage.
- 37 tests in `tests/test_async_guard.py` mirror the sync surface across clean commit, dry-run, denial, release on exception, all five commit-uncertain branches (terminal codes, 5xx with/without body, transport error, uncaught exception, asyncio cancellation), commit-failed branches incl. release-failure recording, and an AP2 sample-type end-to-end through the async path.

**Cancellation policy on async commit:** an outer `asyncio.CancelledError` during the in-flight commit POST is treated as commit-uncertain. Since `asyncio.CancelledError` inherits from `BaseException`, the generic `except Exception` clause does not catch it; without an explicit handler, the cancellation would escape as raw `CancelledError` with no `reservation_id` or `error_code`, despite the post-PSP unknown-outcome contract. The async `_handle_commit` now explicitly converts it to `AP2GuardCommitUncertain(error_code="COMMIT_CANCELLED")` with the original on `__cause__`. No release is attempted (the commit may have reached and settled Cycles before the cancel landed). Sync code path is unaffected; cancellation only applies to async.

**No protocol changes. No wire-shape changes.** Existing v0.1.x callers see the sync API entirely unchanged.

## 2026-05-13 — positioning-review round 4 (ASCII suffix + empty-hash preservation + docstrings)

**Author:** strategic/positioning review round 4 (PR #2 still in-flight)
**Scope:** correctness polish + docstring sync

1. **[P2] ASCII-only filter in idempotency-key suffix** — `safe_suffix` used `c.isalnum()`, but Python's `str.isalnum` is Unicode-aware (e.g. `"É".isalnum() == True`). A non-ASCII exception class name (or any non-ASCII suffix input) would have reached the `Idempotency-Key` HTTP header — RFC 7230 requires ASCII tokens, so httpx would reject the request. **Fix:** tighten the predicate to `(c.isascii() and c.isalnum()) or c in ("_", "-", ".")`. Regression test asserts the key is `isascii()` even when given a French-style suffix `"Échec"`.

2. **[P3] `AP2Mandate.from_ap2` preserved empty `checkout_hash`** — the `or` short-circuit at `getattr(..., "hash", None) or getattr(..., "checkout_hash", None)` masked upstream `hash=""` as `None`, bypassing the model's `min_length=1` rejection. **Fix:** explicit `None` check on the first alternative; fall back only when the first attr is genuinely absent. Two regression tests: empty `hash=""` now raises `ValidationError` at construction; alternate naming (`checkout_hash` attr with no `hash` attr) still falls through correctly.

3. **[P3] Stale docstrings refreshed** —
   - `mapping.build_commit_body.__doc__` now references the consume-once scope rather than `transaction_id` exclusively.
   - `AP2GuardCommitFailed.__doc__` rewritten: it covers only 4xx-explicit-rejection now; the previous paragraph that claimed it excludes `RESERVATION_FINALIZED` / `RESERVATION_EXPIRED` / `IDEMPOTENCY_MISMATCH` as "benign replay" was misleading — those raise `AP2GuardCommitUncertain` (a different exception), not a quiet no-op. The new docstring points readers at `AP2GuardCommitUncertain` for all unknown-outcome conditions.

**Test posture after fixes:**
- 110 tests (up from 107), 99.19% coverage.

No public API additions; only behavior tightening on existing paths.

## 2026-05-13 — positioning-review round 3 (P0 transport/5xx routing + P1 bare-exception wrap)

**Author:** strategic/positioning review round 3 (PR #2 still in-flight)
**Scope:** completing the commit-uncertain contract for all post-PSP failure modes

1. **[P0] Transport errors and 5xx responses on commit are now uncertain (no release)** — previously they fell into the "unrecognized rejection" branch, which called `_handle_release` and raised `AP2GuardCommitFailed` with the message "reservation released" regardless. Wrong: the commit POST may have reached Cycles and mutated state before the failure was observed, so auto-release risks undoing a successful settle. New branch ordering in `_handle_commit`: success → transport/5xx → terminal codes → 4xx unrecognized. The first three all raise `AP2GuardCommitUncertain` with no release; only 4xx-with-unrecognized-code still releases. Synthetic `error_code` values: `TRANSPORT_ERROR` for transport-level failures, `SERVER_ERROR` for 5xx without a parseable code (specific codes propagate when present).

2. **[P1] Bare exceptions during commit POST now surface as commit-uncertain** — the `try / except Exception / raise` block re-raised the raw exception, losing the `reservation_id` and bypassing the reconciliation contract. Now wrapped as `AP2GuardCommitUncertain(error_code="COMMIT_RAISED", reservation_id=...) from exc` so the caller still gets the standard fields and the original exception is preserved via `__cause__`.

3. **Docstring updated** — `AP2GuardCommitUncertain.__doc__` enumerates all the new conditions and how to distinguish them via `error_code`.

**Test posture after fixes:**
- 107 tests (up from 103), 99.18% coverage.

**Four new regression tests:**
- `test_commit_5xx_raises_uncertain_no_release`
- `test_commit_5xx_without_body_uses_synthetic_code`
- `test_commit_transport_error_raises_uncertain_no_release`
- `test_commit_raises_exception_surfaces_as_uncertain`

No public API additions; only behavior shift inside `_handle_commit` and an expanded contract on the existing `AP2GuardCommitUncertain`.

## 2026-05-13 — positioning-review follow-ups (wording accuracy + empty-hash guard)

**Author:** strategic/positioning review round 2 (PR #2 in-flight)
**Scope:** documentation accuracy, additional regression coverage, defensive validation

1. **Reworded "collapses onto one reservation"** — earlier wording implied server-side merging when the actual semantics are: same key + same payload replays the original; same key + divergent payload returns `409 IDEMPOTENCY_MISMATCH` (surfaced as `AP2GuardDenied`). Both prevent a second valid reservation, but the mechanism differs. README, AUDIT, and two test docstrings now describe the actual contract. The consume-once defense itself is unchanged.

2. **Stale `transaction_id`-only retry wording** — `GuardedPayment.__doc__` and the README lifecycle table still implied the retry/lock key is always `transaction_id`. Generalized to "consume-once key (`open_mandate_hash` when present, otherwise `transaction_id`)".

3. **New regression test for IDEMPOTENCY_MISMATCH on second reserve** — `test_open_mandate_divergent_payload_reserve_mismatch_raises_denied` exercises the second half of the consume-once contract: same open mandate hash, distinct transaction_id, first reserve allows, second reserve gets 409 from server → `AP2GuardDenied(reason_code="IDEMPOTENCY_MISMATCH")`. Documents the actual server semantics for divergent payloads sharing the open-mandate scope.

4. **Reject empty `open_mandate_hash` / `checkout_hash`** — pydantic `min_length=1` on both fields. Previously an empty string would pass model validation, then `consume_once_input`'s truthy check would silently fall back to the `tx` scope — data corruption disguised as the default. Now fails fast at model construction. Two new tests cover the rejection paths.

**Test posture after fixes:**
- 103 tests (up from 100), 98.35% coverage.

No public API additions in this round.

## 2026-05-13 — positioning-review fixes (P0-A consume-once, P0-B commit-uncertain)

**Author:** strategic/positioning review response (still v0.1.0; not yet on PyPI)
**Scope:** consume-once correctness, post-PSP failure visibility, README accuracy

1. **[P0-A] Open-mandate consume-once locking** — earlier rounds keyed idempotency on `transaction_id` only. That breaks the AP2 spec's normative defense (specification §6) against an autonomous agent presenting subsequent open mandates without a rejection receipt: two distinct transactions derived from the same open mandate would produce different idempotency keys and BOTH would be authorized. **Fix:** when `AP2Mandate.open_mandate_hash` is present, key the lock on `open_mandate_hash` instead of `transaction_id`. Scope namespace embedded in the key so the two buckets never collide server-side:
   - human-not-present: `ap2:open_mandate:{sha256(open_mandate_hash)[:32]}:{phase}[:{suffix}]`
   - default / human-present: `ap2:tx:{sha256(transaction_id)[:32]}:{phase}[:{suffix}]`
   New `mapping.consume_once_input()` returns the (scope, raw_value) pair. The `idempotency_key()` function signature changed from `(transaction_id: str, ...)` to `(mandate: AP2Mandate, ...)` to enable automatic scope selection. **Wire-shape change** — pre-PyPI so no migration. New test class `TestIdempotencyKeyOpenMandateScope` + `test_open_mandate_overuse_shares_idempotency_bucket_across_transactions` cover the regression (the test was renamed in the follow-up wording pass to avoid the "collapse" overclaim).

2. **[P0-B] Post-PSP commit failures now raise instead of silently logging** — `RESERVATION_FINALIZED`, `RESERVATION_EXPIRED`, and `IDEMPOTENCY_MISMATCH` previously returned silently with a warning log; callers only saw `guard.committed == False`. After the PSP body has run, any of these is a reconciliation event the caller MUST handle. `RESERVATION_EXPIRED` in particular is dangerous: the PSP may have charged while Cycles reclaimed the budget on TTL. **Fix:** new `AP2GuardCommitUncertain` exception (carries `error_code` + `request_id` + `reservation_id`); raised from `_handle_commit` for all three terminal-status codes. Still no auto-release (a prior commit may already have settled). Existing tests that asserted silent behavior were flipped to expect the raise; new `RESERVATION_EXPIRED` test added.

3. **README polish** — added "Independent project — not affiliated with Google" disclaimer; added "What this does NOT do" section (no signature verification, no mandate creation, etc.); softened "any AP2-compatible SDK" wording to "AP2-style PaymentMandate objects via a small adapter layer"; tightened headline value-prop with a bulleted "prevent these failure modes" list; updated lifecycle table for commit-uncertain raise; updated idempotency key table to show the dual-scope behavior.

4. **Real AP2-shaped adapter test** — new `tests/test_ap2_shape_adapter.py` uses local dataclasses matching the upstream AP2 sample-type layout (`PaymentMandate`, `CheckoutMandate`, `Payee`, `PaymentAmount`) so `AP2Mandate.from_ap2()` is exercised against shape closer to real AP2 SDK objects, not just our own internal fakes. If upstream renames a field, this test fails first.

**Test posture after fixes:**
- 100 tests (up from 90), 98.35% coverage.

**Public API additions:**
- `AP2GuardCommitUncertain` exception class (exported)

**Wire-shape change (acceptable because pre-PyPI):**
- idempotency keys now include a scope namespace: `ap2:tx:...` or `ap2:open_mandate:...` (was `ap2:...`)

## 2026-05-13 — sixth-round review fix (P2 non-int amount types)

**Author:** code-review response on PR #1 (round 6)
**Scope:** correctness — exact-type validation on commit-amount inputs

**[P2] `set_actual_micros` and `build_commit_body` accepted non-int types** — the round-5 fix bounded numerical comparisons, but `bool` is an `int` subclass (`isinstance(True, int) is True`) and `float < int` compares numerically. `set_actual_micros(True)` would ship `true` as `actual.amount`; `set_actual_micros(1.5)` would ship `1.5` and only surface as a pydantic error during receipt construction — *after* the commit POST had already gone out. **Fix:** new private `runcycles_ap2._validation.validate_micros(amount, *, field)` helper using `type(amount) is int` to reject `bool` (and everything else non-int). Wired into both `GuardedPayment.set_actual_micros` AND `mapping.build_commit_body` so direct callers of the builder get the same protection. 14 new tests cover `True`/`False`/`1.5`/`1.0`/`"100"`/`None`/`[100]` across both entry points.

**Test posture after fix:**
- 90 tests (up from 75), 98.29% coverage.

**New internal module:**
- `runcycles_ap2/_validation.py` (private; not exported)

## 2026-05-13 — fifth-round review fix (P2 bypass via set_actual_micros)

**Author:** code-review response on PR #1 (round 5)
**Scope:** correctness — int64 cap on the commit-path override

**[P2] `set_actual_micros()` bypassed the int64 ceiling** — round 4 added the int64 cap to `AP2Mandate.amount_micros()`, but the caller-supplied commit override on `GuardedPayment.set_actual_micros()` only rejected negative values. Passing `2**63` flowed through to `build_commit_body` and into the wire payload as `actual.amount = 9223372036854775808`. **Fix:** mirror the same `0 <= amount <= MAX_USD_MICROS` validation; raise `AP2MandateError` (was plain `ValueError`) so all amount-validation errors are reachable via one exception type. Extracted `MAX_USD_MICROS = 2**63 - 1` to `_constants.py` as the shared source of truth between `models.py` and `guard.py`. Three regression tests cover int64.max acceptance, int64.max + 1 rejection (commit not called, release called), and the existing negative-amount path now raising `AP2MandateError`.

**Test posture after fix:**
- 75 tests (up from 72), 98.23% coverage.

**Public API change (very minor):**
- `set_actual_micros(amount)` now raises `AP2MandateError` (a `ValueError` subclass) instead of plain `ValueError`. Code catching `ValueError` still works.

## 2026-05-13 — fourth-round review fix (P2 exact int64 boundary)

**Author:** code-review response on PR #1 (round 4)
**Scope:** correctness at the int64 boundary

**[P2] 19-digit cap permits values one over int64.max** — the round-3 fix bounded `len(digits) + max(0, exponent)` to 19 to block the DoS allocation. That cap correctly rejects 20-digit inputs but lets values like `92233720368.54775808` (int64.max + 1) and `99999999999.99999999` (≈ 10^19 micros) slip through. The server would reject them, but client-side we'd already have shipped the wrong number. **Fix:** add a post-conversion check `micros <= 2**63 - 1` (`_MAX_USD_MICROS = 9_223_372_036_854_775_807`). The 19-digit cap remains as the pre-allocation DoS guard; the post-conversion check is the exact protocol boundary. Three regression tests added: int64.max is accepted, int64.max + 1 is rejected, 19-digit-with-fractional `99999999999.99999999` is rejected.

**Test posture after fix:**
- 72 tests (up from 69), 97.92% coverage.

No public API additions.

## 2026-05-13 — third-round review fixes (P2 DoS + P3 stale docs)

**Author:** code-review response on PR #1 (round 3)
**Scope:** denial-of-service vector, internal/external doc parity

1. **[P2] Exponent-notation DoS in `amount_micros()`** — a short, finite, positive Decimal like `1E+1000000000000` (16 chars, fits the 64-char field) used to pass every validation: `is_finite()` is True, value is positive, and exponent ≥ -8. The code then tried to compute `10 ** (10**12 + 8)`, allocating a trillion-digit integer and hanging the process. Even `0E+1000000000000` triggered the same allocation before the multiplication zeroed the result. **Fix:** pre-allocation digit-count cap. `total_integer_digits = len(digits) + max(0, exponent)` must be ≤ 19 (the digit count of int64.max, which is the protocol's USD_MICROCENTS ceiling). New regression tests cover `1E+1000000000000`, `0E+1000000000000`, and 20-digit "legitimate-shaped but out-of-range" amounts. Also updated the earlier `test_large_value_converts_exactly` test which used a 29-digit value beyond int64 — now uses int64.max (`92233720368.54775807`) which still exercises the no-rounding path.

2. **[P3] Stale README mapping row + `GuardedPayment` class docstring** — README mapping table still claimed `int(round(value * 1e8))` (which is what the old default-context multiplication did) instead of the current exact-integer-tuple conversion. `GuardedPayment.__doc__` still showed `ap2:{tx}:commit` raw-key shapes. **Fix:** rewrote both to match the implementation; docstring now references `runcycles_ap2.mapping.idempotency_key`, calls out the dry-run probe behavior and `AP2GuardCommitFailed.released`/`release_error` for callers reading source.

**Test posture after fixes:**
- 69 tests (up from 66), 97.89% coverage.

No public API additions.

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
