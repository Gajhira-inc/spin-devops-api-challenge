"""Durable transaction intent and provider results."""

import sqlalchemy as sa

from alembic import op
from app.models import ExactMoney

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "transactions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("client_id", sa.String(128), nullable=False),
        sa.Column("idempotency_key_hash", sa.String(64)),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("account_id", sa.String(128), nullable=False),
        sa.Column("type", sa.String(6), nullable=False),
        sa.Column("amount", ExactMoney(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("description", sa.String(500)),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("provider_transaction_id", sa.String(128)),
        sa.Column("balance_after", ExactMoney()),
        sa.Column("provider_code", sa.String(64)),
        sa.Column("provider_message", sa.String(1000)),
        sa.Column("provider_executed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("type IN ('CREDIT','DEBIT')", name="ck_tx_type"),
        sa.CheckConstraint("currency = 'MXN'", name="ck_tx_currency"),
        sa.CheckConstraint(
            "status IN ('PENDING','EXECUTED','REJECTED','UNKNOWN')", name="ck_tx_status"
        ),
        sa.CheckConstraint("CAST(amount AS NUMERIC) > 1", name="ck_tx_amount_min"),
        sa.CheckConstraint(
            "type != 'DEBIT' OR CAST(amount AS NUMERIC) <= 10000", name="ck_tx_debit_max"
        ),
        sa.UniqueConstraint("client_id", "idempotency_key_hash", name="uq_tx_client_key"),
    )
    op.create_index("ix_tx_created_id", "transactions", ["created_at", "id"])
    op.create_index(
        "ix_tx_client_account_created", "transactions", ["client_id", "account_id", "created_at"]
    )
    op.create_index(
        "ix_tx_client_status_type_created",
        "transactions",
        ["client_id", "status", "type", "created_at"],
    )


def downgrade():
    op.drop_table("transactions")
