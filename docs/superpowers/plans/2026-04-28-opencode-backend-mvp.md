# OpenCode Backend MVP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an opt-in OpenCode backend that can be selected through `BACKENDS` and called through `/v1/models` and `/v1/responses`.

**Architecture:** Add a focused `src/backends/opencode` package that satisfies the existing `BackendClient` protocol and talks to OpenCode over HTTP with `httpx`. Update backend discovery to parse `BACKENDS`, update auth fallback for `opencode`, and keep route changes limited to backend-neutral errors and lifecycle cleanup.

**Tech Stack:** Python 3.10+, FastAPI, httpx, pytest, existing `BackendRegistry`/`BackendClient` abstractions.

---

### Task 1: Backend Selection and Descriptor

**Files:**
- Modify: `src/backends/__init__.py`
- Create: `src/backends/opencode/__init__.py`
- Create: `src/backends/opencode/constants.py`
- Test: `tests/test_opencode_backend.py`
- Modify: `tests/test_backends_init_unit.py`

- [ ] **Step 1: Write failing descriptor and BACKENDS tests**

Add tests that expect `BACKENDS` to default to Claude, support `claude,opencode`, skip unknown values, and resolve `opencode/<provider>/<model>`:

```python
def test_opencode_descriptor_resolves_prefixed_model(monkeypatch):
    monkeypatch.setenv("OPENCODE_MODELS", "anthropic/claude-sonnet-4-5")
    import importlib
    import src.backends.opencode as opencode_pkg
    opencode_pkg = importlib.reload(opencode_pkg)

    resolved = opencode_pkg.OPENCODE_DESCRIPTOR.resolve_fn(
        "opencode/anthropic/claude-sonnet-4-5"
    )

    assert resolved is not None
    assert resolved.backend == "opencode"
    assert resolved.provider_model == "anthropic/claude-sonnet-4-5"
    assert opencode_pkg.OPENCODE_DESCRIPTOR.models == [
        "opencode/anthropic/claude-sonnet-4-5"
    ]
```

```python
def test_discover_backends_respects_backends_env(monkeypatch):
    calls = []

    def fake_claude_register(registry_cls=None):
        calls.append("claude")

    def fake_opencode_register(registry_cls=None):
        calls.append("opencode")

    monkeypatch.setenv("BACKENDS", "claude,opencode")
    monkeypatch.setattr("src.backends.claude.register", fake_claude_register)
    monkeypatch.setattr("src.backends.opencode.register", fake_opencode_register)

    from src.backends import discover_backends

    discover_backends()

    assert calls == ["claude", "opencode"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
uv run pytest tests/test_opencode_backend.py tests/test_backends_init_unit.py -q
```

Expected: FAIL because `src.backends.opencode` does not exist and `discover_backends()` ignores `BACKENDS`.

- [ ] **Step 3: Implement descriptor and BACKENDS discovery**

Implement:

```python
def _enabled_backend_names() -> list[str]:
    raw = os.getenv("BACKENDS", "claude")
    names = []
    for item in raw.split(","):
        name = item.strip().lower()
        if name and name not in names:
            names.append(name)
    return names or ["claude"]
```

Implement OpenCode descriptor:

```python
def configured_public_models() -> list[str]:
    return [f"opencode/{m}" for m in configured_provider_models()]

def _opencode_resolve(model: str) -> Optional[ResolvedModel]:
    prefix = "opencode/"
    if not model.startswith(prefix):
        return None
    provider_model = model[len(prefix):]
    if "/" not in provider_model:
        return None
    return ResolvedModel(model, "opencode", provider_model)
```

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
uv run pytest tests/test_opencode_backend.py tests/test_backends_init_unit.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/backends/__init__.py src/backends/opencode/__init__.py src/backends/opencode/constants.py tests/test_opencode_backend.py tests/test_backends_init_unit.py
git commit -m "feat: add opencode backend discovery"
```

### Task 2: OpenCode Auth and HTTP Client

**Files:**
- Create: `src/backends/opencode/auth.py`
- Create: `src/backends/opencode/client.py`
- Modify: `src/auth.py`
- Test: `tests/test_opencode_backend.py`

- [ ] **Step 1: Write failing auth and client tests**

Add tests for `OpenCodeAuthProvider`, `auth_manager.get_provider("opencode")`,
external server mode, prompt conversion, and text parsing:

```python
def test_auth_manager_returns_opencode_provider():
    from src.auth import auth_manager

    provider = auth_manager.get_provider("opencode")

    assert provider.name == "opencode"
```

```python
async def test_opencode_client_sends_prompt_to_existing_server(monkeypatch):
    calls = []

    class FakeResponse:
        def __init__(self, payload):
            self._payload = payload
        def raise_for_status(self):
            return None
        def json(self):
            return self._payload

    class FakeAsyncClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return None
        async def get(self, path):
            return FakeResponse({"healthy": True, "version": "test"})
        async def post(self, path, **kwargs):
            calls.append((path, kwargs))
            if path == "/session":
                return FakeResponse({"id": "oc-session"})
            return FakeResponse({
                "info": {
                    "role": "assistant",
                    "tokens": {"input": 7, "output": 3, "reasoning": 0, "cache": {"read": 0, "write": 0}},
                },
                "parts": [{"type": "text", "text": "hello from opencode"}],
            })

    monkeypatch.setenv("OPENCODE_BASE_URL", "http://127.0.0.1:4096")
    monkeypatch.setattr("src.backends.opencode.client.httpx.AsyncClient", FakeAsyncClient)

    from src.backends.opencode.client import OpenCodeClient
    from src.session_manager import Session

    backend = OpenCodeClient()
    session = Session(session_id="gw-session")
    client = await backend.create_client(
        session=session,
        model="anthropic/claude-sonnet-4-5",
        system_prompt="request instructions",
        _custom_base="base prompt",
        cwd="/tmp/work",
    )
    chunks = [chunk async for chunk in backend.run_completion_with_client(client, "say hi", session)]

    assert calls[0][0] == "/session"
    assert calls[1][0] == "/session/oc-session/message"
    assert calls[1][1]["json"]["system"] == "base prompt\n\nrequest instructions"
    assert chunks[-1]["result"] == "hello from opencode"
    assert backend.parse_message(chunks) == "hello from opencode"
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
uv run pytest tests/test_opencode_backend.py -q
```

Expected: FAIL because auth and client modules do not exist.

- [ ] **Step 3: Implement auth provider and HTTP client**

Implement `OpenCodeAuthProvider.validate()` with:

```python
if os.getenv("OPENCODE_BASE_URL"):
    return {"valid": True, "errors": [], "config": {"mode": "external", "base_url": base_url}}
if shutil.which("opencode"):
    return {"valid": True, "errors": [], "config": {"mode": "managed", "binary": "opencode"}}
return {"valid": False, "errors": ["opencode binary not found on PATH"], "config": {"mode": "managed"}}
```

Implement `OpenCodeClient` with an `OpenCodeSessionClient` dataclass, `verify()`,
`create_client()`, `run_completion_with_client()`, `parse_message()`,
`estimate_token_usage()`, and managed server startup parsing.

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
uv run pytest tests/test_opencode_backend.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/backends/opencode/auth.py src/backends/opencode/client.py src/auth.py tests/test_opencode_backend.py
git commit -m "feat: add opencode backend client"
```

### Task 3: Responses Route and Model Surface Integration

**Files:**
- Modify: `src/routes/responses.py`
- Modify: `src/main.py`
- Test: `tests/test_main_api_unit.py`
- Test: `tests/test_main_coverage_unit.py`

- [ ] **Step 1: Write failing route integration tests**

Add tests proving `/v1/models` includes configured OpenCode models and
`/v1/responses` dispatches to the OpenCode backend without Claude-specific
failure text:

```python
def test_models_endpoint_lists_opencode_models_when_registered():
    from src.backends.base import BackendDescriptor, BackendRegistry, ResolvedModel

    def resolve(model):
        if model == "opencode/anthropic/claude-sonnet-4-5":
            return ResolvedModel(model, "opencode", "anthropic/claude-sonnet-4-5")
        return None

    backend = MagicMock()
    backend.name = "opencode"
    BackendRegistry.register_descriptor(
        BackendDescriptor("opencode", "opencode", ["opencode/anthropic/claude-sonnet-4-5"], resolve)
    )
    BackendRegistry.register("opencode", backend)

    with client_context() as (client, _mock_cli):
        response = client.get("/v1/models")

    ids = [item["id"] for item in response.json()["data"]]
    assert "opencode/anthropic/claude-sonnet-4-5" in ids
```

```python
def test_create_client_failure_uses_backend_neutral_message():
    async def failing_create_client(**kwargs):
        raise RuntimeError("simulated opencode failure")

    with client_context() as (client, mock_cli):
        mock_cli.create_client = failing_create_client
        response = client.post("/v1/responses", json={"model": DEFAULT_MODEL, "input": "hi"})

    assert response.status_code == 503
    assert "Claude Code SDK" not in response.json()["error"]["message"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
uv run pytest tests/test_main_api_unit.py tests/test_main_coverage_unit.py -q
```

Expected: FAIL on the backend-neutral error assertion until route text changes.

- [ ] **Step 3: Implement route and lifecycle integration**

Change `src/routes/responses.py` failure detail to:

```python
detail=f"{resolved.backend} backend unavailable; retry shortly"
```

Add shutdown lifecycle cleanup in `src/main.py`:

```python
for _name, backend in BackendRegistry.all_backends().items():
    close = getattr(backend, "close", None) or getattr(backend, "shutdown", None)
    if close is None:
        continue
    result = close()
    if inspect.isawaitable(result):
        await result
```

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
uv run pytest tests/test_main_api_unit.py tests/test_main_coverage_unit.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/routes/responses.py src/main.py tests/test_main_api_unit.py tests/test_main_coverage_unit.py
git commit -m "feat: wire opencode backend into responses"
```

### Task 4: Final Verification

**Files:**
- Modify: `README.md` only if documenting env usage is necessary for the implemented surface.

- [ ] **Step 1: Run targeted backend tests**

Run:

```bash
uv run pytest tests/test_opencode_backend.py tests/test_backends_init_unit.py tests/test_backend_contract.py -q
```

Expected: PASS.

- [ ] **Step 2: Run route tests**

Run:

```bash
uv run pytest tests/test_main_api_unit.py tests/test_main_coverage_unit.py -q
```

Expected: PASS.

- [ ] **Step 3: Run full non-e2e suite**

Run:

```bash
uv run pytest -q
```

Expected: PASS or a clear unrelated pre-existing failure documented in the final summary.

- [ ] **Step 4: Push branch**

```bash
git push
```

Expected: `codex/opencode-backend-mvp-design` updates on origin.
