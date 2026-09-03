#!/usr/bin/env python3
"""Minimal stdio JSON-RPC probe that reports its own process environment.

Used by the adapter's isolation tests to prove, at the real process boundary,
that ``AppServerTransport``/the Codex adapter strips sibling-backend secrets and
injects a per-user ``CODEX_HOME``. It answers ``initialize`` with a snapshot of
selected environment state and then lingers, draining stdin until EOF.
"""

from __future__ import annotations

import json
import os
import sys


def _write(payload: object) -> None:
    sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def main() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        if message.get("method") == "initialize" and "id" in message:
            _write(
                {
                    "id": message["id"],
                    "result": {
                        "codex_home": os.environ.get("CODEX_HOME"),
                        "has_anthropic_token": "ANTHROPIC_AUTH_TOKEN" in os.environ,
                        "marker_present": "OMG_ISOLATION_MARKER" in os.environ,
                        # A non-allowlisted, non-denylisted gateway var: present
                        # under the inheriting (denylist) mode, absent under the
                        # non-inheriting (allowlist) mode.
                        "probe_secret_present": "OMG_PROBE_SECRET" in os.environ,
                        # An allowlisted runtime essential: must survive the
                        # allowlist so the child can actually run.
                        "has_path": "PATH" in os.environ,
                        "env_key_count": len(os.environ),
                    },
                }
            )
        # Any other message (initialized, etc.) is ignored; keep draining.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
