import httpx
import pytest

from app.provider import ProviderClient
from provider_mock.main import app as stub


@pytest.mark.parametrize(
    "scenario,status,http_status",
    [
        ("approved", "EXECUTED", 201),
        ("rejected", "REJECTED", 201),
        ("unavailable", "UNKNOWN", 502),
        ("timeout", "UNKNOWN", 504),
    ],
)
async def test_actual_stub_through_api_and_persistence(
    harness, body, monkeypatch, scenario, status, http_status
):
    monkeypatch.setenv("MOCK_SCENARIO", scenario)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=stub), base_url="http://provider.test"
    ) as transport:
        provider = ProviderClient(
            transport, "http://provider.test", deadline=0.01 if scenario == "timeout" else 3
        )
        async with harness(provider) as (client, app, _):
            response = await client.post("/transactions", json=body)
            assert response.status_code == http_status
            txid = (
                response.json()["id"]
                if http_status == 201
                else response.json()["error"]["transactionId"]
            )
            assert (await client.get(f"/transactions/{txid}")).json()["status"] == status
