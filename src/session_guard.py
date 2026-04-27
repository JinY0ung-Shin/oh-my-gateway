"""Shared session validation and state mutation for chat and responses endpoints.

Extracts the duplicated session-guard logic (lock acquisition, backend
mismatch check, first-turn tagging) into a single implementation so that
fixes apply uniformly.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncGenerator, List, Optional

from fastapi import HTTPException

from src.backends.base import ResolvedModel
from src.models import Message
from src.session_manager import Session


@dataclass
class SessionPreflight:
    """Result of session validation and state mutation."""

    session: Session
    is_new: bool
    next_turn: int  # session.turn_counter + 1
    lock_acquired: bool  # True → caller must release


async def acquire_session_preflight(
    session: Session,
    resolved: ResolvedModel,
    session_id: str,
    *,
    is_new: Optional[bool] = None,
    turn: Optional[int] = None,
    messages: Optional[List[Message]] = None,
    workspace: Optional[str] = None,
) -> SessionPreflight:
    """Acquire the session lock and run all validation guards.

    Parameters
    ----------
    session : Session
        The session object to validate against.
    resolved : ResolvedModel
        The resolved model/backend from the request.
    session_id : str
        The gateway session identifier used for stale/future-turn validation.
    is_new : bool, optional
        Whether this is a new session.  When ``None`` (default), computed
        as ``len(session.messages) == 0`` inside the lock.
    turn : int, optional
        Turn number from ``previous_response_id`` (responses flow only).
        When provided, stale/future-turn validation is performed.
    messages : list[Message], optional
        Messages to commit to the session inside the lock (chat flow).

    Returns
    -------
    SessionPreflight
        Validated result with computed ``resume_id``, ``next_turn``, etc.

    Raises
    ------
    HTTPException
        400 for backend mismatch, 409 for stale turn, 404 for future turn.
        The lock is always released before raising.
    """
    await session.lock.acquire()

    try:
        # Determine is_new inside the lock (TOCTOU-safe)
        if is_new is None:
            is_new = len(session.messages) == 0

        # --- Responses-only: turn counter validation ---
        if turn is not None and not is_new:
            if turn != session.turn_counter:
                if turn < session.turn_counter:
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            f"Stale previous_response_id: only the latest response "
                            f"(resp_{session_id}_{session.turn_counter}) can be continued"
                        ),
                    )
                else:
                    raise HTTPException(
                        status_code=404,
                        detail="previous_response_id references a future turn",
                    )

        # --- Backend mismatch guard ---
        if not is_new and session.backend and session.backend != resolved.backend:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Session '{session_id}' belongs to backend '{session.backend}', "
                    f"but model '{resolved.public_model}' resolves to '{resolved.backend}'. "
                    f"Cannot mix backends within a session."
                ),
            )

        # --- Compute next_turn ---
        # Always computed so callers (e.g. responses flow) can use it.
        next_turn: int = session.turn_counter + 1

        # --- First-turn tagging ---
        if is_new:
            session.backend = resolved.backend
            from src.system_prompt import get_system_prompt, resolve_request_placeholders

            base = get_system_prompt()
            if workspace:
                base = resolve_request_placeholders(base, workspace)
            session.base_system_prompt = base

        # --- Commit messages (chat flow) ---
        if messages is not None:
            session.add_messages(messages)

    except Exception:
        session.lock.release()
        raise

    return SessionPreflight(
        session=session,
        is_new=is_new,
        next_turn=next_turn,
        lock_acquired=True,
    )


@asynccontextmanager
async def session_preflight_scope(
    session: Session,
    resolved: ResolvedModel,
    session_id: str,
    *,
    is_new: Optional[bool] = None,
    turn: Optional[int] = None,
    messages: Optional[List[Message]] = None,
    workspace: Optional[str] = None,
) -> AsyncGenerator[SessionPreflight, None]:
    """Context manager wrapper around :func:`acquire_session_preflight`.

    Automatically releases the session lock on exit — suitable for
    non-streaming paths that use ``async with``.
    """
    preflight = await acquire_session_preflight(
        session,
        resolved,
        session_id,
        is_new=is_new,
        turn=turn,
        messages=messages,
        workspace=workspace,
    )
    try:
        yield preflight
    finally:
        if preflight.lock_acquired:
            preflight.session.lock.release()
