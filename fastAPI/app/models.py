from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, Index, Numeric, String, UniqueConstraint, Uuid
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator


class Base(DeclarativeBase):
    pass


class ExactMoney(TypeDecorator):
    """Unconstrained NUMERIC on PostgreSQL; exact text in SQLite tests, never float."""

    impl = Numeric
    cache_ok = True

    def load_dialect_impl(self, dialect):
        return dialect.type_descriptor(String() if dialect.name == "sqlite" else Numeric())

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return str(value) if dialect.name == "sqlite" else value

    def process_result_value(self, value, dialect):
        return Decimal(value) if value is not None else None


class Transaction(Base):
    __tablename__ = "transactions"
    __table_args__ = (
        CheckConstraint("type IN ('CREDIT','DEBIT')", name="ck_tx_type"),
        CheckConstraint("currency = 'MXN'", name="ck_tx_currency"),
        CheckConstraint(
            "status IN ('PENDING','EXECUTED','REJECTED','UNKNOWN')", name="ck_tx_status"
        ),
        CheckConstraint("CAST(amount AS NUMERIC) > 1", name="ck_tx_amount_min"),
        CheckConstraint(
            "type != 'DEBIT' OR CAST(amount AS NUMERIC) <= 10000", name="ck_tx_debit_max"
        ),
        UniqueConstraint("client_id", "idempotency_key_hash", name="uq_tx_client_key"),
        Index("ix_tx_created_id", "created_at", "id"),
        Index("ix_tx_client_account_created", "client_id", "account_id", "created_at"),
        Index("ix_tx_client_status_type_created", "client_id", "status", "type", "created_at"),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    client_id: Mapped[str] = mapped_column(String(128))
    idempotency_key_hash: Mapped[str | None] = mapped_column(String(64))
    request_fingerprint: Mapped[str] = mapped_column(String(64))
    account_id: Mapped[str] = mapped_column(String(128))
    type: Mapped[str] = mapped_column(String(6))
    amount: Mapped[Decimal] = mapped_column(ExactMoney)
    currency: Mapped[str] = mapped_column(String(3))
    description: Mapped[str | None] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(16), default="PENDING")
    provider_transaction_id: Mapped[str | None] = mapped_column(String(128))
    balance_after: Mapped[Decimal | None] = mapped_column(ExactMoney)
    provider_code: Mapped[str | None] = mapped_column(String(64))
    provider_message: Mapped[str | None] = mapped_column(String(1000))
    provider_executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
