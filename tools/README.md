# SPIN Python benchmark

Run against an existing local Compose stack or the public HTTPS API. The tool never invokes Docker, Terraform, Kubernetes or cleanup commands. It does not start/stop/recreate containers. Results contain synthetic transaction/request IDs and should be reviewed before sharing.

## Setup

From the challenge repository root, use Python 3.12 in your existing API virtual environment. The tool uses HTTPX and certifi, already included in the API's hash-locked runtime dependencies.

```bash
python3 -c 'import httpx, certifi; print("Benchmark dependencies ready")'
```

If those imports fail, activate the API environment or create a private environment:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python3 -m pip install --require-hashes -r fastAPI/requirements.txt
```

## Concurrent requests per second

```bash
# 12 requests launched concurrently at the start of each second, for 60 seconds.
python3 tools/spin_benchmark.py \
  --url http://localhost:8000 \
  --rps 12 --duration 60
```

A base URL automatically targets `/transactions`. You can pass the complete endpoint instead. RPS mode sends a **batch each second**, not evenly spaced arrivals: 12 concurrent requests at t=0, another 12 at t=1, etc. The client schedules them together; network/OS dispatch is not physically instantaneous. Earlier batches may still be active when a later one starts. This is a burst-shaped workload, distinct from the existing evenly paced k6 constant-arrival test.

## Sequential requests per minute

```bash
# 12/minute: one request every five seconds, never more than one in flight.
python3 tools/spin_benchmark.py \
  --url http://localhost:8000 \
  --rpm 12 --duration 120
```

RPM mode uses start slots separated by `60 / RPM` seconds and awaits the complete response before the next request. If a response runs past the next slot, that slot is skipped and counted; there is no catch-up burst or overlapping request. `--rpm 720` therefore only achieves 12 requests/second if individual responses and the generator are fast enough. The first request starts immediately at t=0.

## Public HTTPS endpoint / authentication

```bash
python3 tools/spin_benchmark.py \
  --url https://spin.develop.gajhira.com \
  --rps 12 --duration 300 --prompt-token
```

The prompt hides your bearer token. Alternatively, supply it through the `SPIN_API_TOKEN` environment variable using your preferred secret-safe shell/password-manager workflow; do not type a token as a command argument. Change the environment-variable name with `--token-env`. If that variable is set, the token is also sent to local targets unless you unset it. Tokens are never stored in the reports.

TLS verification remains enabled using certifi. A custom trusted PEM can be selected with `--ca-bundle /path/to/ca.pem`; there is no insecure mode. Redirects, automatic retries and ambient proxy settings are disabled. Use a URL you control and intend to benchmark.

## GET / query benchmark

```bash
python3 tools/spin_benchmark.py \
  --url 'http://localhost:8000/transactions?page=1&limit=20' \
  --method GET --rpm 60 --duration 60
```

GET considers HTTP 2xx successful; it measures queries, not financial executions. POST succeeds only for **HTTP 201 plus status EXECUTED**. A stored REJECTED, UNKNOWN, PENDING or HTTP 200 replay is reported separately and does not count as a new execution.

## Request data

Default POST payload: synthetic `accountId=benchmark-<run UUID>`, CREDIT 2 MXN, description `Synthetic benchmark`. Each request has its own client request UUID and unique Idempotency-Key. These are real persisted requests to the selected service, not a dry run. The current provider must be the synthetic stub; do not point defaults at real financial accounts/providers.

To supply a different reviewed JSON object, use `--body-file /path/to/payload.json`. The same payload is sent for each request with fresh idempotency keys. Payloads, raw responses, arbitrary headers and bearer tokens are not included in output. Request/transaction UUIDs are retained for correlation. For a timeout, reconstruct the idempotency key as `benchmark-<report run_id>-<CSV index>`; do not blindly resend with a new key. No test records are automatically removed.

## Limits and interruption

- `--duration`: whole seconds, default 60, range 1–3600; the run then drains outstanding requests up to their timeout.
- `--timeout`: whole-operation deadline and per-I/O timeout, default 10 seconds, maximum 120.
- `--max-in-flight`: default 128, configurable 1–4096 for RPS. Requests exceeding the cap are counted as missed, not silently queued. RPM always has one in flight.
- Maximum 100,000 planned requests per run. No automatic warmup requests; run a separate small warmup if wanted and label it separately.
- Ctrl+C on macOS/Linux stops new requests, drains active work and saves a partial report. A second hard interruption may prevent saving. No retries: a timed-out POST may already have executed.
- Exit code 0: all planned starts completed successfully; 1: unsuccessful results or missed starts; 2: input/setup/output error; 130: interrupted. This exit status does not enforce a latency SLO or certify the target throughput.

## Results

Default location: `benchmark-results/`. Override with `--output-dir /your/private/path`.

Every run writes:

| File | Contents |
| --- | --- |
| `benchmark-<UTC>-<run>.json` | Configuration, counts, outcomes, successful throughput, p50/p95/p99/min/mean/max, timing limitations and tool hash |
| Matching `.csv` | One row per attempted request: scheduled/actual/completed offsets, scheduler lag, client latency, time to headers, HTTP/transaction status, correlation IDs and outcome |

Query strings are omitted from the report target and never logged. URL paths remain visible; keep credentials out of URLs and paths. The CSV omits skipped starts; their counts appear in JSON. Raw customer/provider payloads are never written.

Two success rates are shown: completions **within the offering window**, and all successes divided by **total elapsed time including drain**. Requests that finish after the configured window do not inflate the first rate. Inspect both rates, errors and missed starts; a configured rate is not an achieved rate. Percentiles use linear interpolation and are split into all requests versus successful requests. A tiny sample has weak tail-latency evidence.

Client latency covers request initiation through full response consumption and classification. It includes connection setup when needed; pooled HTTP/1.1 connections are reused. Time to response headers is not isolated application processing time, exact TTFB or database time. DB/provider/pool timing requires separate server instrumentation; those fields explicitly say **not measured**.

For the million/day target, check measured successful POST throughput >=12/s with agreed p95/p99 and error criteria. At 12 offered/s with some failures, successful throughput is below12; a separate 13 offered/s run can provide margin. Local runs contend with the API/DB on the same laptop; neither a short local run nor a mock-provider cloud run proves 24-hour production capacity.

## Tool verification

```bash
python3 -m unittest discover -s private-delivery/tools -p 'test_spin_benchmark.py' -q
```

Tests use HTTPX simulated responses, not the current Compose or AWS. They verify concurrent repeated batches, sequential backpressure, capacity accounting, success classification, timeouts/no retries, interruption/drain, input validation, percentiles and report redaction. The baseline has 11 simulated benchmark-tool tests; these do not run against Compose or AWS. Source behavior follows [HTTPX async clients](https://www.python-httpx.org/async/) and [timeout semantics](https://www.python-httpx.org/advanced/timeouts/).
