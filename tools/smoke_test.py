"""One synthetic POST + filtered GET against a running local or HTTPS API."""

import argparse
import getpass
import os
import ssl
from uuid import UUID, uuid4

import certifi
import httpx
from spin_benchmark import endpoint, public_url


def check_api(client, url):
    account = f"smoke-{uuid4().hex}"
    payload = {
        "accountId": account,
        "type": "CREDIT",
        "amount": 2,
        "currency": "MXN",
        "description": "Synthetic reviewer smoke test",
    }
    created = client.post(url, json=payload, headers={"Idempotency-Key": str(uuid4())})
    if created.status_code != 201:
        raise RuntimeError(
            f"POST expected HTTP 201, received {created.status_code}; no retry performed"
        )
    record = created.json()
    if not isinstance(record, dict) or record.get("status") != "EXECUTED":
        raise RuntimeError("POST did not return EXECUTED; no retry performed")
    for key in (
        "id",
        "accountId",
        "type",
        "amount",
        "currency",
        "description",
        "providerTransactionId",
        "balanceAfter",
        "createdAt",
    ):
        if key not in record:
            raise RuntimeError("POST response is missing required transaction fields")
    UUID(record["id"])
    if any(
        record.get(key) != payload[key]
        for key in ("accountId", "type", "amount", "currency", "description")
    ):
        raise RuntimeError("POST did not preserve the submitted transaction fields")
    queried = client.get(url, params={"accountId": account, "page": 1, "limit": 20})
    if queried.status_code != 200:
        raise RuntimeError(f"GET expected HTTP 200, received {queried.status_code}")
    page = queried.json()
    items = page.get("items") if isinstance(page, dict) else None
    if not isinstance(items, list) or not any(
        isinstance(item, dict)
        and item.get("id") == record["id"]
        and all(
            item.get(key) == record[key]
            for key in (
                "accountId",
                "type",
                "amount",
                "currency",
                "status",
                "providerTransactionId",
                "balanceAfter",
            )
        )
        for item in items
    ):
        raise RuntimeError("GET did not return the same persisted transaction")
    return created, queried


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://localhost:8000")
    parser.add_argument("--prompt-token", action="store_true")
    parser.add_argument("--token-env", default="SPIN_API_TOKEN")
    args = parser.parse_args()
    try:
        url = endpoint(args.url)
    except ValueError as error:
        parser.error(str(error))
    token = (
        getpass.getpass("API bearer token: ")
        if args.prompt_token
        else os.environ.get(args.token_env)
    )
    if token and (not token.isascii() or any(char.isspace() for char in token)):
        parser.error("Bearer token must be ASCII without whitespace")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    context = ssl.create_default_context(cafile=certifi.where())
    print(f"Testing {public_url(url)} with one synthetic CREDIT; the stored record is retained.")
    try:
        with httpx.Client(
            verify=context, headers=headers, timeout=15, follow_redirects=False, trust_env=False
        ) as client:
            created, queried = check_api(client, url)
        print("POST", created.status_code, created.text)
        print("GET", queried.status_code, queried.text)
        print("PASS: execution and query returned the same transaction.")
        return 0
    except RuntimeError as error:
        print("FAIL:", error)
    except (ValueError, TypeError):
        print("FAIL: response JSON or transaction identifier was invalid; no retry performed")
    except httpx.HTTPError:
        print("FAIL: HTTP transport/timeout error; POST may be uncertain; no retry performed")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
