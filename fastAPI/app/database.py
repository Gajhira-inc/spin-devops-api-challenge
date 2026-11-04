import asyncio
import ssl

import asyncpg
from sqlalchemy import make_url, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import Transaction


class Database:
    def __init__(self, settings):
        url = settings.sqlalchemy_url()
        parsed = make_url(url)
        options = {"pool_pre_ping": True, "hide_parameters": True}
        if parsed.get_backend_name() == "postgresql":
            options.update(
                pool_size=settings.db_pool_size,
                max_overflow=settings.db_max_overflow,
                pool_timeout=settings.db_pool_timeout,
                pool_recycle=600,
            )
            tls = (
                ssl.create_default_context(cafile=settings.db_sslrootcert)
                if settings.db_sslmode == "verify-full"
                else False
            )
            if settings.db_auth_mode == "iam":
                import boto3

                rds = boto3.client("rds", region_name=settings.aws_region)

                async def connect_iam():
                    # SDK credential refresh can contact the identity agent: keep it off the event loop.
                    token = await asyncio.to_thread(
                        rds.generate_db_auth_token,
                        DBHostname=parsed.host,
                        Port=parsed.port or 5432,
                        DBUsername=parsed.username,
                        Region=settings.aws_region,
                    )
                    return await asyncpg.connect(
                        host=parsed.host,
                        port=parsed.port or 5432,
                        user=parsed.username,
                        database=parsed.database,
                        password=token,
                        ssl=tls,
                        timeout=settings.db_pool_timeout,
                    )

                options["async_creator"] = connect_iam
            else:
                options["connect_args"] = {"ssl": tls, "timeout": settings.db_pool_timeout}
        elif parsed.get_backend_name() != "sqlite" or settings.app_env == "production":
            raise ValueError("Only PostgreSQL or local/test SQLite is supported")
        self.engine = create_async_engine(url, **options)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    async def ready(self):
        async with self.engine.connect() as connection:
            # Verifies migration/table existence without scanning/counting all transactions.
            await connection.execute(select(Transaction.id).limit(1))

    async def close(self):
        await self.engine.dispose()
