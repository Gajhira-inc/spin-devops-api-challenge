from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models import Transaction


class TransactionRepository:
    def __init__(self, sessions):
        self.sessions = sessions

    async def prepare(self, request, client_id, key_hash, fingerprint):
        tx = Transaction(
            id=uuid4(),
            client_id=client_id,
            idempotency_key_hash=key_hash,
            request_fingerprint=fingerprint,
            account_id=request.account_id,
            type=request.type.value,
            amount=request.amount,
            currency=request.currency,
            description=request.description,
            status="PENDING",
        )
        async with self.sessions() as session:
            session.add(tx)
            try:
                await session.commit()
                return tx, True
            except IntegrityError:
                await session.rollback()
                if key_hash is None:
                    raise
                existing = await session.scalar(
                    select(Transaction).where(
                        Transaction.client_id == client_id,
                        Transaction.idempotency_key_hash == key_hash,
                    )
                )
                if existing is None:
                    raise
                return existing, False

    async def finish(
        self,
        transaction_id,
        status,
        provider_id=None,
        balance=None,
        code=None,
        executed_at=None,
        message=None,
    ):
        async with self.sessions() as session:
            tx = await session.get(Transaction, transaction_id)
            if tx is None:
                raise RuntimeError("Durable intent missing")
            tx.status = status
            tx.provider_transaction_id = provider_id
            tx.balance_after = balance
            tx.provider_code = code
            tx.provider_message = message
            tx.provider_executed_at = executed_at
            tx.updated_at = datetime.now(UTC)
            await session.commit()
            return tx

    async def get(self, transaction_id, client_id):
        async with self.sessions() as session:
            return await session.scalar(
                select(Transaction).where(
                    Transaction.id == transaction_id, Transaction.client_id == client_id
                )
            )

    async def list(self, client_id, page, limit, account_id=None, status=None, tx_type=None):
        query = select(Transaction).where(Transaction.client_id == client_id)
        for column, value in [
            (Transaction.account_id, account_id),
            (Transaction.status, status),
            (Transaction.type, tx_type),
        ]:
            if value is not None:
                query = query.where(column == value)
        query = query.order_by(Transaction.created_at.desc(), Transaction.id.desc())
        async with self.sessions() as session:
            records = list(
                (await session.scalars(query.offset((page - 1) * limit).limit(limit + 1))).all()
            )
            return records[:limit], len(records) > limit
