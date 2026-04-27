#!/usr/bin/env python3
"""Multi-turn conversation example using previous_response_id."""

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


def create_response(input_text: str, previous_response_id: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"model": "sonnet", "input": input_text}
    if previous_response_id:
        payload["previous_response_id"] = previous_response_id
    return _request("POST", "/v1/responses", payload)


def output_text(response: dict[str, Any]) -> str:
    return response["output"][0]["content"][0]["text"]


def session_id_from_response_id(response_id: str) -> str:
    _prefix, session_id, _turn = response_id.split("_", 2)
    return session_id


def main() -> None:
    try:
        first = create_response("Hello. My name is Sarah and I am learning React.")
        print(f"Turn 1: {output_text(first)}")

        second = create_response(
            "What is my name and what am I learning?",
            previous_response_id=first["id"],
        )
        print(f"Turn 2: {output_text(second)}")

        third = create_response(
            "Suggest a simple project for me.",
            previous_response_id=second["id"],
        )
        print(f"Turn 3: {output_text(third)}")

        session_id = session_id_from_response_id(third["id"])
        print(f"\nSession id: {session_id}")
        print(json.dumps(_request("GET", f"/v1/sessions/{session_id}"), indent=2))

        _request("DELETE", f"/v1/sessions/{session_id}")
        print("Session deleted.")

    except urllib.error.HTTPError as exc:
        print(f"HTTP {exc.code}: {exc.read().decode('utf-8')}")
    except urllib.error.URLError as exc:
        print(f"Could not reach gateway at {BASE_URL}: {exc}")


if __name__ == "__main__":
    main()
