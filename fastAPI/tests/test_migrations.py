import asyncio
import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy import text

from app.config import Settings
from app.database import Database


async def test_versioned_migration_creates_schema(tmp_path):
    uri = f"sqlite+aiosqlite:///{tmp_path}/migration.db"
    env = os.environ | {"APP_ENV": "test", "DATABASE_URL": uri, "DB_SSLMODE": "disable"}
    result = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=Path(__file__).parents[1],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    database = Database(
        Settings(_env_file=None, app_env="test", database_url=uri, db_sslmode="disable")
    )
    try:
        await database.ready()
        async with database.engine.connect() as connection:
            assert (
                await connection.scalar(text("SELECT version_num FROM alembic_version")) == "0001"
            )
        second = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=Path(__file__).parents[1],
            env=env,
            capture_output=True,
            text=True,
        )
        assert second.returncode == 0  # Repeating an upgrade must be safe.
    finally:
        await database.close()
