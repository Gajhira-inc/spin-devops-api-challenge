"""Configurable HTTP provider stub, never a balance/locking implementation."""

import asyncio
import os
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.responses import Response

from app.json_codec import MoneyJSONResponse

app = FastAPI(title="Private transaction provider stub")


@app.get("/health/live")
async def healthy():
    return {"status": "ok"}


@app.post("/provider/v1/execute")
async def execute(request: Request):
    # Scenario controlled by trusted deployment environment, not a public request header.
    scenario = os.getenv("MOCK_SCENARIO", "approved")
    delay = float(os.getenv("MOCK_DELAY_SECONDS", "0"))
    if delay:
        await asyncio.sleep(min(delay, 60))
    if scenario == "timeout":
        await asyncio.sleep(60)
    if scenario == "malformed":
        return Response("not-json", status_code=200)
    if scenario == "unavailable":
        return Response("upstream unavailable", status_code=503)
    if scenario in {"rejected", "rejected_5xx"}:
        return MoneyJSONResponse(
            {
                "status": "REJECTED",
                "code": "INSUFFICIENT_FUNDS",
                "message": "The account does not have enough balance to complete the transaction",
            },
            status_code=500 if scenario == "rejected_5xx" else 422,
        )
    return MoneyJSONResponse(
        {
            "transactionId": f"txn-{uuid4()}",
            "status": "APPROVED",
            "balance": Decimal(os.getenv("MOCK_BALANCE", "5500.00")),
            "executedAt": datetime.now(UTC),
        }
    )
