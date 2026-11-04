import asyncio
import logging
import secrets
import time
from contextlib import asynccontextmanager
from uuid import UUID, uuid4

import httpx
from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from prometheus_client import CollectorRegistry, Counter, Histogram, generate_latest
from sqlalchemy.exc import SQLAlchemyError
from starlette.responses import Response

from app.config import Settings
from app.database import Database
from app.json_codec import DecimalRoute, MoneyJSONResponse
from app.provider import ProviderClient
from app.repository import TransactionRepository
from app.schemas import (
    ErrorResponse,
    TransactionPage,
    TransactionRequest,
    TransactionStatus,
    TransactionType,
    TransactionView,
)
from app.service import ApiError, TransactionService

logger = logging.getLogger("spin.requests")
bearer = HTTPBearer(auto_error=False)


async def authenticate(
    request: Request, credential: HTTPAuthorizationCredentials | None = Depends(bearer)
):
    expected = request.app.state.settings.api_bearer_token
    if expected is not None and (
        credential is None
        or not secrets.compare_digest(credential.credentials, expected.get_secret_value())
    ):
        raise HTTPException(
            401, "Valid bearer credentials required", headers={"WWW-Authenticate": "Bearer"}
        )
    # One configured client for this POC. Multi-client account entitlement policy is a separate extension.
    return request.app.state.settings.client_id


async def bounded_request(request: Request):
    try:
        await asyncio.wait_for(request.app.state.semaphore.acquire(), timeout=0.05)
    except TimeoutError as exc:
        raise ApiError(
            503, "CAPACITY_EXCEEDED", "Service is busy; no transaction was executed"
        ) from exc
    try:
        yield
    finally:
        request.app.state.semaphore.release()


def create_app(settings=None, provider=None, database=None):
    settings = settings or Settings()
    settings.validate_api_startup()

    @asynccontextmanager
    async def lifespan(app):
        db = database or Database(settings)
        client = httpx.AsyncClient(
            timeout=httpx.Timeout(
                settings.provider_timeout, connect=settings.provider_connect_timeout
            ),
            limits=httpx.Limits(max_connections=64, max_keepalive_connections=16),
            follow_redirects=False,
            trust_env=False,
        )
        app.state.settings = settings
        app.state.database = db
        app.state.repository = TransactionRepository(db.sessions)
        app.state.service = TransactionService(
            app.state.repository,
            provider
            or ProviderClient(client, settings.provider_base_url, settings.provider_timeout),
        )
        app.state.semaphore = asyncio.Semaphore(settings.max_request_concurrency)
        try:
            yield
        finally:
            await client.aclose()
            await db.close()

    app = FastAPI(
        title="Spin Transaction Execution API",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs" if settings.docs_enabled else None,
        redoc_url="/redoc" if settings.docs_enabled else None,
        openapi_url="/openapi.json" if settings.docs_enabled else None,
        default_response_class=MoneyJSONResponse,
    )
    registry = CollectorRegistry()
    requests = Counter(
        "spin_http_requests_total",
        "HTTP requests by bounded route labels",
        ["method", "route", "status_code"],
        registry=registry,
    )
    latency = Histogram(
        "spin_http_request_duration_seconds",
        "HTTP duration",
        ["method", "route"],
        registry=registry,
    )

    @app.middleware("http")
    async def telemetry(request, call_next):
        started = time.monotonic()
        supplied = request.headers.get("X-Request-ID", "")
        try:
            request.state.request_id = str(UUID(supplied))
        except ValueError:
            request.state.request_id = str(uuid4())
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        route = getattr(request.scope.get("route"), "path", "unmatched")
        elapsed = time.monotonic() - started
        requests.labels(
            request.method if request.method in {"GET", "POST", "HEAD", "OPTIONS"} else "OTHER",
            route,
            str(response.status_code),
        ).inc()
        latency.labels(
            request.method if request.method in {"GET", "POST", "HEAD", "OPTIONS"} else "OTHER",
            route,
        ).observe(elapsed)
        # No payloads, account IDs, query strings, tokens or raw provider/SQL exception text.
        logger.info(
            "request_complete request_id=%s method=%s route=%s status=%s duration_ms=%.2f",
            request.state.request_id,
            request.method,
            route,
            response.status_code,
            elapsed * 1000,
        )
        return response

    def error_body(request, code, message, transaction_id=None):
        error = {"code": code, "message": message}
        if transaction_id:
            error["transactionId"] = str(transaction_id)
        return {"error": error, "requestId": getattr(request.state, "request_id", None)}

    @app.exception_handler(ApiError)
    async def api_error(request, exc):
        return MoneyJSONResponse(
            error_body(request, exc.code, exc.message, exc.transaction_id),
            status_code=exc.status_code,
        )

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        return MoneyJSONResponse(
            error_body(request, "HTTP_ERROR", str(exc.detail)),
            status_code=exc.status_code,
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        body = error_body(request, "VALIDATION_ERROR", "Request does not satisfy the API contract")
        body["error"]["fields"] = [
            {"location": list(e["loc"]), "code": e["type"], "message": e["msg"]}
            for e in exc.errors()
        ]
        return MoneyJSONResponse(body, status_code=422)

    @app.exception_handler(SQLAlchemyError)
    async def database_error(request, exc):
        return MoneyJSONResponse(
            error_body(request, "PERSISTENCE_UNAVAILABLE", "Database is unavailable"),
            status_code=503,
        )

    router = APIRouter(
        route_class=DecimalRoute,
        dependencies=[Depends(bounded_request)],
        responses={code: {"model": ErrorResponse} for code in [400, 401, 404, 413, 422, 503]},
    )

    @router.post(
        "/transactions",
        response_model=TransactionView,
        status_code=201,
        responses={
            200: {"model": TransactionView, "description": "Idempotent replay of stored outcome"},
            409: {"model": ErrorResponse, "description": "Conflicting key or unresolved execution"},
            422: {"model": ErrorResponse, "description": "Invalid input; provider not called"},
            502: {"model": ErrorResponse, "description": "Uncertain provider response"},
            503: {"model": ErrorResponse, "description": "Persistence or capacity failure"},
            504: {"model": ErrorResponse, "description": "Provider timeout; outcome uncertain"},
        },
    )
    async def execute(
        body: TransactionRequest,
        request: Request,
        client_id=Depends(authenticate),
        idempotency_key: str | None = Header(
            default=None, alias="Idempotency-Key", min_length=1, max_length=128
        ),
    ):
        tx, created = await request.app.state.service.execute(body, client_id, idempotency_key)
        return MoneyJSONResponse(
            TransactionView.from_record(tx).model_dump(by_alias=True),
            status_code=201 if created else 200,
            headers={"Location": f"/transactions/{tx.id}"},
        )

    @router.get("/transactions", response_model=TransactionPage)
    async def list_transactions(
        request: Request,
        client_id=Depends(authenticate),
        account_id: str | None = Query(default=None, alias="accountId", max_length=128),
        status: TransactionStatus | None = Query(default=None),
        tx_type: TransactionType | None = Query(default=None, alias="type"),
        page: int = Query(default=1, ge=1, le=10000),
        limit: int = Query(default=20, ge=1, le=100),
    ):
        items, more = await request.app.state.repository.list(
            client_id, page, limit, account_id, status, tx_type
        )
        result = TransactionPage(
            items=[TransactionView.from_record(x) for x in items],
            page=page,
            limit=limit,
            hasMore=more,
        )
        return MoneyJSONResponse(result.model_dump(by_alias=True))

    @router.get("/transactions/{transaction_id}", response_model=TransactionView)
    async def get_transaction(
        transaction_id: UUID, request: Request, client_id=Depends(authenticate)
    ):
        tx = await request.app.state.repository.get(transaction_id, client_id)
        if tx is None:
            raise HTTPException(404, "Transaction not found")
        return MoneyJSONResponse(TransactionView.from_record(tx).model_dump(by_alias=True))

    app.include_router(router)

    @app.get("/health/live", include_in_schema=False)
    @app.get("/health/startup", include_in_schema=False)
    async def healthy():
        return {"status": "ok"}

    @app.get("/health/ready", include_in_schema=False)
    async def ready(request: Request):
        try:
            await asyncio.wait_for(request.app.state.database.ready(), timeout=2)
        except (SQLAlchemyError, TimeoutError) as exc:
            raise HTTPException(503, "Database or schema is not ready") from exc
        return {"status": "ready"}

    @app.get("/metrics", include_in_schema=False)
    async def metrics():
        return Response(
            generate_latest(registry), media_type="text/plain; version=0.0.4; charset=utf-8"
        )

    return app


app = create_app()
