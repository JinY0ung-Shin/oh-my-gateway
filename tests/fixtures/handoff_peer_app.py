"""A gateway "color" for the two-process hand-off tests (tests/test_handoff_two_process.py).

Runs the real ``HandoffMiddleware`` and the real ``SessionManager`` ownership
(owner locks, admission, ``release_for_handoff``, drain) over a tiny stand-in
for ``/v1/responses``: each turn appends one line to ``$HANDOFF_EFFECTS`` (the
"tool side effect" a duplicate execution would repeat) and the transcript is a
shared file, so another color can resume the session. Input ``"sleep:N"`` keeps
the turn running N seconds; ``"die"`` writes the side effect and then kills the
process before any response byte is sent.

Usage: GATEWAY_INSTANCE=blue PORT=… python handoff_peer_app.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ.setdefault("GATEWAY_SKIP_DOTENV", "1")

import uvicorn  # noqa: E402
from fastapi import FastAPI, HTTPException, Request  # noqa: E402

from src import instance_registry  # noqa: E402
from src.handoff.middleware import HandoffMiddleware  # noqa: E402
from src.session_manager import Session, session_manager  # noqa: E402

EFFECTS = Path(os.environ["HANDOFF_EFFECTS"])
TRANSCRIPTS = Path(os.environ["HANDOFF_TRANSCRIPTS"])
ME = instance_registry.instance_name()

inner = FastAPI()


def _transcript(sid: str) -> Path:
    return TRANSCRIPTS / f"{sid}.turns"


def _session(sid: str) -> Session:
    cached = session_manager.peek_session(sid)
    if cached is not None:
        return cached
    path = _transcript(sid)
    if not path.exists():
        raise HTTPException(404, "no such session")
    rehydrated = Session(session_id=sid, turn_counter=int(path.read_text()))
    admitted = session_manager._admit_rehydrated(sid, rehydrated, None)
    if admitted is None:
        raise HTTPException(404, "no such session")
    return admitted


@inner.post("/v1/responses")
async def responses(request: Request):
    body = await request.json()
    previous = body.get("previous_response_id")
    if previous:
        sid = previous.split("_")[1]
        session = _session(sid)
    else:
        sid = str(uuid.uuid4())
        session = session_manager.get_or_create_session(sid)
    if session.lock.locked():
        raise HTTPException(409, "turn in progress")
    async with session.lock:
        session.active_response_id = "running"
        try:
            text = str(body.get("input", ""))
            with EFFECTS.open("a") as fh:
                fh.write(f"{ME} {sid} {text}\n")
            if text == "die":
                os._exit(9)
            if text.startswith("sleep:"):
                await asyncio.sleep(float(text.split(":", 1)[1]))
            session.turn_counter += 1
            _transcript(sid).write_text(str(session.turn_counter))
        finally:
            session.active_response_id = None
    return {"id": f"resp_{sid}_{session.turn_counter}", "instance": ME}


@inner.get("/v1/responses/{resp_id}")
async def get_response(resp_id: str):
    sid = resp_id.split("_")[1]
    session = session_manager.peek_session(sid)
    if session is None:
        raise HTTPException(404, "not here")
    return {"id": resp_id, "instance": ME, "busy": session.lock.locked()}


@inner.post("/admin/api/thing")
async def admin_write():
    return {"written_by": ME}


@inner.get("/health")
async def health():
    return {"status": "healthy", **instance_registry.describe()}


app = HandoffMiddleware(inner)


@inner.on_event("startup")
async def _startup() -> None:
    instance_registry.register()


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ["PORT"]), log_level="warning")
