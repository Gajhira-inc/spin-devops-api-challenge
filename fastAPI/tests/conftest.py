import os
from contextlib import asynccontextmanager
from decimal import Decimal
from urllib.parse import urlsplit

import httpx
import pytest

from app.config import Settings
from app.database import Database
from app.main import create_app
from app.models import Base
from app.provider import ProviderResult


class FakeProvider:
    def __init__(self):
        self.calls = 0
        self.result = ProviderResult("EXECUTED", "txn-approved", Decimal("5500.00"))
        self.error = None

    async def execute(self, request, transaction_id):
        self.calls += 1
        if self.error:
            raise self.error
        return self.result


@pytest.fixture
async def harness(tmp_path):
    @asynccontextmanager
    async def start(provider=None, token=None):
        url = os.getenv("TEST_DATABASE_URL") or f"sqlite+aiosqlite:///{tmp_path}/test.db"
        if not url.startswith("sqlite") and urlsplit(
            url.replace("postgresql+asyncpg", "postgresql")
        ).hostname not in {"localhost", "127.0.0.1", "postgres"}:
            raise RuntimeError(
                "Tests require a disposable LOCAL database, never an arbitrary remote DB"
            )
        settings = Settings(
            _env_file=None,
            app_env="test",
            database_url=url,
            db_sslmode="disable",
            api_bearer_token=token,
            provider_base_url="http://provider.test",
        )
        db = Database(settings)
        async with db.engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
            await connection.run_sync(Base.metadata.create_all)
        selected = provider or FakeProvider()
        app = create_app(settings, selected, db)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                yield client, app, selected

    return start


@pytest.fixture
def body():
    return {
        "accountId": "acc-123456",
        "type": "CREDIT",
        "amount": "1500.00",
        "currency": "MXN",
        "description": "Transferencia recibida",
    }
