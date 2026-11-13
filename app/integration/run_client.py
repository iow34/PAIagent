"""Independent HTTP client: submit one durable run, poll, fetch structured output.

Uses PAIAGENT_SERVICE_KEY from the environment; never prints that secret.
"""

import argparse
import asyncio
import json
import os
from uuid import uuid4

import httpx


async def run(
    client,
    profile_id,
    version,
    *,
    query="",
    idempotency_key=None,
    poll_seconds=1,
    timeout_seconds=120,
):
    response = await client.post(
        "/v1/runs",
        json={
            "profile_id": str(profile_id),
            "input": {"version": version, "query": query},
            "idempotency_key": idempotency_key or uuid4().hex,
        },
    )
    response.raise_for_status()
    state = response.json()
    async with asyncio.timeout(timeout_seconds):
        while state["status"] in {"pending", "running"}:
            await asyncio.sleep(poll_seconds)
            response = await client.get("/v1/runs/" + state["id"])
            response.raise_for_status()
            state = response.json()
    if state["status"] != "succeeded" or not state["result_id"]:
        return {"run": state, "result": None}
    response = await client.get("/v1/results/" + state["result_id"])
    response.raise_for_status()
    return {"run": state, "result": response.json()}


async def main_async(args):
    async with httpx.AsyncClient(
        base_url=args.base_url,
        timeout=65,
        headers={
            "Authorization": "Bearer " + os.environ["PAIAGENT_SERVICE_KEY"],
            "X-User-ID": args.user_id,
        },
    ) as client:
        value = await run(
            client,
            args.profile_id,
            args.version,
            query=args.query,
            idempotency_key=args.idempotency_key,
        )
    print(json.dumps(value, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--profile-id", required=True)
    parser.add_argument("--version", required=True, type=int)
    parser.add_argument("--query", default="")
    parser.add_argument("--idempotency-key", default=None)
    args = parser.parse_args()
    if args.version < 1:
        parser.error("--version must be positive")
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
