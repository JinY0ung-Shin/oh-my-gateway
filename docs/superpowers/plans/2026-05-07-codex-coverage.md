# Codex Backend Coverage 90%+ Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Raise `src/backends/codex/__init__.py` from 71% to 90%+ and `src/backends/codex/client.py` from 83% to 90%+ by adding focused unit tests.

**Architecture:** All work is additive in `tests/test_codex_backend.py`. Tests reuse the existing `FakeRpc` pattern and `monkeypatch.setattr("src.backends.codex.client.CodexJsonRpcClient", ...)` to bypass real subprocess. Pure helpers are tested by direct method calls. No production code changes.

**Tech Stack:** pytest (asyncio_mode=auto), unittest.mock.AsyncMock, monkeypatch.

---

## File Structure

- Modify only: `tests/test_codex_backend.py` (append new tests at end)
- No new files
- No production code changes

## Notes for the Implementer

- These are **characterization tests** on existing production code — every test MUST PASS on first run. If a test fails, the test is wrong (or a hidden bug exists — flag it, don't paper over).
- `pytest-asyncio` is configured with `asyncio_mode = "auto"` — do NOT add `@pytest.mark.asyncio` to async tests; the existing `@pytest.mark.asyncio` decorators in this file are leftover but harmless. Match the surrounding style (decorator present) for consistency in this single file.
- After every task: run only the new tests, then commit. Do not run the full suite until Task 7.

---

### Task 1: Add `__init__.py` lazy import and register tests

**Files:**
- Modify: `tests/test_codex_backend.py` (append)

- [ ] **Step 1: Append the test block**

Append the following to the end of `tests/test_codex_backend.py`:

```python


# ---------------------------------------------------------------------------
# Group 1: src/backends/codex/__init__.py lazy imports and register failure
# ---------------------------------------------------------------------------


def test_codex_init_lazy_imports_codex_client():
    """Accessing CodexClient on the package triggers lazy import."""
    import src.backends.codex as codex_pkg
    from src.backends.codex.client import CodexClient

    assert codex_pkg.CodexClient is CodexClient


def test_codex_init_lazy_imports_codex_auth_provider():
    """Accessing CodexAuthProvider on the package triggers lazy import."""
    import src.backends.codex as codex_pkg
    from src.backends.codex.auth import CodexAuthProvider

    assert codex_pkg.CodexAuthProvider is CodexAuthProvider


def test_codex_init_unknown_attribute_raises_attribute_error():
    """Unknown package attributes raise AttributeError with helpful message."""
    import src.backends.codex as codex_pkg

    with pytest.raises(AttributeError, match="DoesNotExist"):
        codex_pkg.DoesNotExist  # noqa: B018


def test_codex_register_records_descriptor_and_live_client():
    """register() registers the descriptor and a CodexClient instance."""
    import src.backends.codex as codex_pkg

    descriptors = []
    registered = []

    class FakeRegistry:
        @classmethod
        def register_descriptor(cls, descriptor):
            descriptors.append(descriptor)

        @classmethod
        def register(cls, name, client):
            registered.append((name, client))

    codex_pkg.register(FakeRegistry)

    assert descriptors == [codex_pkg.CODEX_DESCRIPTOR]
    assert len(registered) == 1
    assert registered[0][0] == "codex"


def test_codex_register_logs_error_when_client_init_fails(monkeypatch, caplog):
    """If CodexClient() raises, register() still installs the descriptor and logs."""
    import src.backends.codex as codex_pkg

    class BoomClient:
        def __init__(self):
            raise RuntimeError("boom from CodexClient init")

    monkeypatch.setattr("src.backends.codex.client.CodexClient", BoomClient)

    descriptors = []
    registered = []

    class FakeRegistry:
        @classmethod
        def register_descriptor(cls, descriptor):
            descriptors.append(descriptor)

        @classmethod
        def register(cls, name, client):
            registered.append((name, client))

    with caplog.at_level("ERROR", logger="src.backends.codex"):
        codex_pkg.register(FakeRegistry)

    assert descriptors == [codex_pkg.CODEX_DESCRIPTOR]
    assert registered == []
    assert "Codex backend client creation failed" in caplog.text
```

- [ ] **Step 2: Run the new tests**

Run: `uv run pytest tests/test_codex_backend.py -k "codex_init or codex_register" -v`
Expected: 5 tests PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/test_codex_backend.py
git commit -m "test(codex): cover __init__ lazy imports and register failure path"
```

---

### Task 2: Add pure helper tests (approval handling)

**Files:**
- Modify: `tests/test_codex_backend.py` (append)

- [ ] **Step 1: Append the test block**

Append to end of `tests/test_codex_backend.py`:

```python


# ---------------------------------------------------------------------------
# Group 2: pure helpers — approval decisions, kinds, options
# ---------------------------------------------------------------------------


def test_codex_normalize_approval_decision_aliases():
    """All alias strings map to canonical decisions."""
    from src.backends.codex.client import CodexClient

    client = CodexClient()

    for value in ["yes", "y", "allow", "approve", "approved", "once"]:
        assert client._normalize_approval_decision(value) == "accept", value
    for value in ["no", "n", "deny", "denied", "reject", "rejected", ""]:
        assert client._normalize_approval_decision(value) == "decline", value
    for value in ["always", "session"]:
        assert client._normalize_approval_decision(value) == "acceptForSession", value
    assert client._normalize_approval_decision("stop") == "cancel"

    # Canonical values pass through unchanged.
    for value in ["accept", "acceptForSession", "decline", "cancel"]:
        assert client._normalize_approval_decision(value) == value, value

    # Unknown value falls through to decline.
    assert client._normalize_approval_decision("unknown_value") == "decline"


def test_codex_normalize_approval_decision_handles_list_and_none():
    """Non-string inputs go through string coercion / list head extraction."""
    from src.backends.codex.client import CodexClient

    client = CodexClient()

    assert client._normalize_approval_decision(["yes", "no"]) == "accept"
    assert client._normalize_approval_decision([]) == "decline"
    assert client._normalize_approval_decision(None) == "decline"


def test_codex_approval_kind_falls_back_for_unknown_method():
    """Known methods map to known kinds; everything else is generic 'approval'."""
    from src.backends.codex.client import CodexClient

    client = CodexClient()

    assert client._approval_kind("item/commandExecution/requestApproval") == "command"
    assert client._approval_kind("item/fileChange/requestApproval") == "file_change"
    assert client._approval_kind("item/permissions/requestApproval") == "permissions"
    assert client._approval_kind("item/newFeature/requestApproval") == "approval"


def test_codex_approval_question_covers_all_kinds():
    """Each approval kind produces a human-readable question."""
    from src.backends.codex.client import CodexClient

    client = CodexClient()

    assert client._approval_question("command", {"command": "ls"}) == (
        "Codex requests approval to run command: ls"
    )
    assert client._approval_question("command", {}) == (
        "Codex requests approval to run a command."
    )
    assert client._approval_question("command", {"command": ""}) == (
        "Codex requests approval to run a command."
    )
    assert client._approval_question("file_change", {}) == (
        "Codex requests approval to apply file changes."
    )
    assert client._approval_question("permissions", {}) == "Codex requests additional permissions."
    assert client._approval_question("approval", {}) == "Codex requests approval."


def test_codex_approval_decision_label_handles_dict_decisions():
    """Dict-shaped decisions produce labels covering every supported branch."""
    from src.backends.codex.client import CodexClient

    client = CodexClient()

    # Plain string passes through.
    assert client._approval_decision_label("accept") == "accept"

    # Empty / non-dict / non-string return "".
    assert client._approval_decision_label({}) == ""
    assert client._approval_decision_label(None) == ""
    assert client._approval_decision_label(123) == ""

    # acceptWithExecpolicyAmendment.
    assert (
        client._approval_decision_label({"acceptWithExecpolicyAmendment": {}})
        == "acceptWithExecpolicyAmendment"
    )

    # applyNetworkPolicyAmendment with full action+host returns enriched label.
    full = {
        "applyNetworkPolicyAmendment": {
            "network_policy_amendment": {"action": "allow", "host": "api.example.com"},
        }
    }
    assert (
        client._approval_decision_label(full)
        == "applyNetworkPolicyAmendment:allow:api.example.com"
    )

    # applyNetworkPolicyAmendment missing host falls back to bare name.
    partial = {"applyNetworkPolicyAmendment": {"network_policy_amendment": {"action": "allow"}}}
    assert client._approval_decision_label(partial) == "applyNetworkPolicyAmendment"

    # applyNetworkPolicyAmendment with non-dict body falls back to bare name.
    bare = {"applyNetworkPolicyAmendment": "raw"}
    assert client._approval_decision_label(bare) == "applyNetworkPolicyAmendment"

    # Other dict shapes return the first key.
    assert client._approval_decision_label({"customDecision": {}}) == "customDecision"


def test_codex_approval_decision_from_available_options_matches_dict_decision():
    """Dict decisions can be selected by their generated label."""
    from src.backends.codex.client import CodexClient

    client = CodexClient()

    decisions = ["accept", {"acceptWithExecpolicyAmendment": {"foo": "bar"}}]

    matched = client._approval_decision_from_available_options(
        "acceptWithExecpolicyAmendment",
        {"availableDecisions": decisions},
    )
    assert matched == {"acceptWithExecpolicyAmendment": {"foo": "bar"}}


def test_codex_approval_decision_from_available_options_returns_none_when_no_match():
    """Non-matching label or missing/invalid availableDecisions returns None."""
    from src.backends.codex.client import CodexClient

    client = CodexClient()

    assert (
        client._approval_decision_from_available_options(
            "nothing", {"availableDecisions": ["accept"]}
        )
        is None
    )
    assert client._approval_decision_from_available_options("accept", {}) is None
    assert (
        client._approval_decision_from_available_options(
            "accept", {"availableDecisions": "not-a-list"}
        )
        is None
    )
```

- [ ] **Step 2: Run the new tests**

Run: `uv run pytest tests/test_codex_backend.py -k "approval_decision or approval_kind or approval_question or normalize_approval" -v`
Expected: 7 tests PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/test_codex_backend.py
git commit -m "test(codex): cover approval helper branches"
```

---

### Task 3: Add pure helper tests (item / usage parsing)

**Files:**
- Modify: `tests/test_codex_backend.py` (append)

- [ ] **Step 1: Append the test block**

Append to end of `tests/test_codex_backend.py`:

```python


# ---------------------------------------------------------------------------
# Group 3: pure helpers — item parsing, token usage, final-response selection
# ---------------------------------------------------------------------------


def test_codex_tool_use_from_item_returns_none_for_invalid_inputs():
    """Non-dict / unknown type / missing or non-string id all skip tool_use conversion."""
    from src.backends.codex.client import CodexClient

    client = CodexClient()

    assert client._tool_use_from_item(None) is None
    assert client._tool_use_from_item("string") is None
    assert client._tool_use_from_item({"type": "agentMessage", "id": "x"}) is None
    assert client._tool_use_from_item({"type": "commandExecution"}) is None
    assert client._tool_use_from_item({"type": "commandExecution", "id": 123}) is None
    assert client._tool_use_from_item({"type": "commandExecution", "id": ""}) is None


def test_codex_tool_use_from_item_strips_meta_fields():
    """Valid items are converted, dropping id / type / aggregatedOutput from input."""
    from src.backends.codex.client import CodexClient

    client = CodexClient()

    item = {
        "type": "commandExecution",
        "id": "tool_1",
        "command": "ls",
        "aggregatedOutput": "should be dropped",
    }

    assert client._tool_use_from_item(item) == {
        "type": "tool_use",
        "id": "tool_1",
        "name": "commandExecution",
        "input": {"command": "ls"},
    }


def test_codex_tool_result_from_item_command_with_non_zero_exit_is_error():
    """commandExecution items with a non-zero exitCode flip is_error to True."""
    from src.backends.codex.client import CodexClient

    client = CodexClient()

    item = {
        "type": "commandExecution",
        "id": "tool_1",
        "status": "completed",
        "exitCode": 1,
        "aggregatedOutput": "boom",
    }

    assert client._tool_result_from_item(item) == {
        "type": "tool_result",
        "tool_use_id": "tool_1",
        "content": "boom",
        "is_error": True,
    }


def test_codex_tool_result_from_item_declined_status_is_error():
    """Declined / failed status flags is_error and falls back to JSON dump when output is empty."""
    import json

    from src.backends.codex.client import CodexClient

    client = CodexClient()

    item = {
        "type": "commandExecution",
        "id": "tool_1",
        "status": "declined",
        "exitCode": 0,
        "aggregatedOutput": "",
        "command": "rm -rf /",
    }

    result = client._tool_result_from_item(item)

    assert result["is_error"] is True
    parsed = json.loads(result["content"])
    assert parsed == {"status": "declined", "exitCode": 0, "command": "rm -rf /"}


def test_codex_tool_result_from_item_non_command_uses_json_dump():
    """Non-command tool items dump remaining fields as JSON content."""
    import json

    from src.backends.codex.client import CodexClient

    client = CodexClient()

    item = {
        "type": "fileChange",
        "id": "tool_2",
        "status": "completed",
        "path": "/tmp/file.txt",
        "patch": "diff --git",
    }

    result = client._tool_result_from_item(item)

    assert result["tool_use_id"] == "tool_2"
    assert result["is_error"] is False
    assert json.loads(result["content"]) == {
        "status": "completed",
        "path": "/tmp/file.txt",
        "patch": "diff --git",
    }


def test_codex_tool_result_from_item_returns_none_for_invalid_inputs():
    """Mirror of tool_use_from_item: filters non-dict / unknown type / bad id."""
    from src.backends.codex.client import CodexClient

    client = CodexClient()

    assert client._tool_result_from_item(None) is None
    assert client._tool_result_from_item({"type": "agentMessage", "id": "x"}) is None
    assert client._tool_result_from_item({"type": "commandExecution"}) is None
    assert client._tool_result_from_item({"type": "commandExecution", "id": 123}) is None


def test_codex_extract_usage_returns_none_for_invalid_inputs():
    """Non-dict tokenUsage and missing / non-dict 'last' return None."""
    from src.backends.codex.client import CodexClient

    client = CodexClient()

    assert client._extract_usage(None) is None
    assert client._extract_usage("string") is None
    assert client._extract_usage({}) is None
    assert client._extract_usage({"last": "not-a-dict"}) is None


def test_codex_final_response_falls_back_to_unknown_phase():
    """When no item has phase=final_answer, fall back to the most recent unknown-phase agentMessage."""
    from src.backends.codex.client import CodexClient

    client = CodexClient()

    items = [
        {"type": "agentMessage", "phase": None, "text": "thinking out loud"},
        {"type": "agentMessage", "phase": "intermediate", "text": "skipped"},
        {"type": "commandExecution", "phase": None, "text": "ignored"},
    ]

    assert client._final_response_from_items(items) == "thinking out loud"


def test_codex_final_response_returns_none_for_no_match():
    """Empty input or items lacking string text return None."""
    from src.backends.codex.client import CodexClient

    client = CodexClient()

    assert client._final_response_from_items([]) is None
    assert client._final_response_from_items([{"type": "commandExecution"}]) is None
    assert (
        client._final_response_from_items(
            [{"type": "agentMessage", "phase": "final_answer", "text": None}]
        )
        is None
    )


def test_codex_turn_error_message_uses_default_when_missing():
    """Missing or message-less turn errors fall back to a default string."""
    from src.backends.codex.client import CodexClient

    client = CodexClient()

    assert client._turn_error_message({}) == "Codex turn failed"
    assert client._turn_error_message({"error": None}) == "Codex turn failed"
    assert client._turn_error_message({"error": {}}) == "Codex turn failed"
    assert client._turn_error_message({"error": {"message": "oops"}}) == "oops"
```

- [ ] **Step 2: Run the new tests**

Run: `uv run pytest tests/test_codex_backend.py -k "tool_use_from_item or tool_result_from_item or extract_usage or final_response or turn_error_message" -v`
Expected: 10 tests PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/test_codex_backend.py
git commit -m "test(codex): cover tool-item, usage, and final-response helpers"
```

---

### Task 4: Add small-utility helper tests

**Files:**
- Modify: `tests/test_codex_backend.py` (append)

- [ ] **Step 1: Append the test block**

Append to end of `tests/test_codex_backend.py`:

```python


# ---------------------------------------------------------------------------
# Group 4: pure helpers — small utilities (params, message parsing, env, errors)
# ---------------------------------------------------------------------------


def test_codex_public_error_message_strips_stderr_tail_for_app_server_error():
    """CodexAppServerError messages drop the verbose stderr_tail suffix."""
    from src.backends.codex.client import CodexAppServerError, CodexClient

    client = CodexClient()

    exc = CodexAppServerError("Timed out. stderr_tail=verbose internal logs")
    assert client._public_error_message(exc) == "Timed out."

    # Empty message returns generic fallback.
    assert client._public_error_message(CodexAppServerError("")) == "Codex app-server error"


def test_codex_public_error_message_passes_through_other_exceptions():
    """Non CodexAppServerError exceptions retain their str() form."""
    from src.backends.codex.client import CodexClient

    client = CodexClient()

    assert client._public_error_message(ValueError("bad input")) == "bad input"


def test_codex_combine_system_prompt_combinations():
    """All four combinations of (custom_base, system_prompt) produce the right output."""
    from src.backends.codex.client import CodexClient

    client = CodexClient()

    assert client._combine_system_prompt(None, None) is None
    assert client._combine_system_prompt("base", None) == "base"
    assert client._combine_system_prompt(None, "user") == "user"
    assert client._combine_system_prompt("base", "user") == "base\n\nuser"


def test_codex_thread_params_includes_only_set_fields():
    """Optional thread params are omitted when their inputs are None / empty."""
    from src.backends.codex.client import CodexClient

    client = CodexClient()

    bare = client._thread_params(model=None, cwd=None, system_prompt=None)
    assert "model" not in bare
    assert "cwd" not in bare
    assert "developerInstructions" not in bare
    assert "approvalPolicy" in bare
    assert "sandbox" in bare

    full = client._thread_params(model="gpt-5", cwd="/tmp", system_prompt="hello")
    assert full["model"] == "gpt-5"
    assert full["cwd"] == "/tmp"
    assert full["developerInstructions"] == "hello"


def test_codex_turn_params_uses_session_client_fields():
    """Turn params reflect the session client's model/cwd, including 'unset' case."""
    from src.backends.codex.client import CodexClient, CodexJsonRpcClient, CodexSessionClient

    client = CodexClient()
    rpc = CodexJsonRpcClient()
    session_client = CodexSessionClient(rpc=rpc, thread_id="t", model=None, cwd=None)

    bare = client._turn_params(session_client)
    assert "model" not in bare
    assert "cwd" not in bare
    assert "approvalPolicy" in bare

    session_client.model = "gpt-5"
    session_client.cwd = "/tmp"
    full = client._turn_params(session_client)
    assert full["model"] == "gpt-5"
    assert full["cwd"] == "/tmp"


def test_codex_metadata_env_filters_by_allowlist(monkeypatch):
    """Only allowlisted metadata keys are forwarded as env vars; None becomes {}."""
    from src import constants as constants_module
    from src.backends.codex.client import CodexClient

    monkeypatch.setattr(
        constants_module, "METADATA_ENV_ALLOWLIST", frozenset({"ALLOWED_KEY"})
    )

    client = CodexClient()

    assert client._metadata_env(None) == {}
    assert client._metadata_env({}) == {}
    assert client._metadata_env({"ALLOWED_KEY": "value", "BLOCKED_KEY": "no"}) == {
        "ALLOWED_KEY": "value"
    }


def test_codex_parse_message_prefers_success_result():
    """The newest success/result string wins over assistant content blocks."""
    from src.backends.codex.client import CodexClient

    client = CodexClient()

    messages = [
        {"type": "assistant", "content": [{"type": "text", "text": "fallback"}]},
        {"subtype": "success", "result": "winning result"},
    ]

    assert client.parse_message(messages) == "winning result"


def test_codex_parse_message_falls_back_to_assistant_content():
    """Without a success/result, parse_message stitches assistant text blocks."""
    from src.backends.codex.client import CodexClient

    client = CodexClient()

    messages = [
        {"type": "assistant", "content": [{"type": "text", "text": "first"}]},
        {"type": "assistant", "content": [{"type": "text", "text": "second"}]},
    ]

    result = client.parse_message(messages)

    assert result is not None
    assert "first" in result
    assert "second" in result


def test_codex_parse_message_returns_none_for_empty_inputs():
    """Empty list and whitespace-only success result both yield None."""
    from src.backends.codex.client import CodexClient

    client = CodexClient()

    assert client.parse_message([]) is None
    assert client.parse_message([{"subtype": "success", "result": "   "}]) is None


def test_codex_estimate_token_usage_uses_length_heuristic():
    """Token estimate is ceil(len/4) with a floor of 1 each."""
    from src.backends.codex.client import CodexClient

    client = CodexClient()

    usage = client.estimate_token_usage("a" * 40, "b" * 80)
    assert usage == {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}

    floor = client.estimate_token_usage("", "")
    assert floor["prompt_tokens"] >= 1
    assert floor["completion_tokens"] >= 1
```

- [ ] **Step 2: Run the new tests**

Run: `uv run pytest tests/test_codex_backend.py -k "public_error_message or combine_system_prompt or thread_params or turn_params or metadata_env or parse_message or estimate_token_usage" -v`
Expected: 10 tests PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/test_codex_backend.py
git commit -m "test(codex): cover small helper utilities"
```

---

### Task 5: Add `CodexJsonRpcClient` I/O error tests

**Files:**
- Modify: `tests/test_codex_backend.py` (append)

- [ ] **Step 1: Append the test block**

Append to end of `tests/test_codex_backend.py`:

```python


# ---------------------------------------------------------------------------
# Group 5: CodexJsonRpcClient I/O error branches
# ---------------------------------------------------------------------------


def _make_rpc_with_queued_line(line: str):
    """Construct an RPC instance whose stdout queue has one preloaded line."""
    import queue as queue_module

    from src.backends.codex.client import CodexJsonRpcClient

    rpc = CodexJsonRpcClient()
    rpc._proc = SimpleNamespace(stdout=SimpleNamespace())
    rpc._stdout_queue = queue_module.Queue()
    rpc._stdout_queue.put(line)
    return rpc


def test_codex_rpc_read_message_raises_on_invalid_json():
    """Garbage on the wire becomes a CodexAppServerError."""
    from src.backends.codex.client import CodexAppServerError

    rpc = _make_rpc_with_queued_line("not valid json\n")

    with pytest.raises(CodexAppServerError, match="Invalid Codex JSON-RPC line"):
        rpc._read_message()


def test_codex_rpc_read_message_raises_on_non_dict_payload():
    """A JSON array (or other non-object) is also rejected."""
    from src.backends.codex.client import CodexAppServerError

    rpc = _make_rpc_with_queued_line('["array", "not dict"]\n')

    with pytest.raises(CodexAppServerError, match="Invalid Codex JSON-RPC payload"):
        rpc._read_message()


def test_codex_rpc_read_message_raises_when_stdout_closed():
    """Sentinel None from the drain thread surfaces as 'closed stdout'."""
    from src.backends.codex.client import CodexAppServerError

    rpc = _make_rpc_with_queued_line(None)

    with pytest.raises(CodexAppServerError, match="closed stdout"):
        rpc._read_message()


def test_codex_rpc_read_message_raises_when_proc_missing():
    """An unstarted RPC instance refuses to read."""
    from src.backends.codex.client import CodexAppServerError, CodexJsonRpcClient

    rpc = CodexJsonRpcClient()

    with pytest.raises(CodexAppServerError, match="not running"):
        rpc._read_message()


def test_codex_rpc_write_message_raises_when_proc_missing():
    """An unstarted RPC instance refuses to write."""
    from src.backends.codex.client import CodexAppServerError, CodexJsonRpcClient

    rpc = CodexJsonRpcClient()

    with pytest.raises(CodexAppServerError, match="not running"):
        rpc._write_message({"id": "x", "method": "ping"})


def test_codex_rpc_close_is_noop_when_proc_missing():
    """close() on a never-started client is a no-op and idempotent."""
    from src.backends.codex.client import CodexJsonRpcClient

    rpc = CodexJsonRpcClient()
    rpc.close()
    rpc.close()


def test_codex_rpc_thread_start_raises_when_response_not_dict(monkeypatch):
    """Bare list / string responses for thread/start raise CodexAppServerError."""
    from src.backends.codex.client import CodexAppServerError, CodexJsonRpcClient

    rpc = CodexJsonRpcClient()
    monkeypatch.setattr(rpc, "request", lambda method, params=None: ["not", "dict"])

    with pytest.raises(CodexAppServerError, match="thread/start"):
        rpc.thread_start({})


def test_codex_rpc_thread_resume_raises_when_response_not_dict(monkeypatch):
    """Bare list / string responses for thread/resume raise CodexAppServerError."""
    from src.backends.codex.client import CodexAppServerError, CodexJsonRpcClient

    rpc = CodexJsonRpcClient()
    monkeypatch.setattr(rpc, "request", lambda method, params=None: "string")

    with pytest.raises(CodexAppServerError, match="thread/resume"):
        rpc.thread_resume("thr_1", {})


def test_codex_rpc_turn_start_raises_when_response_not_dict(monkeypatch):
    """Bare list / string responses for turn/start raise CodexAppServerError."""
    from src.backends.codex.client import CodexAppServerError, CodexJsonRpcClient

    rpc = CodexJsonRpcClient()
    monkeypatch.setattr(rpc, "request", lambda method, params=None: "string")

    with pytest.raises(CodexAppServerError, match="turn/start"):
        rpc.turn_start("thr_1", [], {})


def test_codex_rpc_model_list_raises_when_response_not_dict(monkeypatch):
    """Bare list / string responses for model/list raise CodexAppServerError."""
    from src.backends.codex.client import CodexAppServerError, CodexJsonRpcClient

    rpc = CodexJsonRpcClient()
    monkeypatch.setattr(rpc, "request", lambda method, params=None: None)

    with pytest.raises(CodexAppServerError, match="model/list"):
        rpc.model_list()
```

- [ ] **Step 2: Run the new tests**

Run: `uv run pytest tests/test_codex_backend.py -k "codex_rpc_" -v`
Expected: 10 tests PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/test_codex_backend.py
git commit -m "test(codex): cover JSON-RPC client I/O error branches"
```

---

### Task 6: Add `CodexClient` async error and accessor tests

**Files:**
- Modify: `tests/test_codex_backend.py` (append)

- [ ] **Step 1: Append the test block**

Append to end of `tests/test_codex_backend.py`:

```python


# ---------------------------------------------------------------------------
# Group 6: CodexClient async error paths and simple accessors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_codex_verify_returns_false_when_model_list_raises(monkeypatch):
    """If the live process rejects model/list, verify() reports failure."""
    from src.backends.codex.client import CodexClient

    class ExplodingRpc:
        def start(self):
            return None

        def model_list(self):
            raise RuntimeError("nope")

        def close(self):
            return None

    monkeypatch.setattr(
        "src.backends.codex.client.CodexJsonRpcClient", lambda **kwargs: ExplodingRpc()
    )

    backend = CodexClient()
    assert await backend.verify() is False


@pytest.mark.asyncio
async def test_codex_verify_returns_false_when_data_not_list(monkeypatch):
    """A model/list response missing the 'data' list also yields False."""
    from src.backends.codex.client import CodexClient

    class WrongShapeRpc:
        def start(self):
            return None

        def model_list(self):
            return {"data": "not a list"}

        def close(self):
            return None

    monkeypatch.setattr(
        "src.backends.codex.client.CodexJsonRpcClient", lambda **kwargs: WrongShapeRpc()
    )

    backend = CodexClient()
    assert await backend.verify() is False


def test_codex_runtime_metadata_includes_expected_keys():
    """runtime_metadata returns a stable dict shape for diagnostics."""
    from src.backends.codex.client import CodexClient

    backend = CodexClient()
    metadata = backend.runtime_metadata()

    assert metadata["mode"] == "app-server"
    assert isinstance(metadata["models"], list)
    assert "approval_policy" in metadata
    assert "sandbox" in metadata
    assert metadata["shared_process"] is False


def test_codex_client_simple_accessors():
    """name, supported_models, get_auth_provider expose the expected types."""
    from src.backends.codex.auth import CodexAuthProvider
    from src.backends.codex.client import CodexClient

    backend = CodexClient()

    assert backend.name == "codex"
    assert isinstance(backend.supported_models(), list)
    assert isinstance(backend.get_auth_provider(), CodexAuthProvider)


@pytest.mark.asyncio
async def test_codex_run_completion_yields_error_when_turn_id_missing(monkeypatch):
    """A turn/start response without turn.id surfaces as an error chunk."""
    from src.backends.codex.client import CodexClient

    class TurnLessRpc:
        def __init__(self):
            self.closed = False

        def is_running(self):
            return not self.closed

        def start(self):
            return None

        def close(self):
            self.closed = True

        def thread_start(self, params):
            return {"thread": {"id": "thr_1"}}

        def thread_resume(self, thread_id, params):
            return {"thread": {"id": thread_id}}

        def turn_start(self, thread_id, input_items, params):
            return {"turn": {}}

        def next_notification(self):
            raise AssertionError("should not be reached")

    monkeypatch.setattr(
        "src.backends.codex.client.CodexJsonRpcClient", lambda **kwargs: TurnLessRpc()
    )

    backend = CodexClient()
    session = SimpleNamespace(session_id="gw")
    client = await backend.create_client(session=session)

    chunks = [
        chunk async for chunk in backend.run_completion_with_client(client, "hi", session)
    ]

    assert len(chunks) == 1
    assert chunks[0]["type"] == "error"
    assert chunks[0]["is_error"] is True
    assert "turn.id" in chunks[0]["error_message"]


@pytest.mark.asyncio
async def test_codex_resume_approval_errors_when_request_id_missing(monkeypatch):
    """resume_approval rejects a session whose pending request_id was never set."""
    from src.backends.codex.client import (
        CodexClient,
        CodexJsonRpcClient,
        CodexSessionClient,
    )

    backend = CodexClient()

    rpc = CodexJsonRpcClient()
    monkeypatch.setattr(backend, "_ensure_rpc_locked", AsyncMock(return_value=rpc))
    monkeypatch.setattr(backend, "_close_rpc_locked", AsyncMock())

    session_client = CodexSessionClient(
        rpc=rpc,
        thread_id="thr_1",
        model=None,
        cwd=None,
        env={},
    )

    chunks = [
        chunk
        async for chunk in backend.resume_approval_with_client(
            session_client, "call_xyz", "accept", session=SimpleNamespace()
        )
    ]

    assert len(chunks) == 1
    assert chunks[0]["type"] == "error"
    assert "request id" in chunks[0]["error_message"]


@pytest.mark.asyncio
async def test_codex_resume_approval_errors_when_turn_id_missing(monkeypatch):
    """resume_approval rejects a session whose turn id was lost."""
    from src.backends.codex.client import (
        CodexClient,
        CodexJsonRpcClient,
        CodexSessionClient,
    )

    backend = CodexClient()

    rpc = CodexJsonRpcClient()
    monkeypatch.setattr(backend, "_ensure_rpc_locked", AsyncMock(return_value=rpc))
    monkeypatch.setattr(backend, "_close_rpc_locked", AsyncMock())

    session_client = CodexSessionClient(
        rpc=rpc,
        thread_id="thr_1",
        model=None,
        cwd=None,
        env={},
        pending_approval_request_id="req_1",
        pending_approval_method="item/commandExecution/requestApproval",
        pending_approval_turn_id=None,
        pending_approval_params={},
    )

    chunks = [
        chunk
        async for chunk in backend.resume_approval_with_client(
            session_client, "req_1", "accept", session=SimpleNamespace()
        )
    ]

    assert len(chunks) == 1
    assert chunks[0]["type"] == "error"
    assert "turn id" in chunks[0]["error_message"]
```

- [ ] **Step 2: Run the new tests**

Run: `uv run pytest tests/test_codex_backend.py -k "verify_returns_false or runtime_metadata or client_simple_accessors or run_completion_yields_error or resume_approval_errors" -v`
Expected: 7 tests PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/test_codex_backend.py
git commit -m "test(codex): cover async error paths and accessors"
```

---

### Task 7: Verify coverage and commit summary

**Files:**
- None. Verification step only.

- [ ] **Step 1: Run the full codex test file**

Run: `uv run pytest tests/test_codex_backend.py -v`
Expected: All tests pass (existing + ~49 new).

- [ ] **Step 2: Confirm codex coverage**

Run:
```bash
uv run pytest tests/test_codex_backend.py --cov=src/backends/codex --cov-report=term-missing
```

Expected: `src/backends/codex/__init__.py` ≥ 90% AND `src/backends/codex/client.py` ≥ 90%.

If either is below 90%, identify the next-cheapest missing line cluster from the `Missing` column and add a small targeted test (do NOT bulk-add tests). Re-run.

- [ ] **Step 3: Confirm full suite still passes and overall coverage didn't regress**

Run:
```bash
uv run pytest --cov=src --cov-report=term
```

Expected: All tests pass; total coverage ≥ 92% (previous baseline).

- [ ] **Step 4: No commit needed for verification**

Each task already committed its tests in earlier steps; this task introduces no new files.

If Step 2 required additional targeted tests, commit them here:

```bash
git add tests/test_codex_backend.py
git commit -m "test(codex): close remaining coverage gap"
```

---

## Self-Review Checklist (post-write)

- [x] Spec coverage:
  - Group 1 → Task 1 ✓
  - Group 2 → Task 2 ✓
  - Group 3 → Task 3 ✓
  - Group 4 → Task 4 ✓
  - Group 5 → Task 5 ✓
  - Group 6 → Task 6 ✓
  - Verification → Task 7 ✓
- [x] No "TBD" / "implement later" / "similar to Task N" placeholders.
- [x] Every step that changes code includes the actual code.
- [x] Method names referenced in tests match production names in `src/backends/codex/client.py` (verified).
- [x] `pytest.mark.asyncio` decorator is consistent with existing async tests in the file (already present on existing async tests; matches pattern even though `asyncio_mode=auto` would also work without it).
