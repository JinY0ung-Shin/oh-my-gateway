from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

FIXTURE = Path(__file__).parents[1] / "fixtures" / "fake_app_server.py"


def _spawn(tmp_path: Path, scenario: dict):
    scenario_path = tmp_path / "scenario.json"
    scenario_path.write_text(json.dumps(scenario), encoding="utf-8")
    return subprocess.Popen(
        [sys.executable, str(FIXTURE), str(scenario_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        bufsize=1,
    )


def _send(proc: subprocess.Popen[str], payload: dict) -> None:
    assert proc.stdin is not None
    proc.stdin.write(json.dumps(payload) + "\n")
    proc.stdin.flush()


def _read(proc: subprocess.Popen[str]) -> dict:
    assert proc.stdout is not None
    return json.loads(proc.stdout.readline())


def test_fixture_can_emit_notification_before_matching_response(tmp_path):
    proc = _spawn(
        tmp_path,
        {
            "steps": [
                {
                    "expect_method": "turn/start",
                    "actions": [
                        {
                            "type": "message",
                            "message": {
                                "method": "turn/completed",
                                "params": {"turnId": "turn-1"},
                            },
                        },
                        {"type": "response", "result": {"turnId": "turn-1"}},
                    ],
                }
            ]
        },
    )
    try:
        _send(proc, {"id": "req-1", "method": "turn/start", "params": {}})
        assert _read(proc) == {
            "method": "turn/completed",
            "params": {"turnId": "turn-1"},
        }
        assert _read(proc) == {"id": "req-1", "result": {"turnId": "turn-1"}}
        assert proc.wait(timeout=3) == 0
    finally:
        if proc.poll() is None:
            proc.kill()


def test_fixture_can_exit_immediately_after_request(tmp_path):
    proc = _spawn(
        tmp_path,
        {
            "steps": [
                {
                    "expect_method": "probe",
                    "actions": [{"type": "exit", "code": 23}],
                }
            ]
        },
    )
    try:
        _send(proc, {"id": "req-2", "method": "probe"})
        assert proc.wait(timeout=3) == 23
        assert proc.stdout is not None
        assert proc.stdout.read() == ""
    finally:
        if proc.poll() is None:
            proc.kill()


def test_fixture_can_emit_malformed_line_and_stderr(tmp_path):
    proc = _spawn(
        tmp_path,
        {
            "steps": [
                {
                    "expect_method": "probe",
                    "actions": [
                        {"type": "stderr", "text": "synthetic failure context"},
                        {"type": "malformed", "text": "{not-json"},
                    ],
                }
            ]
        },
    )
    try:
        _send(proc, {"id": "req-3", "method": "probe"})
        assert proc.stdout is not None
        assert proc.stdout.readline() == "{not-json\n"
        assert proc.wait(timeout=3) == 0
        assert proc.stderr is not None
        assert "synthetic failure context" in proc.stderr.read()
    finally:
        if proc.poll() is None:
            proc.kill()
