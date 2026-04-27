#!/usr/bin/env python3
"""Streaming example for the Responses API.

Run the gateway locally, then:

    python examples/streaming.py
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any


BASE_URL = os.getenv("BASE_URL", "http://localhost:8000")


def _headers() -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }
    api_key = os.getenv("API_KEY")
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def stream_response(prompt: str, *, previous_response_id: str | None = None) -> str | None:
    payload: dict[str, Any] = {
        "model": "sonnet",
        "input": prompt,
        "stream": True,
    }
    if previous_response_id:
        payload["previous_response_id"] = previous_response_id

    request = urllib.request.Request(
        f"{BASE_URL}/v1/responses",
        data=json.dumps(payload).encode("utf-8"),
        headers=_headers(),
        method="POST",
    )

    final_response_id = None
    with urllib.request.urlopen(request, timeout=120) as response:
        for raw_line in response:
            line = raw_line.decode("utf-8").strip()
            if not line.startswith("data: "):
                continue
            event = json.loads(line.removeprefix("data: "))
            event_type = event.get("type")
            if event_type == "response.output_text.delta":
                print(event.get("delta", ""), end="", flush=True)
            elif event_type == "response.completed":
                final_response_id = event["response"]["id"]
            elif event_type == "response.failed":
                error = event.get("response", {}).get("error", {})
                raise RuntimeError(error.get("message", "response failed"))
    print()
    return final_response_id


def main() -> None:
    try:
        print("Streaming first response:")
        first_id = stream_response("Write a haiku about programming.")

        if first_id:
            print("\nStreaming follow-up:")
            stream_response("Now make it about debugging.", previous_response_id=first_id)

    except urllib.error.HTTPError as exc:
        print(f"HTTP {exc.code}: {exc.read().decode('utf-8')}")
    except urllib.error.URLError as exc:
        print(f"Could not reach gateway at {BASE_URL}: {exc}")


if __name__ == "__main__":
    main()
