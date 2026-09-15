"""Quota-specific coverage for the Claude PreToolUse workspace policy hook."""

import asyncio
from pathlib import Path

import pytest

import src.backends.claude.workspace_sandbox as sandbox

_MIB = 1024 * 1024


async def _call(hook, tool_name: str, tool_input: dict) -> dict:
    return await hook(
        {"tool_name": tool_name, "tool_input": tool_input},
        "tool-use-id",
        None,
    )


def _is_deny(result: dict) -> bool:
    return result.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"


@pytest.fixture
def managed_workspace(tmp_path: Path, monkeypatch):
    workspace = tmp_path / "alice" / "claude"
    workspace.mkdir(parents=True)
    monkeypatch.setattr(sandbox.workspace_manager, "base_path", tmp_path)
    monkeypatch.setenv("USER_WORKSPACE_QUOTA_MB", "1")
    monkeypatch.delenv("WORKSPACE_SANDBOX_ENABLED", raising=False)
    return workspace


def test_quota_enables_pretool_policy_transport(managed_workspace):
    assert sandbox.sandbox_enabled() is True


async def test_quota_only_mode_does_not_implicitly_enable_path_sandbox(
    managed_workspace, tmp_path
):
    outside = tmp_path / "bob" / "claude" / "other.txt"
    outside.parent.mkdir(parents=True)
    outside.write_text("other")

    hook = sandbox.make_workspace_sandbox_hook(managed_workspace)
    result = await _call(hook, "Read", {"file_path": str(outside)})

    assert result == {}


async def test_write_over_remaining_quota_is_denied(managed_workspace):
    (managed_workspace / "existing.bin").write_bytes(b"x" * (900 * 1024))
    hook = sandbox.make_workspace_sandbox_hook(managed_workspace)

    result = await _call(
        hook,
        "Write",
        {
            "file_path": str(managed_workspace / "new.txt"),
            "content": "y" * (200 * 1024),
        },
    )

    assert _is_deny(result)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "Workspace quota exceeded" in reason
    assert str(_MIB) in reason


async def test_write_overwrite_that_shrinks_is_allowed(managed_workspace):
    target = managed_workspace / "full.txt"
    target.write_text("x" * _MIB)
    hook = sandbox.make_workspace_sandbox_hook(managed_workspace)

    result = await _call(
        hook,
        "Write",
        {"file_path": str(target), "content": "small"},
    )

    assert result == {}


async def test_edit_projected_growth_is_denied(managed_workspace):
    target = managed_workspace / "large.txt"
    old = "a" * (900 * 1024)
    target.write_text(old)
    hook = sandbox.make_workspace_sandbox_hook(managed_workspace)

    result = await _call(
        hook,
        "Edit",
        {
            "file_path": str(target),
            "old_string": old,
            "new_string": old + ("b" * (200 * 1024)),
        },
    )

    assert _is_deny(result)


async def test_concurrent_same_user_hooks_are_best_effort_and_recovery_denies_growth(
    managed_workspace,
):
    """PreToolUse checks do not reserve bytes across concurrent Claude sessions.

    Both sessions can observe the same pre-write snapshot and independently fit.
    That race is an intentional soft-quota limitation. Once the overrun exists,
    later deterministic growth must be denied until usage is reduced.
    """

    (managed_workspace / "existing.bin").write_bytes(b"x" * (600 * 1024))
    hook_a = sandbox.make_workspace_sandbox_hook(managed_workspace)
    hook_b = sandbox.make_workspace_sandbox_hook(managed_workspace)
    target_a = managed_workspace / "a.txt"
    target_b = managed_workspace / "b.txt"
    content = "y" * (300 * 1024)

    result_a, result_b = await asyncio.gather(
        _call(hook_a, "Write", {"file_path": str(target_a), "content": content}),
        _call(hook_b, "Write", {"file_path": str(target_b), "content": content}),
    )

    # Each proposed write fits against the shared 600 KiB pre-write snapshot,
    # so both PreToolUse checks can allow before either Claude tool commits.
    assert result_a == {}
    assert result_b == {}

    target_a.write_text(content)
    target_b.write_text(content)
    assert sum(p.stat().st_size for p in managed_workspace.iterdir()) > _MIB

    follow_up = await _call(
        hook_a,
        "Write",
        {"file_path": str(managed_workspace / "c.txt"), "content": "z"},
    )
    assert _is_deny(follow_up)
    reason = follow_up["hookSpecificOutput"]["permissionDecisionReason"]
    assert "Workspace quota exceeded" in reason


async def test_bash_remains_best_effort_in_quota_only_mode(managed_workspace):
    (managed_workspace / "full.bin").write_bytes(b"x" * _MIB)
    hook = sandbox.make_workspace_sandbox_hook(managed_workspace)

    result = await _call(
        hook,
        "Bash",
        {"command": "dd if=/dev/zero of=extra.bin bs=1M count=10"},
    )

    # Arbitrary shell byte deltas are not knowable before execution. This is the
    # explicit soft-quota boundary; filesystem project quotas are the hard option.
    assert result == {}


async def test_anonymous_workspace_is_not_charged_named_user_quota(
    tmp_path, monkeypatch
):
    anonymous = tmp_path / "_tmp_session"
    anonymous.mkdir()
    monkeypatch.setattr(sandbox.workspace_manager, "base_path", tmp_path)
    monkeypatch.setenv("USER_WORKSPACE_QUOTA_MB", "1")
    monkeypatch.delenv("WORKSPACE_SANDBOX_ENABLED", raising=False)
    hook = sandbox.make_workspace_sandbox_hook(anonymous)

    result = await _call(
        hook,
        "Write",
        {"file_path": str(anonymous / "large.txt"), "content": "x" * (2 * _MIB)},
    )

    assert result == {}


async def test_quota_preflight_io_runs_off_the_event_loop(managed_workspace, monkeypatch):
    """The async hook must not execute the O(files) accounting scan on the loop.

    A quota check blocked in a worker thread leaves the loop free: an
    independent task keeps advancing while the hook is in flight, and the hook
    is observably still pending at that moment. Run on the loop, the blocking
    call would freeze every other coroutine — and the hook would already be
    finished by the time this coroutine regained control.
    """
    import threading

    started = threading.Event()
    release = threading.Event()

    def blocking_ensure_growth_fits(*_args, **_kwargs):
        started.set()
        # Bounded so a regression fails instead of hanging the suite.
        release.wait(timeout=5)

    monkeypatch.setattr(sandbox, "ensure_growth_fits", blocking_ensure_growth_fits)
    hook = sandbox.make_workspace_sandbox_hook(managed_workspace)

    hook_task = asyncio.create_task(
        _call(hook, "Write", {"file_path": str(managed_workspace / "a.txt"), "content": "x"})
    )
    ticks = 0
    for _ in range(500):
        if started.is_set():
            break
        await asyncio.sleep(0.01)
        ticks += 1
    assert started.is_set(), "quota preflight never ran"
    # The scan is blocked in a worker while this coroutine is running: the loop
    # is responsive and the hook has not returned yet.
    assert not hook_task.done(), "quota preflight ran synchronously on the event loop"

    release.set()
    assert await hook_task == {}


async def test_edit_projection_read_runs_off_the_event_loop(managed_workspace, monkeypatch):
    """The Edit/MultiEdit ``read_text`` projection is also blocking I/O."""
    import threading

    started = threading.Event()
    release = threading.Event()
    real_read_text = Path.read_text

    target = managed_workspace / "doc.txt"
    target.write_text("hello")

    def slow_read_text(self, *args, **kwargs):
        if self == target:
            started.set()
            release.wait(timeout=5)
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", slow_read_text)
    hook = sandbox.make_workspace_sandbox_hook(managed_workspace)

    hook_task = asyncio.create_task(
        _call(
            hook,
            "Edit",
            {"file_path": str(target), "old_string": "hello", "new_string": "hello!"},
        )
    )
    for _ in range(500):
        if started.is_set():
            break
        await asyncio.sleep(0.01)
    assert started.is_set()
    assert not hook_task.done(), "Edit projection read ran synchronously on the event loop"

    release.set()
    assert await hook_task == {}


async def test_accounting_failure_denies_growth_instead_of_raising(managed_workspace, monkeypatch):
    """An unreadable subtree is never counted as zero: the hook fails closed."""
    from src.workspace_quota import WorkspaceQuotaAccountingError

    def failing_ensure_growth_fits(*_args, **_kwargs):
        raise WorkspaceQuotaAccountingError(
            managed_workspace / "blocked", PermissionError(13, "Permission denied")
        )

    monkeypatch.setattr(sandbox, "ensure_growth_fits", failing_ensure_growth_fits)
    hook = sandbox.make_workspace_sandbox_hook(managed_workspace)

    result = await _call(
        hook, "Write", {"file_path": str(managed_workspace / "a.txt"), "content": "x"}
    )

    assert _is_deny(result)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "could not be measured safely" in reason
