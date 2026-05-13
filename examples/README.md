# Examples

## ap2_human_not_present.py

End-to-end demo of guarding an AP2 human-not-present payment with a Cycles `reserve / commit / release` lifecycle.

```bash
pip install -e ".[dev]"
# point at a running Cycles server with `payment.charge` permitted under your tenant:
CYCLES_BASE_URL=http://localhost:7878 \
CYCLES_API_KEY=test-key \
CYCLES_TENANT=acme \
python examples/ap2_human_not_present.py
```

Set `DRY_RUN=1` to evaluate the policy decision without creating a reservation. Re-run with the same `transaction_id` to demonstrate that the server returns the original reservation (idempotent replay — the double-spend defense).

## ap2_human_not_present_async.py

Same demo, async. Use this when your agent runtime is asyncio-based (FastAPI, anyio, the OpenAI async SDK, etc.). Same environment variables and `DRY_RUN` toggle. The mandate in this variant also sets `open_mandate_hash`, so the idempotency lock is keyed on the open mandate (the AP2 §6 consume-once boundary) rather than `transaction_id`.
