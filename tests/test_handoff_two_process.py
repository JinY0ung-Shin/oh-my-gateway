"""Two gateway processes sharing one ``data/`` — the hand-off contract end to end.

``docs/zero-downtime-deploy.md`` §8: each scenario counts the side effects every
turn writes, so a duplicate execution fails the test. The processes run the
real ``HandoffMiddleware`` and ``SessionManager`` ownership over a stand-in turn
(``tests/fixtures/handoff_peer_app.py``).
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

FIXTURE = Path(__file__).parent / "fixtures" / "handoff_peer_app.py"
ADMIN_KEY = "handoff-test-admin-key"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Color:
    def __init__(self, name: str, tmp: Path) -> None:
        self.name = name
        self.port = _free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        env = {
            **os.environ,
            "GATEWAY_SKIP_DOTENV": "1",
            "GATEWAY_INSTANCE": name,
            "GATEWAY_INSTANCE_URL": self.url,
            "GATEWAY_INSTANCE_DIR": str(tmp / "data" / "instances"),
            "HANDOFF_EFFECTS": str(tmp / "effects"),
            "HANDOFF_TRANSCRIPTS": str(tmp / "transcripts"),
            "ADMIN_API_KEY": ADMIN_KEY,
            "PORT": str(self.port),
            "NO_PROXY": "*",
        }
        self.proc = subprocess.Popen([sys.executable, str(FIXTURE)], env=env)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                if httpx.get(self.url + "/health", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.1)
        else:
            raise RuntimeError(f"{name} did not start")
        self.boot_id = httpx.get(self.url + "/health").json()["boot_id"]

    def post(self, body: dict, **kw) -> httpx.Response:
        return httpx.post(self.url + "/v1/responses", json=body, timeout=30, **kw)

    def drain_into(self, other: "Color") -> httpx.Response:
        return httpx.post(
            self.url + "/internal/drain",
            json={"target": other.name, "target_boot_id": other.boot_id},
            headers={"Authorization": f"Bearer {ADMIN_KEY}"},
        )

    def drain_status(self) -> dict:
        return httpx.get(self.url + "/health/drain").json()

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()


@pytest.fixture
def pair(tmp_path):
    (tmp_path / "transcripts").mkdir()
    (tmp_path / "effects").touch()
    colors = [Color("blue", tmp_path), Color("green", tmp_path)]
    yield colors[0], colors[1], tmp_path / "effects"
    for c in colors:
        c.stop()


def _effects(path: Path) -> list:
    return [line.split() for line in path.read_text().splitlines()]


def _wait(pred, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.1)
    return False


def test_idle_session_is_taken_over_without_an_error(pair):
    blue, green, effects = pair
    first = blue.post({"input": "one"}).json()
    assert first["instance"] == "blue"
    # blue still holds the idle session; the next turn arrives at green
    res = green.post({"previous_response_id": first["id"], "input": "two"})
    assert res.status_code == 200, res.text
    assert res.json()["instance"] == "green"
    assert [e[0] for e in _effects(effects)] == ["blue", "green"]


def test_busy_session_is_forwarded_to_its_owner(pair):
    blue, green, effects = pair
    first = blue.post({"input": "one"}).json()
    import threading

    long_turn = {}
    t = threading.Thread(
        target=lambda: long_turn.setdefault(
            "res", blue.post({"previous_response_id": first["id"], "input": "sleep:2"})
        )
    )
    t.start()
    assert _wait(lambda: len(_effects(effects)) == 2)
    seen = httpx.get(green.url + f"/v1/responses/{first['id']}").json()
    assert seen["instance"] == "blue" and seen["busy"] is True
    t.join()
    assert long_turn["res"].json()["instance"] == "blue"
    assert len(_effects(effects)) == 2


def test_drain_routes_new_sessions_and_released_ones_to_the_target(pair):
    blue, green, effects = pair
    first = blue.post({"input": "one"}).json()
    assert blue.drain_into(green).status_code == 200
    # nginx would still send to blue here: new sessions open on green
    fresh = blue.post({"input": "new"}).json()
    assert fresh["instance"] == "green"
    assert httpx.post(blue.url + "/admin/api/thing").json()["written_by"] == "green"
    # blue releases its idle session, then a continuation sent to blue runs on green
    assert _wait(lambda: blue.drain_status()["owned_sessions"] == 0)
    nxt = blue.post({"previous_response_id": first["id"], "input": "two"}).json()
    assert nxt["instance"] == "green"
    status = blue.drain_status()
    assert (
        status["draining"] and status["active_turns"] == 0 and status["forwarding"] == 0
    )
    assert [e[0] for e in _effects(effects)] == ["blue", "green", "green"]


def test_drain_target_is_validated(pair):
    blue, green, _ = pair
    self_target = httpx.post(
        blue.url + "/internal/drain",
        json={"target": "blue", "target_boot_id": blue.boot_id},
        headers={"Authorization": f"Bearer {ADMIN_KEY}"},
    )
    assert self_target.status_code == 400
    wrong_boot = httpx.post(
        blue.url + "/internal/drain",
        json={"target": "green", "target_boot_id": "0" * 32},
        headers={"Authorization": f"Bearer {ADMIN_KEY}"},
    )
    assert wrong_boot.status_code == 400
    assert not blue.drain_status()["draining"]
    no_auth = httpx.post(blue.url + "/internal/drain", json={})
    assert no_auth.status_code == 401


def test_rollback_keeps_sessions_the_target_opened(pair):
    blue, green, effects = pair
    assert blue.drain_into(green).status_code == 200
    on_green = blue.post({"input": "new"}).json()
    assert on_green["instance"] == "green"
    assert httpx.delete(
        blue.url + "/internal/drain", headers={"Authorization": f"Bearer {ADMIN_KEY}"}
    ).json() == {"draining": False}
    # blue serves new sessions again; green keeps (or hands back) what it opened
    assert blue.post({"input": "back"}).json()["instance"] == "blue"
    nxt = blue.post({"previous_response_id": on_green["id"], "input": "two"})
    assert nxt.status_code == 200
    assert len(_effects(effects)) == 3


def test_dead_drain_target_answers_503_and_opens_nothing_locally(pair):
    blue, green, effects = pair
    assert blue.drain_into(green).status_code == 200
    green.stop()
    res = blue.post({"input": "new"})
    assert res.status_code == 503
    assert res.json()["error"]["code"] == "drain_target_unavailable"
    assert _effects(effects) == []


def test_owner_dies_after_the_side_effect_no_resend(pair):
    """A forwarded request ran its side effect, then the process running it died
    before sending a response byte: the sender reports forward_interrupted
    (settle cannot answer → unknown) and never resends."""
    blue, green, effects = pair
    assert blue.drain_into(green).status_code == 200
    res = blue.post({"input": "die"})
    assert res.status_code == 502, res.text
    assert res.json()["error"]["code"] == "forward_interrupted"
    assert res.headers["x-should-retry"] == "false"
    assert [e[0] for e in _effects(effects)] == ["green"]


def test_forged_forward_headers_are_ignored(pair):
    blue, green, effects = pair
    res = blue.post(
        {"input": "x"},
        headers={
            "x-gateway-forwarded": "green",
            "x-gateway-dispatch-id": f"{blue.boot_id}.{int(time.time()*1000)}.{'a'*32}",
            "x-gateway-signature": "0" * 64,
        },
    )
    assert res.status_code == 200
    assert res.json()["instance"] == "blue"
    internal = httpx.post(blue.url + "/internal/dispatch/x/settle")
    assert internal.status_code == 404
