import asyncio
from decimal import Decimal

import httpx
import pytest

from app.provider import ProviderClient, ProviderUncertain
from app.schemas import TransactionRequest


@pytest.mark.parametrize(
    "status,body,expected",
    [
        (
            200,
            '{"transactionId":"txn-1","status":"APPROVED","balance":5500.01,"executedAt":"2026-11-02T10:00:00Z"}',
            "EXECUTED",
        ),
        (422, '{"status":"REJECTED","code":"INSUFFICIENT_FUNDS","message":"rejected"}', "REJECTED"),
        (500, '{"status":"REJECTED","code":"INSUFFICIENT_FUNDS","message":"rejected"}', "REJECTED"),
    ],
)
async def test_provider_contract(status, body, expected):
    calls = []

    def handler(request):
        calls.append(request)
        assert request.url.path == "/provider/v1/execute"
        assert b'"description"' not in request.content
        return httpx.Response(status, content=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await ProviderClient(client, "http://provider.test").execute(
            TransactionRequest(accountId="acc-1", type="CREDIT", amount="1500.00", currency="MXN"),
            "tx-1",
        )
    assert result.status == expected and len(calls) == 1
    if expected == "EXECUTED":
        assert result.balance == Decimal("5500.01")


@pytest.mark.parametrize(
    "status,body",
    [
        (200, "bad-json"),
        (503, "unavailable"),
        (200, '{"status":"APPROVED"}'),
        (302, "{}"),
        (
            200,
            '{"transactionId":"a","status":"APPROVED","balance":"NaN","executedAt":"2026-11-02T10:00:00Z"}',
        ),
    ],
)
async def test_bad_provider_result_is_uncertain_no_retry(status, body):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, content=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProviderUncertain) as error:
            await ProviderClient(client, "http://provider.test").execute(
                TransactionRequest(accountId="acc-1", type="CREDIT", amount=10, currency="MXN"),
                "tx-1",
            )
    assert error.value.http_status == 502 and len(calls) == 1


async def test_timeout_and_slow_stream_have_overall_deadline():
    async def handler(request):
        await asyncio.sleep(0.1)
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProviderUncertain) as error:
            await ProviderClient(client, "http://provider.test", deadline=0.01).execute(
                TransactionRequest(accountId="acc-1", type="CREDIT", amount=10, currency="MXN"),
                "tx-1",
            )
    assert error.value.http_status == 504


async def test_provider_decimal_encoding_bounded():
    body = '{"transactionId":"txn-1","status":"APPROVED","balance":"1e999999999","executedAt":"2026-11-02T10:00:00Z"}'
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=body))
    ) as client:
        with pytest.raises(ProviderUncertain):
            await ProviderClient(client, "http://provider.test").execute(
                TransactionRequest(accountId="acc-1", type="CREDIT", amount=10, currency="MXN"),
                "tx-1",
            )
