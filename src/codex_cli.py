"""Codex CLI subprocess wrapper.

Spawns the Codex Rust binary (``codex exec --json``) and normalizes its JSONL
output into the same internal chunk-dict format that ``streaming_utils.py``
already consumes.  This keeps all Codex-specific logic out of the streaming
layer and out of ``main.py``.

The module satisfies the ``BackendClient`` protocol defined in
``src/backend_registry.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, List, Optional

from src.constants import (
    CODEX_CLI_PATH,
    CODEX_CONFIG_ISOLATION,
    CODEX_TIMEOUT_MS,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Codex event → internal chunk-dict normalization
# ---------------------------------------------------------------------------

# Keys present in Codex ``turn.completed`` → ``usage`` that map into the
# internal usage dict consumed by ``streaming_utils.extract_sdk_usage``.
_USAGE_KEY_MAP = {
    "input_tokens": "input_tokens",
    "output_tokens": "output_tokens",
    "cached_tokens": "cache_read_input_tokens",
}


def _extract_text_from_item(item: Dict[str, Any]) -> Optional[str]:
    """Pull visible text from a Codex item payload."""
    item_type = item.get("type")
    if item_type == "agent_message":
        return item.get("text")
    if item_type == "reasoning":
        return item.get("text")
    return None


def _extract_text_and_collab(text: str) -> tuple[str, list[Dict[str, Any]]]:
    """Split agent_message text into plain text and collab_tool_call JSON objects.

    Returns (cleaned_text, list_of_parsed_collab_dicts).
    Uses string-aware brace-counting so that ``{`` / ``}`` inside JSON
    string values (e.g. ``"message": "Found {3} files"``) do not confuse
    the depth tracker.
    """
    import re

    plain_parts: list[str] = []
    collab_events: list[Dict[str, Any]] = []
    i = 0
    while i < len(text):
        if text[i] == "{":
            # String-aware brace counter
            depth = 0
            j = i
            in_string = False
            escape_next = False
            while j < len(text):
                ch = text[j]
                if escape_next:
                    escape_next = False
                elif ch == "\\" and in_string:
                    escape_next = True
                elif ch == '"':
                    in_string = not in_string
                elif not in_string:
                    if ch == "{":
                        depth += 1
                    elif ch == "}":
                        depth -= 1
                        if depth == 0:
                            break
                j += 1
            block = text[i : j + 1] if j < len(text) else text[i:]
            try:
                parsed = json.loads(block)
                if isinstance(parsed, dict) and (
                    "collab_tool_call" in parsed or parsed.get("type") == "collab_tool_call"
                ):
                    collab_events.append(parsed)
                    i = j + 1
                    continue
            except json.JSONDecodeError:
                pass
            # Non-collab JSON block or invalid JSON — keep as content
            plain_parts.append(block)
            i = j + 1
            continue
        plain_parts.append(text[i])
        i += 1

    cleaned = "".join(plain_parts)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip(), collab_events


def _unwrap_collab_event(event: Dict[str, Any]) -> Dict[str, Any]:
    """Unwrap ``{"collab_tool_call": {...}}`` wrapper to get the inner event.

    If the event is already in flat form (has a ``"tool"`` key), return as-is.
    """
    if "collab_tool_call" in event and isinstance(event["collab_tool_call"], dict):
        return event["collab_tool_call"]
    return event


def _collab_to_tool_blocks(
    collab_events: list[Dict[str, Any]],
    agent_tool_ids: Optional[Dict[str, str]] = None,
) -> list[Dict[str, Any]]:
    """Convert collab_tool_call events into tool_use / tool_result content blocks.

    Mapping:
    - spawn_agent  → tool_use (name="Agent")
    - send_input   → tool_use (name="Agent")
    - wait (with completed agent message) → tool_result
    - close_agent  → skip (cleanup only)

    *agent_tool_ids* is an optional mapping of receiver_thread_id → tool_use_id
    that persists across calls, allowing spawn → wait correlation.  When ``None``
    a local dict is created (single-call scope).
    """
    blocks: list[Dict[str, Any]] = []
    # Track spawn tool_use_ids so wait results can reference them
    if agent_tool_ids is None:
        agent_tool_ids = {}  # receiver_thread_id → tool_use_id

    for raw_event in collab_events:
        event = _unwrap_collab_event(raw_event)
        tool = event.get("tool", "")
        agents_states = event.get("agents_states", {})

        if tool == "spawn_agent":
            prompt = event.get("prompt", "")
            receivers = event.get("receiver_thread_ids", [])
            tool_id = f"codex_agent_{uuid.uuid4().hex[:12]}"
            # Track for later tool_result matching
            for rid in receivers:
                agent_tool_ids[rid] = tool_id
            blocks.append(
                {
                    "type": "tool_use",
                    "id": tool_id,
                    "name": "Agent",
                    "input": {"prompt": prompt},
                }
            )

        elif tool == "send_input":
            prompt = event.get("prompt", "")
            receivers = event.get("receiver_thread_ids", [])
            tool_id = f"codex_agent_{uuid.uuid4().hex[:12]}"
            for rid in receivers:
                agent_tool_ids[rid] = tool_id
            if prompt:
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": tool_id,
                        "name": "Agent",
                        "input": {"prompt": prompt},
                    }
                )

        elif tool == "wait":
            # Extract completed agent results
            for agent_id, state in agents_states.items():
                if state.get("status") == "completed" and state.get("message"):
                    parent_id = agent_tool_ids.get(agent_id, f"codex_agent_{uuid.uuid4().hex[:12]}")
                    blocks.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": parent_id,
                            "content": state["message"],
                            "is_error": False,
                        }
                    )

        # close_agent → skip (cleanup)

    return blocks


def _strip_collab_json(text: str) -> str:
    """Remove embedded collab_tool_call JSON blocks (backward compat wrapper)."""
    cleaned, _ = _extract_text_and_collab(text)
    return cleaned


def _build_content_blocks(item: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Convert a Codex item into internal content blocks."""
    item_type = item.get("type")

    if item_type == "agent_message":
        raw_text = item.get("text", "")
        cleaned_text, collab_events = _extract_text_and_collab(raw_text)
        blocks: List[Dict[str, Any]] = []
        # Convert collab events to tool_use/tool_result blocks
        if collab_events:
            blocks.extend(_collab_to_tool_blocks(collab_events))
        # Add remaining plain text
        if cleaned_text:
            blocks.append({"type": "text", "text": cleaned_text})
        return blocks

    if item_type == "reasoning":
        text = item.get("text", "")
        return [{"type": "thinking", "thinking": text}]

    if item_type == "command_execution":
        return [
            {
                "type": "tool_use",
                "id": f"codex_cmd_{uuid.uuid4().hex[:12]}",
                "name": "Bash",
                "input": {"command": item.get("command", "")},
            }
        ]

    if item_type == "file_change":
        changes = item.get("changes", [])
        blocks: List[Dict[str, Any]] = []
        for ch in changes:
            blocks.append(
                {
                    "type": "tool_use",
                    "id": f"codex_file_{uuid.uuid4().hex[:12]}",
                    "name": "Edit" if ch.get("kind") == "update" else "Write",
                    "input": {"path": ch.get("path", ""), "kind": ch.get("kind", "")},
                }
            )
        return blocks or [{"type": "text", "text": "[file change]"}]

    if item_type == "mcp_tool_call":
        return [
            {
                "type": "tool_use",
                "id": f"codex_mcp_{uuid.uuid4().hex[:12]}",
                "name": f"mcp_{item.get('server', '')}_{item.get('tool', '')}",
                "input": item.get("arguments", {}),
            }
        ]

    if item_type == "web_search":
        return [
            {
                "type": "tool_use",
                "id": f"codex_web_{uuid.uuid4().hex[:12]}",
                "name": "WebSearch",
                "input": {"query": item.get("query", "")},
            }
        ]

    if item_type == "error":
        return [{"type": "text", "text": f"[Codex error: {item.get('message', '')}]"}]

    # Fallback for unknown item types
    return [{"type": "text", "text": json.dumps(item, default=str)}]


def _normalize_usage(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Map Codex usage fields to the internal format expected by extract_sdk_usage."""
    return {
        "input_tokens": raw.get("input_tokens", 0),
        "output_tokens": raw.get("output_tokens", 0),
        "cache_read_input_tokens": raw.get("cached_tokens", 0),
        "cache_creation_input_tokens": 0,
    }


def normalize_codex_event(event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Convert a single Codex JSONL event into an internal chunk dict.

    Returns ``None`` for events that should not be yielded downstream
    (e.g. ``thread.started`` — the caller handles that separately).
    """
    event_type = event.get("type", "")

    # --- item.completed: the main content-carrying event ---
    if event_type == "item.completed":
        item = event.get("item", {})
        blocks = _build_content_blocks(item)
        return {"type": "assistant", "content": blocks}

    # --- item.started: early signal, useful for tool-progress ---
    if event_type == "item.started":
        item = event.get("item", {})
        item_type = item.get("type")
        # For command_execution started, emit as system task event
        if item_type == "command_execution":
            return {
                "type": "system",
                "subtype": "task_started",
                "task_id": f"codex_cmd_{uuid.uuid4().hex[:8]}",
                "description": f"Running: {item.get('command', '...')}",
                "session_id": "",
            }
        return None

    # --- item.updated: progress (e.g. todo list) ---
    if event_type == "item.updated":
        item = event.get("item", {})
        if item.get("type") == "todo_list":
            items = item.get("items", [])
            done = sum(1 for i in items if i.get("completed"))
            total = len(items)
            return {
                "type": "system",
                "subtype": "task_progress",
                "task_id": "codex_todo",
                "description": f"Todo: {done}/{total} completed",
            }
        return None

    # --- turn.started ---
    if event_type == "turn.started":
        return None  # No downstream representation needed

    # --- turn.completed: terminal success ---
    if event_type == "turn.completed":
        usage_raw = event.get("usage", {})
        usage = _normalize_usage(usage_raw) if usage_raw else {}
        return {
            "type": "result",
            "subtype": "success",
            "result": "",
            "usage": usage,
        }

    # --- turn.failed: terminal failure ---
    if event_type == "turn.failed":
        return {
            "type": "result",
            "subtype": "error_during_execution",
            "is_error": True,
            "error_message": event.get("message", "Codex turn failed"),
        }

    # --- error: unrecoverable stream error ---
    if event_type == "error":
        return {
            "type": "result",
            "subtype": "error_during_execution",
            "is_error": True,
            "error_message": event.get("message", "Codex stream error"),
        }

    # --- collab_tool_call: collaborative agent orchestration event ---
    # Codex may emit these as standalone JSONL events (not just embedded in
    # agent_message text).  Convert to tool_use/tool_result blocks.
    if event_type == "collab_tool_call":
        blocks = _collab_to_tool_blocks([event])
        if blocks:
            return {"type": "assistant", "content": blocks}
        return None

    # Unrecognized event — log and skip
    logger.debug("Unrecognized Codex event type: %s", event_type)
    return None


# ---------------------------------------------------------------------------
# CodexCLI — BackendClient implementation
# ---------------------------------------------------------------------------


class CodexCLI:
    """Codex Rust CLI wrapper that satisfies the ``BackendClient`` protocol.

    Each ``run_completion`` call spawns a fresh ``codex exec --json``
    subprocess and streams normalized chunk dicts.  For multi-turn sessions
    use the ``resume`` parameter which maps to
    ``codex exec resume <thread_id> --json``.
    """

    def __init__(
        self,
        timeout: Optional[int] = None,
        cwd: Optional[str] = None,
    ):
        if timeout is None:
            timeout = CODEX_TIMEOUT_MS
        self.timeout = timeout / 1000  # ms → seconds

        if cwd:
            self.cwd = Path(cwd)
            if not self.cwd.exists():
                raise ValueError(f"Codex working directory does not exist: {self.cwd}")
            logger.info("Codex using working directory: %s", self.cwd)
        else:
            self._temp_dir = tempfile.mkdtemp(prefix="codex_workspace_")
            self.cwd = Path(self._temp_dir)
            logger.info("Codex using temporary workspace: %s", self.cwd)

        self._codex_bin = self._find_codex_binary()
        logger.info("Codex binary: %s", self._codex_bin)

        # Capture OPENAI_API_KEY at init time so that _build_env() never
        # reads from os.environ at call time.  This prevents a race where
        # a concurrent Claude request's _sdk_env() temporarily removes
        # OPENAI_API_KEY from os.environ.
        self._api_key: Optional[str] = os.getenv("OPENAI_API_KEY") or None

        # Persistent collab agent ID mapping for spawn_agent → wait correlation
        # across separate JSONL events within a single run_completion() call.
        self._collab_agent_ids: Dict[str, str] = {}

    # ------------------------------------------------------------------
    # Binary discovery
    # ------------------------------------------------------------------

    @staticmethod
    def _find_codex_binary() -> str:
        """Locate the Codex binary from CODEX_CLI_PATH or PATH."""
        # Explicit path from config
        configured = CODEX_CLI_PATH
        if configured and configured != "codex":
            path = Path(configured)
            if path.is_file():
                return str(path)
            raise RuntimeError(f"CODEX_CLI_PATH points to non-existent file: {configured}")

        # Lookup in PATH
        found = shutil.which("codex")
        if found:
            return found

        raise RuntimeError(
            "Codex binary not found. Install @openai/codex globally or set CODEX_CLI_PATH."
        )

    # ------------------------------------------------------------------
    # Environment construction
    # ------------------------------------------------------------------

    def _build_env(self) -> Dict[str, str]:
        """Build an isolated environment for the Codex subprocess.

        - Injects OPENAI_API_KEY from the init-time capture (never reads
          os.environ at call time — safe against concurrent Claude _sdk_env)
        - Removes ANTHROPIC_AUTH_TOKEN to prevent cross-contamination
        - Optionally isolates ``~/.codex/config.toml`` via CODEX_HOME
        """
        env = os.environ.copy()

        # Auth: use the init-time captured key if available, otherwise
        # remove any empty OPENAI_API_KEY so Codex CLI uses its own auth
        # (e.g. codex login).
        if self._api_key:
            env["OPENAI_API_KEY"] = self._api_key
        else:
            env.pop("OPENAI_API_KEY", None)

        # Prevent Claude auth token from leaking into Codex subprocess
        env.pop("ANTHROPIC_AUTH_TOKEN", None)
        env.pop("ANTHROPIC_API_KEY", None)

        # Config isolation: redirect CODEX_HOME to prevent inheriting
        # the operator's personal ~/.codex/config.toml
        if CODEX_CONFIG_ISOLATION:
            isolation_dir = str(self.cwd / ".codex-server")
            os.makedirs(isolation_dir, exist_ok=True)
            env["CODEX_HOME"] = isolation_dir

        return env

    # ------------------------------------------------------------------
    # Subprocess management
    # ------------------------------------------------------------------

    @contextlib.asynccontextmanager
    async def _spawn_codex(
        self,
        thread_id: Optional[str] = None,
        model: Optional[str] = None,
        system_prompt: Optional[str] = None,
    ):
        """Spawn a ``codex exec`` process with --json output.

        Yields the ``asyncio.subprocess.Process``.  The process is terminated
        on context-manager exit if still running.
        """
        cmd = [self._codex_bin, "exec"]
        if thread_id:
            cmd.extend(["resume", thread_id])
        cmd.append("--json")

        # Add model selection (e.g. "o3" from "codex/o3")
        if model:
            cmd.extend(["--model", model])

        # Add system prompt via Codex config override mechanism.
        # Codex CLI accepts -c 'instructions="..."' to set system instructions.
        if system_prompt:
            cmd.extend(["-c", f"instructions={json.dumps(system_prompt)}"])

        # Add approval/sandbox mode
        cmd.append("--full-auto")

        env = self._build_env()

        # Redact system prompt instructions from debug log to avoid leaking
        # sensitive system prompt content.
        redacted_cmd = []
        skip_next = False
        for k, part in enumerate(cmd):
            if skip_next:
                skip_next = False
                continue
            if part == "-c" and k + 1 < len(cmd) and cmd[k + 1].startswith("instructions="):
                redacted_cmd.append("-c")
                redacted_cmd.append("instructions=<REDACTED>")
                skip_next = True
            else:
                redacted_cmd.append(part)
        logger.debug("Spawning Codex: %s", " ".join(redacted_cmd))
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(self.cwd),
            env=env,
        )
        try:
            yield proc
        finally:
            if proc.returncode is None:
                proc.terminate()
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(proc.wait(), timeout=5.0)
                if proc.returncode is None:
                    proc.kill()

    # ------------------------------------------------------------------
    # JSONL parsing
    # ------------------------------------------------------------------

    @staticmethod
    async def _parse_jsonl(
        stdout: asyncio.StreamReader,
        line_timeout: Optional[float] = None,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """Parse JSONL from stdout, skipping non-JSON preamble lines.

        When *line_timeout* is set, each ``readline()`` call is wrapped with
        ``asyncio.wait_for`` so that a stalled subprocess cannot block the
        streaming loop indefinitely.  On timeout the generator ends cleanly
        and the caller is responsible for emitting an error chunk.
        """
        while True:
            try:
                if line_timeout is not None:
                    line = await asyncio.wait_for(stdout.readline(), timeout=line_timeout)
                else:
                    line = await stdout.readline()
            except asyncio.TimeoutError:
                logger.warning("Codex readline timed out after %.1fs", line_timeout)
                break
            if not line:
                break
            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            # Guard: skip non-JSON preamble (e.g. "Reading prompt from stdin...")
            if not text.startswith("{"):
                logger.debug("Codex non-JSON output: %s", text)
                continue
            try:
                yield json.loads(text)
            except json.JSONDecodeError:
                logger.warning("Codex malformed JSON line: %.200s", text)

    # ------------------------------------------------------------------
    # BackendClient: run_completion
    # ------------------------------------------------------------------

    async def run_completion(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        model: Optional[str] = None,
        stream: bool = True,
        max_turns: int = 10,
        allowed_tools: Optional[List[str]] = None,
        disallowed_tools: Optional[List[str]] = None,
        session_id: Optional[str] = None,
        resume: Optional[str] = None,
        permission_mode: Optional[str] = None,
        output_format: Optional[Dict[str, Any]] = None,
        mcp_servers: Optional[Dict[str, Any]] = None,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """Spawn Codex, stream JSONL, and yield normalized internal chunks.

        For multi-turn conversations:
        - First turn: ``session_id`` is ignored (Codex manages its own thread id)
        - Follow-up turns: pass ``resume=<thread_id>`` from a prior ``thread.started`` event
        """
        thread_id_for_resume = resume  # Codex thread_id if resuming
        captured_thread_id: Optional[str] = None
        # Reset per-turn collab agent ID tracking so spawn→wait correlation
        # is scoped to this completion but persists across JSONL events.
        self._collab_agent_ids = {}

        try:
            async with self._spawn_codex(
                thread_id=thread_id_for_resume,
                model=model,
                system_prompt=system_prompt,
            ) as proc:
                # Send prompt via stdin
                assert proc.stdin is not None
                proc.stdin.write(prompt.encode("utf-8"))
                proc.stdin.write(b"\n")
                await proc.stdin.drain()
                proc.stdin.close()

                # Stream JSONL events with per-line timeout enforcement.
                # Each readline() is individually guarded so a stalled
                # subprocess cannot block the loop past the timeout.
                assert proc.stdout is not None
                timed_out = False

                async for event in self._parse_jsonl(proc.stdout, line_timeout=self.timeout):
                    # Capture thread_id from thread.started for session management.
                    # Yield the codex_session meta-event IMMEDIATELY so the caller
                    # can store provider_session_id even if the turn later fails
                    # or times out.
                    if event.get("type") == "thread.started":
                        captured_thread_id = event.get("thread_id")
                        logger.debug("Codex thread started: %s", captured_thread_id)
                        if captured_thread_id:
                            yield {
                                "type": "codex_session",
                                "session_id": captured_thread_id,
                            }
                        continue  # Don't yield to downstream as normal content

                    # Handle collab_tool_call events inline with the
                    # instance-level agent ID dict so spawn→wait correlation
                    # persists across separate JSONL events.
                    if event.get("type") == "collab_tool_call":
                        blocks = _collab_to_tool_blocks(
                            [event], agent_tool_ids=self._collab_agent_ids
                        )
                        if blocks:
                            yield {"type": "assistant", "content": blocks}
                        continue

                    normalized = normalize_codex_event(event)
                    if normalized is not None:
                        yield normalized

                # Detect timeout: _parse_jsonl breaks on readline timeout
                # while the process is still running.  Mark *before* wait()
                # so clean EOF (process already exited) is not confused.
                if proc.returncode is None:
                    # Process still alive after JSONL loop ended — either a
                    # readline timeout or the process hasn't set returncode yet.
                    # Wait briefly for natural exit to distinguish EOF from timeout.
                    with contextlib.suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(proc.wait(), timeout=2.0)
                    if proc.returncode is None:
                        timed_out = True
                else:
                    # Process already exited — clean EOF, not a timeout.
                    pass

                if timed_out:
                    logger.error("Codex timed out after %.1f seconds", self.timeout)
                    yield {
                        "type": "result",
                        "subtype": "error_during_execution",
                        "is_error": True,
                        "error_message": f"Codex timed out after {self.timeout:.0f}s",
                    }
                    return

                # Collect stderr for diagnostics
                assert proc.stderr is not None
                stderr_data = await proc.stderr.read()
                if stderr_data:
                    stderr_text = stderr_data.decode("utf-8", errors="replace").strip()
                    if stderr_text:
                        logger.debug("Codex stderr: %s", stderr_text[:500])
                if proc.returncode and proc.returncode != 0:
                    logger.warning("Codex exited with code %d", proc.returncode)

        except asyncio.TimeoutError:
            logger.error("Codex timed out after %.1f seconds", self.timeout)
            yield {
                "type": "result",
                "subtype": "error_during_execution",
                "is_error": True,
                "error_message": f"Codex timed out after {self.timeout:.0f}s",
            }
        except Exception as e:
            logger.error("Codex execution error: %s", e)
            yield {
                "type": "result",
                "subtype": "error_during_execution",
                "is_error": True,
                "error_message": str(e),
            }

        # Note: codex_session meta-event is yielded immediately at
        # thread.started time (above) so the caller can store
        # provider_session_id even if the turn later fails/times out.

    # ------------------------------------------------------------------
    # BackendClient: parse_message
    # ------------------------------------------------------------------

    def parse_message(self, messages: List[Dict[str, Any]]) -> Optional[str]:
        """Extract assistant text from collected internal chunk dicts.

        Mirrors ``ClaudeCodeCLI.parse_claude_message`` — looks for
        result text first, then falls back to assistant content blocks.
        """
        # First pass: check for ResultMessage with result text
        result_text = None
        for msg in messages:
            if msg.get("subtype") == "success" and "result" in msg:
                result = msg["result"]
                if result and result.strip():
                    result_text = result

        if result_text is not None:
            return _strip_collab_json(result_text) or None

        # Second pass: collect text from assistant content blocks
        all_parts: List[str] = []
        for msg in messages:
            if msg.get("type") != "assistant":
                continue
            content = msg.get("content", [])
            if not isinstance(content, list):
                continue
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "text":
                        text = block.get("text", "")
                        if text.strip():
                            all_parts.append(text)
                    elif block.get("type") == "thinking":
                        # Include thinking in parse but wrapped
                        thinking = block.get("thinking", "")
                        if thinking.strip():
                            all_parts.append(thinking)

        return "\n".join(all_parts) if all_parts else None

    # ------------------------------------------------------------------
    # BackendClient: estimate_token_usage
    # ------------------------------------------------------------------

    def estimate_token_usage(
        self,
        prompt: str,
        completion: str,
        model: Optional[str] = None,
    ) -> Dict[str, int]:
        """Estimate token usage (~4 characters per token)."""
        prompt_tokens = max(1, len(prompt) // 4)
        completion_tokens = max(1, len(completion) // 4)
        return {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        }

    # ------------------------------------------------------------------
    # BackendClient: verify
    # ------------------------------------------------------------------

    async def verify(self) -> bool:
        """Verify the Codex binary exists and is executable."""
        try:
            proc = await asyncio.create_subprocess_exec(
                self._codex_bin,
                "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10.0)
            version = stdout.decode("utf-8", errors="replace").strip()
            if proc.returncode == 0:
                logger.info("Codex CLI verified: %s", version)
                return True
            logger.warning("Codex --version exited with code %d", proc.returncode)
            return False
        except FileNotFoundError:
            logger.error("Codex binary not found at: %s", self._codex_bin)
            return False
        except asyncio.TimeoutError:
            logger.error("Codex --version timed out")
            return False
        except Exception as e:
            logger.error("Codex verification failed: %s", e)
            return False
