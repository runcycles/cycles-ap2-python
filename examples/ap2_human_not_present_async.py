"""End-to-end async example: guard an AP2 human-not-present payment with Cycles.

Mirrors examples/ap2_human_not_present.py exactly except the I/O is awaited and
the client is AsyncCyclesClient. Useful when the agent runtime is asyncio-based
(FastAPI, anyio, the OpenAI async SDK, etc.).

Usage:
    CYCLES_BASE_URL=http://localhost:7878 CYCLES_API_KEY=test CYCLES_TENANT=acme \\
        python examples/ap2_human_not_present_async.py

    # Probe the decision without creating a reservation or moving money:
    DRY_RUN=1 python examples/ap2_human_not_present_async.py
"""

from __future__ import annotations

import asyncio
import json
import os

from runcycles import AsyncCyclesClient, CyclesConfig

from runcycles_ap2 import AP2DryRunResult, AP2Mandate, cycles_guard_payment_async


async def fake_psp_charge_async(mandate: AP2Mandate) -> dict[str, str]:
    """Stand-in for a real async payment-service-provider call."""
    await asyncio.sleep(0)  # simulate an awaitable I/O boundary
    return {"id": f"psp_{mandate.transaction_id}", "status": "captured"}


async def run_dry_run(client: AsyncCyclesClient, mandate: AP2Mandate, tenant: str) -> None:
    """Dry-run is a policy probe — the ``async with`` body never runs."""
    try:
        async with cycles_guard_payment_async(
            client,
            mandate=mandate,
            run_id="run_demo_async_001",
            tenant=tenant,
            agent="checkout-bot",
            workflow="ap2-human-not-present",
            dry_run=True,
        ):
            raise AssertionError("dry-run body must not execute")
    except AP2DryRunResult as result:
        print(f"dry-run decision={result.decision}, reason_code={result.reason_code}")


async def run_real(client: AsyncCyclesClient, mandate: AP2Mandate, tenant: str) -> None:
    async with cycles_guard_payment_async(
        client,
        mandate=mandate,
        run_id="run_demo_async_001",
        tenant=tenant,
        agent="checkout-bot",
        workflow="ap2-human-not-present",
    ) as guard:
        print(f"decision={guard.decision}, reservation_id={guard.reservation_id}")
        psp = await fake_psp_charge_async(mandate)
        guard.attach_receipt_fields(psp_ref=psp["id"])

    if guard.receipt is not None:
        print(json.dumps(guard.receipt.model_dump(by_alias=True), indent=2))


async def main() -> None:
    config = CyclesConfig(
        base_url=os.environ.get("CYCLES_BASE_URL", "http://localhost:7878"),
        api_key=os.environ.get("CYCLES_API_KEY", "test-key"),
        tenant=os.environ.get("CYCLES_TENANT", "acme"),
    )
    mandate = AP2Mandate(
        transaction_id="ap2-tx-demo-async-001",
        amount_value="199.00",
        currency="USD",
        payee_website="merchant.example",
        checkout_hash="ch_demo_async_001",
        open_mandate_hash="omh_demo_async_001",
    )
    async with AsyncCyclesClient(config) as client:
        if os.environ.get("DRY_RUN") == "1":
            await run_dry_run(client, mandate, config.tenant or "acme")
        else:
            await run_real(client, mandate, config.tenant or "acme")


if __name__ == "__main__":
    asyncio.run(main())
