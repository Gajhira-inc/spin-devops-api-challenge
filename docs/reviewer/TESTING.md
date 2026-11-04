# Testing the transaction API

Use synthetic data. POST requests create persisted transaction records. The provider supplied for this challenge is a configurable fixture stub; it does not maintain real balances or locks.

## 1. Python environment and automated tests

From the repository root:

```bash
python3.12 -m venv fastAPI/.venv
source fastAPI/.venv/bin/activate
python -m pip install --require-hashes -r fastAPI/requirements-dev.txt
cd fastAPI
pytest -q
ruff check app provider_mock tests alembic
ruff format --check app provider_mock tests alembic
cd ..
python -m unittest discover -s tools -p 'test_*.py' -q
```

Default API tests use temporary SQLite and isolated HTTP transports. They cover validation before provider execution, exact money, successful/rejected outcomes, pagination, idempotency, concurrent duplicates, timeouts and persistence failure windows. Tool tests cover scheduling, latency calculations, correlation and response classification. They do not require Compose, AWS credentials or a public token.

The migration test independently uses temporary SQLite. To test the API fixtures against real PostgreSQL, use an explicitly disposable **local** database named `spin_test` and set `TEST_DATABASE_URL=postgresql+asyncpg://USER:PASSWORD@localhost:5432/spin_test` in your local environment. The fixtures drop/recreate tables. Do not point them at the Compose demo database `spin`, shared data or Aurora. Keep credentials out of Git and command history.

Recorded baseline: 43 API tests, 11 benchmark-tool tests and 3 smoke-tool tests passed locally on 4 November 2026. Fresh checkout results should be checked independently. No GitHub CI run or PostgreSQL load-test result is implied.

## 2. Existing local Docker Compose system

With Docker Engine and Compose v2 available, a new local setup starts with:

```bash
docker compose up --build
```

If Compose is already running, leave it running and use:

```bash
docker compose ps
python tools/smoke_test.py --url http://localhost:8000
```

The smoke test sends one synthetic CREDIT, checks POST 201 / EXECUTED and required returned data, then verifies GET returns that same transaction. It does not restart containers, modify mock scenarios or erase data. Local auth is normally disabled; unset an unrelated `SPIN_API_TOKEN` before local testing if appropriate, or use the correct configured token.

For manual exploration open `http://localhost:8000/docs`. The contract is also available at `/openapi.json`. Local readiness is `/health/ready`; it checks database/schema availability. PostgreSQL/migrations must be ready before the API.

To exercise rejection locally, deliberately recreate only the fixture provider with another scenario (this is optional and changes the running mock):

```bash
MOCK_SCENARIO=rejected docker compose up -d --force-recreate provider
python tools/smoke_test.py --url http://localhost:8000
# The approval smoke should now fail because REJECTED is not EXECUTED.
# Restore the normal fixture before further success benchmarks:
MOCK_SCENARIO=approved docker compose up -d --force-recreate provider
```

The API persists a known rejection as a created record: HTTP 201 alone does not mean financial approval. Unit tests verify rejection persistence and no-call validation; Swagger can demonstrate malformed amount/currency/DEBIT inputs. Other mock scenarios are documented in the root README.

## 3. Authenticated HTTPS demonstration

The environment must still be online and a reviewer token must be supplied separately by the author. Neither AWS CLI access nor database credentials are needed for API testing.

```bash
python tools/smoke_test.py \
  --url https://spin.develop.gajhira.com --prompt-token
```

The token is read with a hidden prompt. Alternatively, read it from `SPIN_API_TOKEN`; never commit a real token or put its literal value in a command. TLS hostname/chain checks remain enabled with certifi, including on macOS Python installations without default roots. A request without valid auth should receive 401; an auth failure does not authorize retries of uncertain POSTs.

Public routes are `/transactions` and `/transactions/{id}`. Public `/docs`, `/health/*` and `/metrics` are not exposed by the transaction Ingress; their absence is intentional. Cloud POST/GET uses the same API contract with the private fixture provider. A 5500 balance in the response is a fixture, not proof of balance calculation.

## 4. Benchmark the selected system

```bash
# Concurrent batches: 12 new transaction requests together each second.
python tools/spin_benchmark.py --url http://localhost:8000 --rps 12 --duration 60

# Sequential: 12 requests/minute; at most one in flight.
python tools/spin_benchmark.py --url http://localhost:8000 --rpm 12 --duration 60

# Cloud example: start with a small bounded run.
python tools/spin_benchmark.py \
  --url https://spin.develop.gajhira.com --rps 1 --duration 10 --prompt-token

# Queries instead of financial executions:
python tools/spin_benchmark.py \
  --url 'http://localhost:8000/transactions?page=1&limit=20' \
  --method GET --rpm 60 --duration 60
```

See [the benchmark guide](../../tools/README.md) for concurrency limits, custom payloads, interruption and JSON/CSV reports. Results default to Git-ignored `benchmark-results/`. POST writes remain in the selected database; the utility does not clean them up. Use fresh idempotency keys for new synthetic transactions; never blindly retry an uncertain financial operation with a new key.

Report actual EXECUTED completions/sec, p50/p95/p99, errors, missed starts, duration and workload shape. The client cannot isolate database/provider time without server instrumentation. Twelve successful executions/sec sustained extrapolates to 1,036,800/day; a brief mock-provider run is not a 24-hour production capacity guarantee. RPS uses concurrent second-boundary batches; it differs from evenly spaced arrivals.
