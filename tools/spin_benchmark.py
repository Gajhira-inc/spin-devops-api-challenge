#!/usr/bin/env python3
"""Client-side transaction benchmark. Does not change Docker or AWS resources."""

from __future__ import annotations

import argparse
import asyncio
import csv
import getpass
import hashlib
import json
import math
import os
import signal
import ssl
import statistics
import time
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID, uuid4

import certifi
import httpx

VERSION = "1.0.0"
MAX_BODY = 1_048_576
TOOL_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    rank = (len(ordered) - 1) * fraction
    low = math.floor(rank)
    high = math.ceil(rank)
    return round(ordered[low] + (ordered[high] - ordered[low]) * (rank - low), 3)


def distribution(values):
    return {
        "count": len(values),
        "min": round(min(values), 3) if values else None,
        "mean": round(statistics.mean(values), 3) if values else None,
        "p50": percentile(values, 0.50),
        "p95": percentile(values, 0.95),
        "p99": percentile(values, 0.99),
        "max": round(max(values), 3) if values else None,
    }


def endpoint(value):
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ValueError("URL must start with http:// or https:// and include a host")
    if parts.username is not None or parts.password is not None or parts.fragment:
        raise ValueError("URL must not contain credentials or a fragment")
    _ = parts.port  # Validate malformed ports before network activity.
    path = parts.path if parts.path not in {"", "/"} else "/transactions"
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, ""))


def public_url(value):
    parts = urlsplit(value)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def uuid_only(value):
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        return None


@dataclass
class Config:
    url: str
    mode: str
    rate: float
    duration: float
    method: str = "POST"
    concurrency: int = 128
    timeout: float = 10

    @property
    def interval(self):
        return 1.0 if self.mode == "rps" else 60.0 / self.rate

    @property
    def batch(self):
        return int(self.rate) if self.mode == "rps" else 1

    @property
    def slots(self):
        return math.ceil(self.duration / self.interval)


async def run(config, client, payload=None, token=None, stop=None):
    """Injectable HTTP client makes scheduling/outcome tests independent of live services."""
    stop = stop or asyncio.Event()
    run_id = uuid4().hex
    account = f"benchmark-{run_id}"
    payload = (
        payload
        if payload is not None
        else {
            "accountId": account,
            "type": "CREDIT",
            "amount": 2,
            "currency": "MXN",
            "description": "Synthetic benchmark",
        }
    )
    content = json.dumps(payload, allow_nan=False).encode() if config.method == "POST" else None
    records = []
    pending = set()
    peak = 0
    active = 0
    missing_capacity = 0
    missing_schedule = 0
    start = time.perf_counter()
    deadline = start + config.duration
    started_utc = datetime.now(UTC).isoformat()

    async def sleep_until(target):
        remaining = target - time.perf_counter()
        if remaining > 0:
            try:
                await asyncio.wait_for(stop.wait(), timeout=remaining)
            except TimeoutError:
                pass

    async def request(index, scheduled):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        began = time.perf_counter()
        row = {
            "index": index,
            "scheduled_offset_s": round(scheduled, 6),
            "start_offset_s": round(began - start, 6),
            "schedule_lag_ms": round(max(0, began - start - scheduled) * 1000, 3),
            "headers_ms": None,
            "latency_ms": None,
            "completed_offset_s": None,
            "http_status": None,
            "transaction_status": None,
            "transaction_id": None,
            "request_id": str(uuid4()),
            "response_bytes": 0,
            "success": False,
            "outcome": "unclassified",
        }
        headers = {"Accept": "application/json", "X-Request-ID": row["request_id"]}
        if config.method == "POST":
            headers["Content-Type"] = "application/json"
            headers["Idempotency-Key"] = f"benchmark-{run_id}-{index}"
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            # Whole-operation deadline supplements HTTPX's per-I/O inactivity timeouts.
            async with asyncio.timeout(config.timeout):
                async with client.stream(
                    config.method, config.url, content=content, headers=headers
                ) as response:
                    row["headers_ms"] = round((time.perf_counter() - began) * 1000, 3)
                    row["http_status"] = response.status_code
                    row["request_id"] = (
                        uuid_only(response.headers.get("X-Request-ID")) or row["request_id"]
                    )
                    chunks = bytearray()
                    async for chunk in response.aiter_bytes():
                        row["response_bytes"] += len(chunk)
                        if len(chunks) + len(chunk) > MAX_BODY:
                            row["outcome"] = "response_too_large"
                            break
                        chunks.extend(chunk)
                    else:
                        ok_http = 200 <= response.status_code < 300
                        if config.method == "GET":
                            row["success"] = ok_http
                            row["outcome"] = "http_success" if ok_http else "http_error"
                        else:
                            try:
                                body = json.loads(chunks)
                            except (ValueError, UnicodeError):
                                body = None
                            if isinstance(body, dict):
                                state = body.get("status")
                                if isinstance(state, str) and state in {
                                    "EXECUTED",
                                    "REJECTED",
                                    "UNKNOWN",
                                    "PENDING",
                                }:
                                    row["transaction_status"] = state
                                error = body.get("error")
                                error = error if isinstance(error, dict) else {}
                                row["transaction_id"] = uuid_only(
                                    body.get("id") or error.get("transactionId")
                                )
                            row["success"] = (
                                response.status_code == 201
                                and row["transaction_status"] == "EXECUTED"
                            )
                            if row["success"]:
                                row["outcome"] = "executed"
                            elif row["transaction_status"]:
                                row["outcome"] = row["transaction_status"].lower()
                            elif not ok_http:
                                row["outcome"] = "http_error"
                            else:
                                row["outcome"] = "unexpected_response"
        except (TimeoutError, httpx.TimeoutException):
            row["outcome"] = "timeout"
        except httpx.RequestError as error:
            cause = error
            tls = False
            for _ in range(8):
                if isinstance(cause, ssl.SSLCertVerificationError):
                    tls = True
                cause = getattr(cause, "__cause__", None)
                if cause is None:
                    break
            row["outcome"] = "tls_error" if tls else "transport_error"
        ended = time.perf_counter()
        row["latency_ms"] = round((ended - began) * 1000, 3)
        row["completed_offset_s"] = round(ended - start, 6)
        records.append(row)
        active -= 1

    tick = 0
    while tick < config.slots and not stop.is_set():
        # Never catch up missed slots with an artificial burst.
        elapsed_ticks = (time.perf_counter() - start) / config.interval
        earliest = (
            math.floor(elapsed_ticks) if config.mode == "rps" else math.ceil(elapsed_ticks - 1e-8)
        )
        # The initial batch should run immediately, rather than be lost to startup overhead.
        if tick == 0:
            earliest = 0
        if earliest > tick:
            skipped = min(earliest, config.slots) - tick
            missing_schedule += skipped * config.batch
            tick += skipped
        if tick >= config.slots:
            break
        scheduled = tick * config.interval
        await sleep_until(start + scheduled)
        if stop.is_set() or time.perf_counter() >= deadline:
            break
        if config.mode == "rpm":
            await request(tick + 1, scheduled)
        else:
            # Done callbacks run on the loop; filter finished tasks before admission.
            pending = {task for task in pending if not task.done()}
            admitted = min(config.batch, max(0, config.concurrency - len(pending)))
            missing_capacity += config.batch - admitted
            for member in range(admitted):
                task = asyncio.create_task(request(tick * config.batch + member + 1, scheduled))
                pending.add(task)
        tick += 1
    unscheduled = (config.slots - tick) * config.batch
    if not stop.is_set():
        await sleep_until(deadline)
    offering_window = (
        min(config.duration, time.perf_counter() - start) if stop.is_set() else config.duration
    )
    if pending:
        await asyncio.gather(*pending)
    elapsed = time.perf_counter() - start
    successful = [row for row in records if row["success"]]
    within_window = [row for row in successful if row["completed_offset_s"] <= offering_window]
    all_latencies = [row["latency_ms"] for row in records]
    report = {
        "schema_version": 1,
        "tool_version": VERSION,
        "tool_sha256": TOOL_SHA256,
        "run_id": run_id,
        "started_utc": started_utc,
        "finished_utc": datetime.now(UTC).isoformat(),
        "target": public_url(config.url),
        "query_redacted": bool(urlsplit(config.url).query),
        "method": config.method,
        "mode": "concurrent_batches_per_second"
        if config.mode == "rps"
        else "sequential_requests_per_minute",
        "configured_rate": config.rate,
        "configured_duration_s": config.duration,
        "offering_window_s": round(offering_window, 6),
        "elapsed_including_drain_s": round(elapsed, 6),
        "max_in_flight": config.concurrency if config.mode == "rps" else 1,
        "observed_peak_in_flight": peak,
        "interrupted": stop.is_set(),
        "planned_requests": config.slots * config.batch,
        "started_requests": len(records),
        "completed_requests": len(records),
        "successful_requests": len(successful),
        "successful_completed_within_window": len(within_window),
        "failed_requests": len(records) - len(successful),
        "missed_scheduler": missing_schedule,
        "missed_concurrency_limit": missing_capacity,
        "not_scheduled_at_stop": unscheduled,
        "started_rps": round(len(records) / offering_window, 6) if offering_window else 0,
        "successful_rps_within_window": round(len(within_window) / offering_window, 6)
        if offering_window
        else 0,
        "successful_rps_including_drain": round(len(successful) / elapsed, 6) if elapsed else 0,
        "success_percent": round(100 * len(successful) / len(records), 3) if records else None,
        "http_status_counts": dict(Counter(str(row["http_status"]) for row in records)),
        "outcome_counts": dict(Counter(row["outcome"] for row in records)),
        "client_latency_ms_all": distribution(all_latencies),
        "client_latency_ms_successful": distribution([row["latency_ms"] for row in successful]),
        "time_to_response_headers_ms": distribution(
            [row["headers_ms"] for row in records if row["headers_ms"] is not None]
        ),
        "scheduler_lag_ms": distribution([row["schedule_lag_ms"] for row in records]),
        "server_application_time": "not measured; requires server instrumentation",
        "database_and_provider_time": "not measured; requires server instrumentation",
        "measurement_notes": [
            "Client wall time includes connection setup if needed, server wait and body consumption; pooled HTTP/1.1 connections are reused.",
            "Time to headers is not isolated application time or exact time to first byte.",
            "RPS mode sends concurrent batches at second boundaries; RPM mode never overlaps requests and skips missed slots.",
            "No retries or redirects; POST timeout/transport errors may represent an executed but uncertain transaction.",
            "Warmup is not excluded automatically; local results do not establish cloud capacity or 24-hour availability.",
        ],
    }
    return report, sorted(records, key=lambda row: row["index"])


def save(report, records, folder):
    stem = f"benchmark-{report['started_utc'][:19].replace(':', '').replace('-', '')}-{report['run_id'][:8]}"
    path = folder / stem
    with path.with_suffix(".json").open("x") as output:
        json.dump(report, output, indent=2, allow_nan=False)
        output.write("\n")
    with path.with_suffix(".csv").open("x", newline="") as output:
        if records:
            writer = csv.DictWriter(output, fieldnames=list(records[0]))
            writer.writeheader()
            writer.writerows(records)
        else:
            output.write("index,outcome\n")
    return path


def arguments(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    parser.add_argument(
        "--url",
        required=True,
        help="Base URL or complete endpoint; base URLs default to /transactions",
    )
    rates = parser.add_mutually_exclusive_group(required=True)
    rates.add_argument("--rps", type=int, help="Concurrent batch size launched each second")
    rates.add_argument(
        "--rpm", type=float, help="Sequential start slots per minute; never overlaps"
    )
    parser.add_argument(
        "--duration",
        type=int,
        default=60,
        help="Offering duration in seconds, followed by bounded drain",
    )
    parser.add_argument("--method", choices=["POST", "GET"], default="POST")
    parser.add_argument(
        "--max-in-flight",
        type=int,
        default=128,
        help="RPS concurrency cap; excess starts are counted as missed",
    )
    parser.add_argument(
        "--timeout", type=float, default=10, help="Whole-request and per-I/O timeout, seconds"
    )
    parser.add_argument(
        "--body-file",
        type=Path,
        help="Optional JSON object for POST; contents never written into results",
    )
    parser.add_argument(
        "--token-env", default="SPIN_API_TOKEN", help="Environment variable containing bearer token"
    )
    parser.add_argument(
        "--prompt-token", action="store_true", help="Read token interactively without terminal echo"
    )
    parser.add_argument(
        "--ca-bundle", type=Path, help="Custom trusted CA PEM; TLS verification always enabled"
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "benchmark-results",
    )
    args = parser.parse_args(argv)
    try:
        args.url = endpoint(args.url)
    except ValueError as error:
        parser.error(str(error))
    rate = args.rps if args.rps is not None else args.rpm
    if not (0 < rate <= 100_000):
        parser.error("Rate must be finite, positive and <=100000")
    if not math.isfinite(args.duration) or not 1 <= args.duration <= 3600:
        parser.error("Duration must be 1..3600 seconds")
    if not math.isfinite(args.timeout) or not 0 < args.timeout <= 120:
        parser.error("Timeout must be >0 and <=120 seconds")
    if not 1 <= args.max_in_flight <= 4096:
        parser.error("max-in-flight must be 1..4096")
    if args.method == "GET" and args.body_file:
        parser.error("body-file is only supported with POST")
    config = Config(
        args.url,
        "rps" if args.rps is not None else "rpm",
        rate,
        args.duration,
        args.method,
        args.max_in_flight,
        args.timeout,
    )
    if not math.isfinite(config.interval) or config.interval <= 0:
        parser.error("Rate interval is outside the supported numeric range")
    if config.slots * config.batch > 100_000:
        parser.error("A run is limited to 100,000 planned requests; reduce rate/duration")
    return args, config


async def main(args, config, token, payload):
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, stop.set)
        except NotImplementedError:
            pass
    context = ssl.create_default_context(
        cafile=str(args.ca_bundle) if args.ca_bundle else certifi.where()
    )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    print(
        f"Target: {public_url(config.url)} | {config.method} | {config.mode.upper()} {config.rate:g} | {config.duration:g}s",
        flush=True,
    )
    print(
        "POST creates synthetic transactions; no retries. Ctrl+C stops new requests and drains in-flight work.",
        flush=True,
    )
    limits = httpx.Limits(
        max_connections=config.concurrency, max_keepalive_connections=config.concurrency
    )
    async with httpx.AsyncClient(
        verify=context,
        limits=limits,
        timeout=httpx.Timeout(config.timeout),
        follow_redirects=False,
        trust_env=False,
    ) as client:
        report, records = await run(config, client, payload, token, stop)
    path = save(report, records, args.output_dir)
    latencies = report["client_latency_ms_all"]
    print(
        f"Completed: {len(records)} | Success: {report['successful_requests']} | Failed: {report['failed_requests']}"
    )
    print(f"Successful RPS (within offering window): {report['successful_rps_within_window']:.3f}")
    print(f"Successful RPS (including drain): {report['successful_rps_including_drain']:.3f}")
    print(
        f"Client latency ms: p50={latencies['p50']} p95={latencies['p95']} p99={latencies['p99']} max={latencies['max']}"
    )
    print(
        f"Missed starts: scheduler={report['missed_scheduler']}, concurrency={report['missed_concurrency_limit']}, stop={report['not_scheduled_at_stop']}"
    )
    print("Outcomes: " + json.dumps(report["outcome_counts"], sort_keys=True))
    print(f"Reports: {path}.json and {path}.csv")
    if report["interrupted"]:
        return 130
    return int(
        bool(
            report["failed_requests"]
            or report["missed_scheduler"]
            or report["missed_concurrency_limit"]
            or report["not_scheduled_at_stop"]
        )
    )


def cli():
    args, config = arguments()
    token = (
        getpass.getpass("API bearer token: ")
        if args.prompt_token
        else os.environ.get(args.token_env)
    )
    if token and (not token.isascii() or any(ch.isspace() for ch in token)):
        raise SystemExit("Bearer token must be ASCII without whitespace")
    try:
        payload = None
        if args.body_file:
            if args.body_file.stat().st_size > 65536:
                raise ValueError("body-file must be <=64 KiB")
            payload = json.loads(args.body_file.read_text(), parse_constant=lambda _: None)
            if not isinstance(payload, dict):
                raise ValueError("body-file must contain a JSON object")
        return asyncio.run(main(args, config, token, payload))
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError):
        print(
            "Configuration or report I/O failed. Check body file, CA bundle and output-directory permissions; sensitive details omitted."
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(cli())
