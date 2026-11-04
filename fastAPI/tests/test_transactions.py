import asyncio
from decimal import Decimal

import pytest
import simplejson
from sqlalchemy.exc import SQLAlchemyError

from app.provider import ProviderResult, ProviderUncertain


@pytest.mark.parametrize(
    "changes",
    [
        {"amount": "1.00"},
        {"amount": "0"},
        {"amount": "-100"},
        {"amount": "1.001"},
        {"amount": "NaN"},
        {"amount": "Infinity"},
        {"amount": True},
        {"type": "DEBIT", "amount": "10000.01"},
        {"currency": "USD"},
        {"type": "TRANSFER"},
        {"accountId": ""},
        {"unexpected": "field"},
    ],
)
async def test_invalid_business_rules_never_execute(harness, body, changes):
    async with harness() as (client, app, provider):
        response = await client.post("/transactions", json=body | changes)
        assert response.status_code == 422
        assert provider.calls == 0
        assert (await client.get("/transactions")).json()["items"] == []


@pytest.mark.parametrize(
    "kind,amount", [("DEBIT", "10000.00"), ("CREDIT", "10000.01"), ("CREDIT", "1.01")]
)
async def test_valid_boundaries(harness, body, kind, amount):
    async with harness() as (client, app, provider):
        response = await client.post("/transactions", json=body | {"type": kind, "amount": amount})
        assert response.status_code == 201
        assert response.json()["status"] == "EXECUTED"
        assert provider.calls == 1


async def test_credit_has_no_business_cap_and_numeric_json_is_exact(harness):
    async with harness() as (client, app, provider):
        amount = Decimal("1234567890123456789012345678901234567890.12")
        raw = (
            '{"accountId":"acc-big","type":"CREDIT","amount":' + str(amount) + ',"currency":"MXN"}'
        )
        response = await client.post(
            "/transactions", content=raw, headers={"Content-Type": "application/json"}
        )
        assert response.status_code == 201
        assert simplejson.loads(response.text, use_decimal=True)["amount"] == amount
        stored = await client.get(response.headers["Location"])
        assert simplejson.loads(stored.text, use_decimal=True)["amount"] == amount
        assert isinstance(
            response.json()["amount"], (float, int)
        )  # Numeric wire contract, not quoted string.


async def test_rejection_persisted(harness, body):
    from conftest import FakeProvider

    provider = FakeProvider()
    provider.result = ProviderResult("REJECTED", code="INSUFFICIENT_FUNDS")
    async with harness(provider) as (client, app, _):
        response = await client.post("/transactions", json=body)
        assert response.status_code == 201
        result = response.json()
        assert result["status"] == "REJECTED" and result["providerCode"] == "INSUFFICIENT_FUNDS"
        assert result["balanceAfter"] is None and result["providerTransactionId"] is None
        assert (await client.get(response.headers["Location"])).json()["status"] == "REJECTED"


async def test_filters_pagination_and_enum_errors(harness, body):
    async with harness() as (client, app, provider):
        await client.post("/transactions", json=body)
        await client.post("/transactions", json=body | {"accountId": "acc-other", "type": "DEBIT"})
        first = (await client.get("/transactions?limit=1")).json()
        second = (await client.get("/transactions?limit=1&page=2")).json()
        assert first["hasMore"] and not second["hasMore"]
        assert first["items"][0]["id"] != second["items"][0]["id"]
        filtered = (
            await client.get("/transactions?accountId=acc-other&type=DEBIT&status=EXECUTED")
        ).json()
        assert len(filtered["items"]) == 1
        assert (await client.get("/transactions?limit=101")).status_code == 422
        assert (await client.get("/transactions?status=INVALID")).status_code == 422
        assert (await client.get("/transactions?page=0")).status_code == 422


async def test_idempotent_replay_and_conflicting_payload(harness, body):
    async with harness() as (client, app, provider):
        headers = {"Idempotency-Key": "same-operation"}
        first = await client.post("/transactions", json=body, headers=headers)
        second = await client.post("/transactions", json=body, headers=headers)
        conflict = await client.post(
            "/transactions", json=body | {"amount": "2000.00"}, headers=headers
        )
        assert first.status_code == 201 and second.status_code == 200
        assert first.json()["id"] == second.json()["id"]
        assert conflict.status_code == 409 and provider.calls == 1


async def test_concurrent_duplicate_has_one_provider_call(harness, body):
    from conftest import FakeProvider

    class BlockingProvider(FakeProvider):
        started = asyncio.Event()
        release = asyncio.Event()

        async def execute(self, request, transaction_id):
            self.calls += 1
            self.started.set()
            await self.release.wait()
            return self.result

    provider = BlockingProvider()
    async with harness(provider) as (client, app, _):
        headers = {"Idempotency-Key": "concurrent-operation"}
        first = asyncio.create_task(client.post("/transactions", json=body, headers=headers))
        await asyncio.wait_for(provider.started.wait(), 2)
        try:
            second = await client.post("/transactions", json=body, headers=headers)
            assert second.status_code == 409
        finally:
            provider.release.set()
        assert (await first).status_code == 201
        assert provider.calls == 1


async def test_timeout_is_unknown_and_not_replayed(harness, body):
    from conftest import FakeProvider

    provider = FakeProvider()
    provider.error = ProviderUncertain("PROVIDER_TIMEOUT", 504)
    async with harness(provider) as (client, app, _):
        headers = {"Idempotency-Key": "uncertain-operation"}
        first = await client.post("/transactions", json=body, headers=headers)
        assert first.status_code == 504
        txid = first.json()["error"]["transactionId"]
        assert (await client.get(f"/transactions/{txid}")).json()["status"] == "UNKNOWN"
        replay = await client.post("/transactions", json=body, headers=headers)
        assert replay.status_code == 200 and replay.json()["status"] == "UNKNOWN"
        assert provider.calls == 1


async def test_db_failure_before_provider_never_executes(harness, body, monkeypatch):
    async with harness() as (client, app, provider):

        async def fail(*args):
            raise SQLAlchemyError("private connection details")

        monkeypatch.setattr(app.state.repository, "prepare", fail)
        response = await client.post("/transactions", json=body)
        assert response.status_code == 503 and provider.calls == 0
        assert "private connection" not in response.text


async def test_persist_failure_after_approval_keeps_unresolved_intent(harness, body, monkeypatch):
    async with harness() as (client, app, provider):

        async def fail(*args):
            raise SQLAlchemyError("internal password")

        monkeypatch.setattr(app.state.repository, "finish", fail)
        response = await client.post(
            "/transactions", json=body, headers={"Idempotency-Key": "db-failure"}
        )
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "RESULT_NOT_PERSISTED"
        txid = response.json()["error"]["transactionId"]
        assert (await client.get(f"/transactions/{txid}")).json()["status"] == "PENDING"
        assert (
            await client.post("/transactions", json=body, headers={"Idempotency-Key": "db-failure"})
        ).status_code == 409
        assert provider.calls == 1 and "internal password" not in response.text


async def test_auth_health_metrics_and_body_bound(harness, body):
    token = "test-token-that-is-long-enough-for-local-tests"
    async with harness(token=token) as (client, app, provider):
        assert (await client.post("/transactions", json=body)).status_code == 401
        response = await client.post(
            "/transactions", json=body, headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 201
        assert "X-Request-ID" in response.headers
        assert (await client.get("/health/live")).status_code == 200
        assert (await client.get("/health/ready")).status_code == 200
        assert "spin_http_requests_total" in (await client.get("/metrics")).text
        too_big = await client.post(
            "/transactions", content=b"x" * 70000, headers={"Content-Type": "application/json"}
        )
        assert too_big.status_code == 413
        assert provider.calls == 1


async def test_missing_schema_readiness_but_not_liveness(harness):
    from app.models import Base

    async with harness() as (client, app, provider):
        async with app.state.database.engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
        assert (await client.get("/health/ready")).status_code == 503
        assert (await client.get("/health/live")).status_code == 200


async def test_unknown_transaction_is_not_found(harness):
    async with harness() as (client, app, _):
        assert (
            await client.get("/transactions/00000000-0000-0000-0000-000000000000")
        ).status_code == 404
