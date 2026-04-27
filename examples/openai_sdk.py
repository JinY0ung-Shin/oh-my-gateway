#!/usr/bin/env python3
"""Responses API examples using only the Python standard library.

Run the gateway locally, then:

    python examples/openai_sdk.py

Set API_KEY when the gateway is protected by a bearer token.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any


BASE_URL = os.getenv("BASE_URL", "http://localhost:8000")


def _headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    api_key = os.getenv("API_KEY")
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _request(method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        f"{BASE_URL}{path}",
        data=data,
        headers=_headers(),
        method=method,
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        return json.loads(response.read().decode("utf-8"))


def create_response(**payload: Any) -> dict[str, Any]:
    return _request("POST", "/v1/responses", payload)


def print_output(label: str, response: dict[str, Any]) -> None:
    text = response["output"][0]["content"][0]["text"]
    print(f"\n=== {label} ===")
    print(text)
    print(f"response_id: {response['id']}")


def main() -> None:
    try:
        health = _request("GET", "/health")
        print(f"Gateway status: {health['status']}")

        basic = create_response(model="sonnet", input="What is 2 + 2?")
        print_output("Basic Response", basic)

        instructed = create_response(
            model="sonnet",
            instructions="Answer in one short sentence.",
            input="How do I read a file in Python?",
        )
        print_output("Instructions", instructed)

        first = create_response(model="sonnet", input="My name is Alice.")
        follow_up = create_response(
            model="sonnet",
            input="What is my name?",
            previous_response_id=first["id"],
        )
        print_output("Multi-turn", follow_up)

        models = _request("GET", "/v1/models")
        print("\n=== Models ===")
        for model in models["data"]:
            print(f"- {model['id']} ({model['owned_by']})")

    except urllib.error.HTTPError as exc:
        print(f"HTTP {exc.code}: {exc.read().decode('utf-8')}")
    except urllib.error.URLError as exc:
        print(f"Could not reach gateway at {BASE_URL}: {exc}")


if __name__ == "__main__":
    main()
