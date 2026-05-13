"""End-to-end example: guard an AP2 human-not-present payment with Cycles.

Runs against a local Cycles server (see cycles-client-python README). The PSP is a
fake — replace with a real charge call in production.

Usage:
    CYCLES_BASE_URL=http://localhost:7878 CYCLES_API_KEY=test CYCLES_TENANT=acme \\
        python examples/ap2_human_not_present.py

    # Probe the decision without creating a reservation or moving money:
    DRY_RUN=1 python examples/ap2_human_not_present.py
"""

from __future__ import annotations

import json
import os

from runcycles import CyclesClient, CyclesConfig

from runcycles_ap2 import AP2DryRunResult, AP2Mandate, cycles_guard_payment


def fake_psp_charge(mandate: AP2Mandate) -> dict[str, str]:
    """Stand-in for a real payment-service-provider call."""
    return {"id": f"psp_{mandate.transaction_id}", "status": "captured"}


def run_dry_run(client: CyclesClient, mandate: AP2Mandate, tenant: str) -> None:
    """Dry-run is a POLICY PROBE — the `with` body never runs. We catch the result."""
    try:
        with cycles_guard_payment(
            client,
            mandate=mandate,
            run_id="run_demo_001",
            tenant=tenant,
            agent="checkout-bot",
            workflow="ap2-human-not-present",
            dry_run=True,
        ):
            # This block is unreachable under dry_run=True — AP2DryRunResult was raised.
            raise AssertionError("dry-run body must not execute")
    except AP2DryRunResult as result:
        print(f"dry-run decision={result.decision}, reason_code={result.reason_code}")


def run_real(client: CyclesClient, mandate: AP2Mandate, tenant: str) -> None:
    with cycles_guard_payment(
        client,
        mandate=mandate,
        run_id="run_demo_001",
        tenant=tenant,
        agent="checkout-bot",
        workflow="ap2-human-not-present",
    ) as guard:
        print(f"decision={guard.decision}, reservation_id={guard.reservation_id}")
        psp = fake_psp_charge(mandate)
        guard.attach_receipt_fields(psp_ref=psp["id"])

    if guard.receipt is not None:
        print(json.dumps(guard.receipt.model_dump(by_alias=True), indent=2))


def main() -> None:
    config = CyclesConfig(
        base_url=os.environ.get("CYCLES_BASE_URL", "http://localhost:7878"),
        api_key=os.environ.get("CYCLES_API_KEY", "test-key"),
        tenant=os.environ.get("CYCLES_TENANT", "acme"),
    )
    mandate = AP2Mandate(
        transaction_id="ap2-tx-demo-001",
        amount_value="199.00",
        currency="USD",
        payee_website="merchant.example",
        checkout_hash="ch_demo_001",
    )
    with CyclesClient(config) as client:
        if os.environ.get("DRY_RUN") == "1":
            run_dry_run(client, mandate, config.tenant or "acme")
        else:
            run_real(client, mandate, config.tenant or "acme")


if __name__ == "__main__":
    main()
