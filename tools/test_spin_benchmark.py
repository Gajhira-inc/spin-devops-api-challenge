import asyncio
import tempfile
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path

import httpx
from spin_benchmark import Config, arguments, endpoint, percentile, run, save


class SchedulingTests(unittest.IsolatedAsyncioTestCase):
    async def test_rps_batch_is_concurrent_and_keys_are_unique(self):
        active, peak = 0, 0
        keys = []

        async def respond(request):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            keys.append(request.headers["Idempotency-Key"])
            await asyncio.sleep(0.015)
            active -= 1
            return httpx.Response(201, json={"status": "EXECUTED"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            report, rows = await run(Config("http://test/transactions", "rps", 4, 0.06), client)
        self.assertEqual(peak, 4)
        self.assertEqual(report["observed_peak_in_flight"], 4)
        self.assertEqual(report["successful_requests"], 4)
        self.assertEqual(len(set(keys)), 4)
        self.assertEqual(len(rows), 4)

    async def test_rps_sends_repeated_batches_on_second_boundaries(self):
        starts = []

        async def respond(request):
            starts.append(asyncio.get_running_loop().time())
            await asyncio.sleep(0.005)
            return httpx.Response(201, json={"status": "EXECUTED"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            report, _ = await run(Config("http://test/transactions", "rps", 2, 1.08), client)
        self.assertEqual(len(starts), 4)
        self.assertLess(starts[1] - starts[0], 0.1)
        self.assertGreater(starts[2] - starts[0], 0.9)
        self.assertEqual(report["missed_scheduler"], 0)

    async def test_rpm_never_overlaps_and_reports_slow_missed_slots(self):
        active, peak = 0, 0

        async def respond(request):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.025)
            active -= 1
            return httpx.Response(200, json=[])

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            report, rows = await run(
                Config("http://test/transactions", "rpm", 6000, 0.09, method="GET"), client
            )
        self.assertEqual(peak, 1)
        self.assertGreater(report["missed_scheduler"], 0)
        self.assertEqual(len(rows) + report["missed_scheduler"], report["planned_requests"])

    async def test_capacity_is_bounded_and_missing_starts_counted(self):
        async def respond(request):
            await asyncio.sleep(0.01)
            return httpx.Response(201, json={"status": "EXECUTED"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            report, _ = await run(
                Config("http://test/transactions", "rps", 6, 0.05, concurrency=2), client
            )
        self.assertEqual(report["started_requests"], 2)
        self.assertEqual(report["missed_concurrency_limit"], 4)
        self.assertEqual(report["observed_peak_in_flight"], 2)

    async def test_financial_rejection_and_bad_payloads_are_not_success(self):
        cases = [
            (201, {"status": "REJECTED"}),
            (201, {"status": "UNKNOWN"}),
            (200, {"status": "EXECUTED"}),
            (201, {"status": []}),
            (503, {"error": "sensitive upstream error"}),
        ]
        for code, body in cases:
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda _, code=code, body=body: httpx.Response(code, json=body)
                )
            ) as client:
                report, _ = await run(Config("http://test/transactions", "rps", 1, 0.002), client)
            self.assertEqual(report["successful_requests"], 0, (code, body))

    async def test_timeout_has_no_retry_and_drain_is_accounted_for(self):
        calls = 0

        async def respond(request):
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.1)
            return httpx.Response(201, json={"status": "EXECUTED"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            report, rows = await run(
                Config("http://test/transactions", "rps", 1, 0.005, timeout=0.02), client
            )
        self.assertEqual(calls, 1)
        self.assertEqual(rows[0]["outcome"], "timeout")
        self.assertGreater(report["elapsed_including_drain_s"], report["configured_duration_s"])

    async def test_reports_omit_token_query_and_payload(self):
        async def respond(request):
            self.assertEqual(request.headers["Authorization"], "Bearer secret-marker")
            return httpx.Response(
                201, json={"status": "EXECUTED", "extra": "secret-response-marker"}
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            report, rows = await run(
                Config("http://test/transactions?key=secret-query", "rps", 1, 0.005),
                client,
                payload={"description": "secret-body-marker"},
                token="secret-marker",
            )
        with tempfile.TemporaryDirectory() as tmp:
            path = save(report, rows, Path(tmp))
            result = path.with_suffix(".json").read_text() + path.with_suffix(".csv").read_text()
        for secret in [
            "secret-marker",
            "secret-query",
            "secret-body-marker",
            "secret-response-marker",
        ]:
            self.assertNotIn(secret, result)
        self.assertTrue(report["query_redacted"])

    async def test_stop_stops_new_starts_and_drains_active(self):
        stop = asyncio.Event()

        async def respond(request):
            stop.set()
            await asyncio.sleep(0.01)
            return httpx.Response(201, json={"status": "EXECUTED"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            report, rows = await run(
                Config("http://test/transactions", "rpm", 60, 10), client, stop=stop
            )
        self.assertTrue(report["interrupted"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(report["not_scheduled_at_stop"], 9)


class InputTests(unittest.TestCase):
    def test_base_and_complete_urls(self):
        self.assertEqual(endpoint("http://localhost:8000"), "http://localhost:8000/transactions")
        self.assertEqual(
            endpoint("https://example.org/transactions?limit=20"),
            "https://example.org/transactions?limit=20",
        )
        with self.assertRaises(ValueError):
            endpoint("https://user:password@example.org")

    def test_percentiles_interpolate(self):
        self.assertEqual(percentile([10, 20, 30, 40], 0.95), 38.5)
        self.assertIsNone(percentile([], 0.95))

    def test_cli_rejects_conflicting_and_invalid_rates(self):
        for args in [
            ["--rps", "0"],
            ["--rpm", "nan"],
            ["--rpm", "1", "--rps", "1"],
            ["--rps", "100001"],
        ]:
            with (
                self.subTest(args=args),
                self.assertRaises(SystemExit),
                redirect_stderr(StringIO()),
            ):
                arguments(["--url", "http://localhost:8000", *args])


if __name__ == "__main__":
    unittest.main()
