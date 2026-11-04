from datetime import datetime
from decimal import Decimal, InvalidOperation, localcontext
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, WithJsonSchema, field_validator, model_validator

Money = Annotated[Decimal, WithJsonSchema({"type": "number"}, mode="serialization")]


class TransactionType(StrEnum):
    CREDIT = "CREDIT"
    DEBIT = "DEBIT"


class TransactionStatus(StrEnum):
    PENDING = "PENDING"
    EXECUTED = "EXECUTED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"


class TransactionRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=False,
        json_schema_extra={
            "examples": [
                {
                    "accountId": "acc-123456",
                    "type": "CREDIT",
                    "amount": 1500.00,
                    "currency": "MXN",
                    "description": "Transferencia recibida",
                }
            ]
        },
    )
    account_id: str = Field(
        alias="accountId", min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$"
    )
    type: TransactionType
    amount: Decimal = Field(allow_inf_nan=False, gt=Decimal("1.00"))
    currency: Literal["MXN"]
    description: str | None = Field(default=None, max_length=500)

    @field_validator("amount", mode="before")
    @classmethod
    def reject_boolean(cls, value):
        if isinstance(value, bool):
            raise ValueError("amount must be a decimal number")
        return value

    @field_validator("amount")
    @classmethod
    def exact_cents(cls, value):
        # Technical encoding bound, not a CREDIT business maximum. Avoid huge exponents/DoS.
        if (
            len(value.as_tuple().digits) > 1000
            or value.adjusted() > 999
            or value.adjusted() < -1000
        ):
            raise ValueError("amount exceeds supported decimal encoding precision")
        try:
            with localcontext() as ctx:
                ctx.prec = max(
                    28, len(value.as_tuple().digits) + abs(value.as_tuple().exponent) + 4
                )
                cents = value.quantize(Decimal("0.01"))
                if cents != value:
                    raise ValueError("MXN amount must have at most two fractional digits")
                return cents
        except InvalidOperation as exc:
            raise ValueError("invalid decimal amount") from exc

    @model_validator(mode="after")
    def debit_maximum(self):
        if self.type == TransactionType.DEBIT and self.amount > Decimal("10000.00"):
            raise ValueError("DEBIT amount cannot exceed 10000.00 MXN")
        return self


class TransactionView(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    id: UUID
    account_id: str = Field(alias="accountId")
    type: TransactionType
    amount: Money
    currency: Literal["MXN"]
    description: str | None
    status: TransactionStatus
    provider_transaction_id: str | None = Field(alias="providerTransactionId")
    balance_after: Money | None = Field(alias="balanceAfter")
    provider_code: str | None = Field(alias="providerCode")
    created_at: datetime = Field(alias="createdAt")

    @classmethod
    def from_record(cls, tx):
        return cls(
            id=tx.id,
            account_id=tx.account_id,
            type=tx.type,
            amount=tx.amount,
            currency=tx.currency,
            description=tx.description,
            status=tx.status,
            provider_transaction_id=tx.provider_transaction_id,
            balance_after=tx.balance_after,
            provider_code=tx.provider_code,
            created_at=tx.created_at,
        )


class TransactionPage(BaseModel):
    items: list[TransactionView]
    page: int
    limit: int
    has_more: bool = Field(alias="hasMore")


class ErrorField(BaseModel):
    location: list[str | int]
    code: str
    message: str


class ErrorDetail(BaseModel):
    code: str
    message: str
    transactionId: UUID | None = None
    fields: list[ErrorField] | None = None


class ErrorResponse(BaseModel):
    error: ErrorDetail
    requestId: str | None = None
