# Use of artificial intelligence

OpenAI ChatGPT/Codex was used while developing this challenge. The brief permits AI use and asks the author to disclose it and be able to explain the delivered implementation.

## Where AI assisted

- Interpreting the brief and discussing Python/FastAPI, persistence and provider boundaries.
- Drafting and iterating on API layers, exact-money handling, validation, provider adapter, persistence and migrations.
- Drafting unit/in-process integration tests for business rules, provider responses, idempotency and uncertain failure windows.
- Investigating development/deployment errors and proposing fixes.
- Creating the local Docker/Compose setup, benchmark and smoke-test utilities, architecture diagrams and documentation.

AI also assisted with an optional AWS demonstration. That infrastructure implementation is outside the API source files shared for this challenge; the high-level architecture is included to explain the demonstration.

## Engineering responsibility

Language choice follows my Python familiarity. The provider remains responsible for account balances and locks. Engineering tradeoffs, code review, credential handling and explaining the behavior remain my responsibility. Generated code and prose are review material, not evidence of correctness by themselves.

Automated checks are described with their actual scope: default API tests use temporary SQLite databases and simulated/in-process provider calls; benchmark tests use simulated HTTP responses. A live POST/GET smoke verifies only that demonstrated path. A new cloud benchmark, database-stage timing, production authorization, failover or sustained million/day performance is not implied by those checks.

The README documents intended behavior, design decisions and known limitations so that the implementation can be questioned and explained during technical review. No credentials, secret values or raw private operational logs are included.
