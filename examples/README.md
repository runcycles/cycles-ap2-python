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
