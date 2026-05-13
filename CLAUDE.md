## Git Rules — STRICT
- ALWAYS use native git for ALL commits and pushes
- NEVER use mcp__github__ tools for committing or pushing
- Use mcp__github__ ONLY for: PRs, Issues, GitHub Actions
- Write commit messages to a temp file, then: `git commit -F <file>`
- NEVER use --no-gpg-sign flag

# Cycles strict rules
- yaml API specs always the authority (cycles-protocol, AP2)
- always update AUDIT.md when making changes that affect public API, wire-level payload shape, or AP2/Cycles conformance posture
- maintain at least 95% or higher test coverage for all code repos

# AP2 strict rules
- this package is a Cycles-side guard; it MUST NOT verify or sign AP2 mandates — that is the upstream SDK's job
- `AP2Mandate` is the adapter layer; upstream field renames touch only `models.py` and `mapping.py`
- never default to a custom action kind that requires server-side registration; default is built-in `payment.charge`
- USD only in v0.1 — non-USD raises `AP2CurrencyError` client-side
- idempotency keys are derived deterministically from `transaction_id`; callers MUST NOT pass their own

# Build & Test
- Install: `pip install -e ".[dev]"`
- Test: `pytest`
- Test with coverage: `pytest --cov=runcycles_ap2`
- Lint & format: `ruff check` / `ruff format`
- Type check: `mypy runcycles_ap2`
