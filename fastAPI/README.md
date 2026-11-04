# Python Transaction Execution API

See the [project README](../README.md) for startup instructions, API rules, design decisions and AI-use disclosure. The [testing guide](../docs/reviewer/TESTING.md) covers isolated tests, Docker Compose, the HTTPS demonstration and benchmarks.

| Directory | Responsibility |
| --- | --- |
| `app/main.py`, `schemas.py`, `json_codec.py` | HTTP contract, validation, exact-money JSON and bounded HTTP telemetry |
| `app/service.py` | Execution orchestration, idempotency and uncertain-outcome policy |
| `app/provider.py` | External HTTP provider adapter and response interpretation |
| `app/repository.py`, `models.py`, `database.py` | Persistence, database sessions, connection and TLS settings |
| `alembic/` | Versioned schema migrations |
| `provider_mock/` | Configurable fixture provider, separate from the API process |
| `tests/` | Isolated business-rule, provider-contract, persistence and failure tests |

Swagger is available at `http://localhost:8000/docs` with the local Compose profile. The exported contract is [openapi.json](openapi.json). Unit tests do not require AWS.
