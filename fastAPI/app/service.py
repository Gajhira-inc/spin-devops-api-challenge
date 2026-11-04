import hashlib

import simplejson
from sqlalchemy.exc import SQLAlchemyError

from app.provider import ProviderUncertain


class ApiError(Exception):
    def __init__(self, status_code, code, message, transaction_id=None):
        self.status_code = status_code
        self.code = code
        self.message = message
        self.transaction_id = transaction_id


class TransactionService:
    def __init__(self, repository, provider):
        self.repository = repository
        self.provider = provider

    async def execute(self, request, client_id, key):
        canonical = request.model_dump(by_alias=True, mode="python")
        fingerprint = hashlib.sha256(
            simplejson.dumps(canonical, use_decimal=True, sort_keys=True).encode()
        ).hexdigest()
        key_hash = hashlib.sha256(key.encode()).hexdigest() if key else None
        try:
            tx, created = await self.repository.prepare(request, client_id, key_hash, fingerprint)
        except SQLAlchemyError as exc:
            raise ApiError(
                503, "PERSISTENCE_UNAVAILABLE", "Cannot persist transaction intent"
            ) from exc
        if not created:
            if tx.request_fingerprint != fingerprint:
                raise ApiError(
                    409,
                    "IDEMPOTENCY_CONFLICT",
                    "Key was already used for a different request",
                    tx.id,
                )
            if tx.status == "PENDING":
                raise ApiError(
                    409,
                    "EXECUTION_UNRESOLVED",
                    "Execution is in progress or requires reconciliation; do not retry with a new key",
                    tx.id,
                )
            return tx, False
        try:
            result = await self.provider.execute(request, tx.id)
        except ProviderUncertain as exc:
            try:
                await self.repository.finish(tx.id, "UNKNOWN", code=exc.code)
            except SQLAlchemyError:
                # Durable PENDING intent still identifies an unresolved operation.
                pass
            raise ApiError(
                exc.http_status,
                exc.code,
                "Provider outcome is uncertain; query this transaction and reconcile before replay",
                tx.id,
            ) from exc
        try:
            tx = await self.repository.finish(
                tx.id,
                result.status,
                result.transaction_id,
                result.balance,
                result.code,
                result.executed_at,
                result.message,
            )
        except SQLAlchemyError as exc:
            raise ApiError(
                503,
                "RESULT_NOT_PERSISTED",
                "Provider returned an outcome but persistence failed; do not execute again",
                tx.id,
            ) from exc
        return tx, True
