import asyncio

from alembic import context
from app.config import Settings
from app.database import Database
from app.models import Base


def migrate(connection):
    context.configure(connection=connection, target_metadata=Base.metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def run():
    database = Database(Settings())
    try:
        async with database.engine.connect() as connection:
            await connection.run_sync(migrate)
    finally:
        await database.close()


if context.is_offline_mode():
    raise RuntimeError(
        "Use an actual migration connection; offline SQL generation is not configured"
    )
asyncio.run(run())
