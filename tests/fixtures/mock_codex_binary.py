#!/usr/bin/env python3
"""Mock Codex CLI binary for integration tests.

Inspects argv for: exec, resume <id>, --json, --model <m>, -c <config>
Controlled via environment variables:
    MOCK_CODEX_SCENARIO   : basic|multi_item|error|turn_failed|timeout|preamble
    MOCK_CODEX_THREAD_ID  : thread ID to return (default: mock-thread-001)
    MOCK_CODEX_RESPONSE   : response text (default: Mock response)
    MOCK_CODEX_EXIT_CODE  : exit code (default: 0)
    MOCK_CODEX_DELAY      : per-event delay in seconds (default: 0)
    MOCK_CODEX_STDERR     : text to write to stderr
    MOCK_CODEX_INFO_FILE  : path to write parsed argv info (JSON) for test assertions
"""

import json
import os
import sys
import time

args = sys.argv[1:]
prompt = sys.stdin.read().strip() if not sys.stdin.isatty() else ""

# Parse relevant flags from argv
has_json = "--json" in args
model = None
instructions = None
resume_id = None
for i, arg in enumerate(args):
    if arg == "--model" and i + 1 < len(args):
        model = args[i + 1]
    if arg == "-c" and i + 1 < len(args):
        instructions = args[i + 1]
    if arg == "resume" and i + 1 < len(args):
        resume_id = args[i + 1]

# Write parsed args to file for test assertion
info_path = os.getenv("MOCK_CODEX_INFO_FILE")
if info_path:
    with open(info_path, "w") as f:
        json.dump(
            {
                "args": args,
                "prompt": prompt,
                "model": model,
                "instructions": instructions,
                "resume_id": resume_id,
                "has_json": has_json,
            },
            f,
        )

scenario = os.getenv("MOCK_CODEX_SCENARIO", "basic")
thread_id = os.getenv("MOCK_CODEX_THREAD_ID", "mock-thread-001")
delay = float(os.getenv("MOCK_CODEX_DELAY", "0"))
exit_code = int(os.getenv("MOCK_CODEX_EXIT_CODE", "0"))
response = os.getenv("MOCK_CODEX_RESPONSE", "Mock response")
stderr_text = os.getenv("MOCK_CODEX_STDERR", "")

SCENARIOS = {
    "basic": [
        {"type": "thread.started", "thread_id": thread_id},
        {"type": "turn.started"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": response}},
        {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 5}},
    ],
    "multi_item": [
        {"type": "thread.started", "thread_id": thread_id},
        {"type": "turn.started"},
        {"type": "item.completed", "item": {"type": "reasoning", "text": "Let me think..."}},
        {
            "type": "item.started",
            "item": {"type": "command_execution", "command": "ls -la"},
        },
        {
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "command": "ls -la",
                "exit_code": 0,
                "output": "file1.py\nfile2.py",
            },
        },
        {
            "type": "item.completed",
            "item": {
                "type": "file_change",
                "changes": [{"kind": "update", "path": "main.py"}],
            },
        },
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": "I found the files."},
        },
        {
            "type": "turn.completed",
            "usage": {"input_tokens": 100, "output_tokens": 60},
        },
    ],
    "error": [
        {"type": "thread.started", "thread_id": thread_id},
        {"type": "error", "message": "Rate limit exceeded"},
    ],
    "turn_failed": [
        {"type": "thread.started", "thread_id": thread_id},
        {"type": "turn.started"},
        {"type": "turn.failed", "message": "Internal error"},
    ],
}

# Write stderr if requested
if stderr_text:
    print(stderr_text, file=sys.stderr)

# Handle special scenarios
if scenario == "timeout":
    # Sleep longer than any reasonable line_timeout
    time.sleep(300)
    sys.exit(0)

if scenario == "preamble":
    print("Reading prompt from stdin...")
    print("Initializing...")
    # Fall through to emit basic events
    scenario = "basic"

events = SCENARIOS.get(scenario, SCENARIOS["basic"])

for event in events:
    print(json.dumps(event), flush=True)
    if delay > 0:
        time.sleep(delay)

sys.exit(exit_code)
