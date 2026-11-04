import asyncio
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, localcontext
from typing import Literal

import httpx
import simplejson
from pydantic import BaseModel, Field, ValidationError, field_validator


@dataclass(frozen=True)
class ProviderResult:
    status: str
    transaction_id: str | None = None
    balance: Decimal | None = None
    code: str | None = None
    executed_at: datetime | None = None
    message: str | None = None


class Approved(BaseModel):
    transactionId: str = Field(min_length=1, max_length=128)
    status: Literal["APPROVED"]
    balance: Decimal = Field(allow_inf_nan=False)
    executedAt: datetime

    @field_validator("balance")
    @classmethod
    def bounded_balance_encoding(cls, value):
        if len(value.as_tuple().digits) > 1000 or abs(value.as_tuple().exponent) > 1000:
            raise ValueError("Provider balance exceeds decimal encoding precision")
        with localcontext() as ctx:
            ctx.prec = max(28, len(value.as_tuple().digits) + abs(value.as_tuple().exponent) + 4)
            cents = value.quantize(Decimal("0.01"))
            if cents != value:
                raise ValueError("Provider MXN balance must use exact cents")
            return cents

    @field_validator("executedAt")
    @classmethod
    def timezone_required(cls, value):
        if value.tzinfo is None:
            raise ValueError("Provider timestamp must include timezone")
        return value


class Rejected(BaseModel):
    status: Literal["REJECTED"]
    code: str = Field(min_length=1, max_length=64, pattern=r"^[A-Z0-9_]+$")
    message: str = Field(max_length=1000)


class ProviderUncertain(Exception):
    def __init__(self, code, http_status):
        self.code = code
        self.http_status = http_status


class ProviderClient:
    def __init__(self, client, base_url, deadline=3):
        self.client = client
        self.deadline = deadline
        self.url = base_url.rstrip("/") + "/provider/v1/execute"

    async def execute(self, request, transaction_id):
        payload = {
            "accountId": request.account_id,
            "type": request.type.value,
            "amount": request.amount,
            "currency": request.currency,
        }
        try:
            # No retries: contract has neither idempotency nor authoritative execution lookup.
            async with asyncio.timeout(self.deadline):
                async with self.client.stream(
                    "POST",
                    self.url,
                    content=simplejson.dumps(payload, use_decimal=True),
                    headers={
                        "Content-Type": "application/json",
                        "X-Request-ID": str(transaction_id),
                    },
                ) as response:
                    content = bytearray()
                    async for chunk in response.aiter_bytes():
                        content.extend(chunk)
                        if len(content) > 65536:
                            raise ProviderUncertain("PROVIDER_INVALID_RESPONSE", 502)
                    body = simplejson.loads(bytes(content), use_decimal=True, allow_nan=False)
                    if response.status_code == 200:
                        result = Approved.model_validate(body)
                        return ProviderResult(
                            "EXECUTED",
                            result.transactionId,
                            result.balance,
                            executed_at=result.executedAt,
                        )
                    if 400 <= response.status_code <= 599:
                        result = Rejected.model_validate(body)
                        return ProviderResult("REJECTED", code=result.code, message=result.message)
                    raise ProviderUncertain("PROVIDER_INVALID_RESPONSE", 502)
        except (httpx.TimeoutException, TimeoutError) as exc:
            raise ProviderUncertain("PROVIDER_TIMEOUT", 504) from exc
        except (httpx.HTTPError, ValueError, ValidationError, TypeError) as exc:
            raise ProviderUncertain("PROVIDER_INVALID_RESPONSE", 502) from exc
