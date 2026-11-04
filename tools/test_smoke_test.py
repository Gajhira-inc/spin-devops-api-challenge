import unittest
from uuid import uuid4

import httpx
from smoke_test import check_api


class SmokeTests(unittest.TestCase):
    def test_post_and_filtered_get_return_same_transaction(self):
        stored = {}

        def respond(request):
            if request.method == "POST":
                import json

                stored.update(json.loads(request.content))
                stored.update(
                    id=str(uuid4()),
                    status="EXECUTED",
                    providerTransactionId="txn-fixture",
                    balanceAfter=5500,
                    createdAt="2026-11-04T00:00:00Z",
                )
                return httpx.Response(201, json=stored)
            self.assertEqual(request.url.params["accountId"], stored["accountId"])
            return httpx.Response(
                200, json={"items": [stored], "page": 1, "limit": 20, "hasMore": False}
            )

        with httpx.Client(transport=httpx.MockTransport(respond)) as client:
            created, queried = check_api(client, "http://test/transactions")
        self.assertEqual(created.status_code, 201)
        self.assertEqual(queried.status_code, 200)

    def test_rejected_post_is_not_success_and_is_not_retried(self):
        calls = []

        def respond(request):
            calls.append(request)
            return httpx.Response(201, json={"status": "REJECTED"})

        with (
            httpx.Client(transport=httpx.MockTransport(respond)) as client,
            self.assertRaises(RuntimeError),
        ):
            check_api(client, "http://test/transactions")
        self.assertEqual(len(calls), 1)

    def test_timeout_is_not_retried(self):
        calls = []

        def respond(request):
            calls.append(request)
            raise httpx.ReadTimeout("simulated")

        with (
            httpx.Client(transport=httpx.MockTransport(respond)) as client,
            self.assertRaises(httpx.ReadTimeout),
        ):
            check_api(client, "http://test/transactions")
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
