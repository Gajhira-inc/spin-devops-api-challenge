# Transaction Execution API

Python 3.12 / FastAPI solution to the SPIN backend challenge. Receive a credit/debit, validate rules, execute through a mock external provider, persist the outcome and query transactions. The provider owns account balances and locks; this API does not implement an account ledger.

## Project guide

| Document | Contents |
| --- | --- |
| [Testing guide](docs/reviewer/TESTING.md) | Unit tests, local Compose and authenticated HTTPS smoke tests, benchmarks |
| [Compose architecture](docs/reviewer/ARCHITECTURE_COMPOSE.md) | Local service boundaries, startup dependencies and persistence |
| [AWS architecture](docs/reviewer/ARCHITECTURE_AWS.md) | Optional deployed environment, identity/network paths and POC tradeoffs |
| [Benchmark tool](tools/README.md) | Configurable URL, concurrent RPS or sequential RPM, JSON/CSV reports |
| [AI use](AI_USAGE.md) | Where AI assisted and the engineering responsibility boundary |
| [OpenAPI](fastAPI/openapi.json) | Versioned API contract; interactive Swagger is available locally |

## Run locally

Prerequisites: Docker Engine with Compose v2. From this repository root:

```bash
docker compose up --build
```

Open http://localhost:8000/docs for Swagger, or `/openapi.json` for the contract. Compose starts PostgreSQL 16, applies Alembic migrations, starts the provider stub and API. Local ports bind to loopback. Local demonstration DB credentials are not production credentials. No AWS account is required. A host Python environment is only needed for tests or running outside Docker.

```bash
curl -i http://localhost:8000/transactions \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: demo-001' \
  -d '{"accountId":"demo","type":"CREDIT","amount":1500.00,"currency":"MXN","description":"Demo credit"}'
curl 'http://localhost:8000/transactions?accountId=demo&page=1&limit=20'
```

The named PostgreSQL volume persists local records between container restarts. The provider stub returns fixed fixture balances, not real account state. Alternative local mock scenarios include rejected, rejected_5xx, timeout, malformed and unavailable; see the testing guide. Existing running Compose services can be tested directly; no restart is needed for smoke or benchmark requests.

## Tests

```bash
cd fastAPI
python3.12 -m venv .venv
source .venv/bin/activate
pip install --require-hashes -r requirements-dev.txt
pytest -q
ruff check app provider_mock tests alembic
ruff format --check app provider_mock tests alembic
```

Default tests use isolated SQLite databases and in-process HTTP transports, including provider-contract, validation, persistence, pagination, idempotency and failure cases. They do not prove PostgreSQL performance. To run API tests against disposable local PostgreSQL, set `TEST_DATABASE_URL=postgresql+asyncpg://USER:PASSWORD@localhost:5432/spin_test`. **These tests drop/recreate tables**; never use production or shared demo data. The migration test independently uses temporary SQLite. No executed GitHub CI result is implied.

## Contract and rules

POST `/transactions`: accountId, type CREDIT/DEBIT, amount, currency and optional description. Amount >1, DEBIT <=10000, MXN only. CREDIT has no business cap; bounded decimal encoding/request size protects parsing. All validation precedes the provider call. Decimal parsing, PostgreSQL NUMERIC and numeric JSON preserve exact money without binary float conversion.

POST `/provider/v1/execute` is an internal mock contract: accountId, type, amount, currency. APPROVED becomes EXECUTED; structured REJECTED outcomes are persisted with a provider code/message. POST returns 201 for a new stored final outcome, including REJECTED; financial status must be inspected separately. GET `/transactions` supports accountId/status/type/page/limit; response is `{items,page,limit,hasMore}` with stable ordering. GET `/transactions/{id}` is an additional status lookup. Unknown/malformed outcomes return controlled errors with a transaction ID where available.

## Design decisions

- Python is the author's confident language. FastAPI validation/OpenAPI, HTTPX and async SQLAlchemy/asyncpg keep I/O boundaries explicit.
- Router → service → repository/provider adapter separates policy from persistence and transport. Tests inject isolated providers.
- PostgreSQL supplies durable transactions/constraints and exact NUMERIC. SQLite is test convenience, not the production concurrency model.
- Persist PENDING intent before calling the provider, then persist EXECUTED/REJECTED. No DB transaction is held across external HTTP.
- Provider calls have bounded deadlines and responses; no blind retries. A timeout/crash can leave UNKNOWN/PENDING even if the provider executed. Provider-side idempotency/status lookup and reconciliation are necessary for stronger guarantees; exactly-once financial execution is not promised.
- Optional Idempotency-Key is client-scoped: same finalized payload/key replays the stored result with 200; mismatched payload/unresolved intent returns 409. No key means independent transactions.
- Local auth is disabled by default. Optional configured bearer auth is one client, not per-account multi-user authorization. Health endpoints distinguish process startup/liveness from DB readiness; `/metrics` provides HTTP counts/duration. No measured million/day capacity is claimed.

## Running without Docker

Install dev dependencies, copy `fastAPI/.env.example` to `fastAPI/.env`, configure a disposable PostgreSQL database, run `alembic upgrade head` from `fastAPI`, then run `uvicorn provider_mock.main:app --port 8001 --no-access-log` and `uvicorn app.main:app --port 8000 --no-access-log` in separate terminals. Never commit `.env` or real credentials. Tests explicitly ignore local `.env` where appropriate.

## Optional HTTPS demonstration

The deployed POC exposes the same transaction contract at `https://spin.develop.gajhira.com/transactions`. A bearer token must be supplied separately by the author; it is not stored in Git. Availability depends on the demo environment being kept online. Interactive Swagger remains local; the public Ingress exposes transaction routes, not `/docs` or `/metrics`.

```bash
# From the repository root, with the Python environment active:
python tools/smoke_test.py --url https://spin.develop.gajhira.com --prompt-token
```

The live environment uses the mock provider, not real account balances. Its architecture is documented separately from the API implementation; infrastructure source is outside the scope of this repository's reviewer-facing files.

## Deliverables

| Challenge deliverable | Location |
| --- | --- |
| Source code in a Git repository | `fastAPI/app`, provider stub, migrations and focused tests |
| README with execution instructions and design decisions | This document; detailed walkthrough in the testing guide |
| AI-use disclosure | The section below and `AI_USAGE.md` |
| Additional materials | Docker/Compose, OpenAPI, benchmark utility and local/AWS architecture diagrams |

## AI assistance and limitations

I used OpenAI ChatGPT/Codex to assist with requirement analysis, implementation scaffolding and revisions, test cases, debugging, container packaging, the benchmark utility, architecture diagrams and documentation. The engineering decisions and responsibility remain mine; AI assistance does not substitute for understanding and explaining the delivered code. See [AI_USAGE.md](AI_USAGE.md) for scope and evidence boundaries.

Local automated checks and the recorded live smoke demonstrate specific behaviors, not audited banking readiness or sustained million-transactions/day capacity. Known POC limits include a fixture provider, a single configured bearer client and no distributed exactly-once execution guarantee.
