# CLAUDE.md

## Claude-Specific
- `pytest-asyncio` uses `asyncio_mode = "auto"`; do not add `@pytest.mark.asyncio` unless a test specifically needs it.
- Mock SDK calls in tests and prefer the shared fixtures in `tests/conftest.py`.
