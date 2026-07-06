"""Shared per-request header injection into MCP server configs.

Used by the claude and codex backends to merge gateway-resolved context headers
(identity + caller-owned credentials — see ``MCP_FORWARD_CONTEXT`` in
``src/constants.py``) into every http/SSE MCP server's ``headers`` so downstream
MCP servers can authorize server-side, out of the LLM's reach.
"""

import copy
import logging
from typing import Any, Dict, Optional
from urllib.parse import quote

logger = logging.getLogger(__name__)

_HTTP_TYPES = {"http", "sse", "streamable-http"}


def header_safe(value: str) -> str:
    """Return *value* usable as an HTTP header value.

    Percent-encodes non-ascii AND any control characters (CR/LF/NUL/…) so a
    value derived from request input can neither inject extra headers (CRLF) nor
    break the outbound MCP request. Plain printable-ascii passes through
    unchanged.
    """
    if value.isascii() and not any(ord(c) < 0x20 or ord(c) == 0x7F for c in value):
        return value
    return quote(value)


def inject_mcp_headers(
    mcp_servers: Optional[Dict[str, Any]],
    forward_headers: Optional[Dict[str, str]],
) -> Optional[Dict[str, Any]]:
    """Return *mcp_servers* with *forward_headers* merged into every http/SSE
    server's ``headers``.

    Deep-copies first so a shared, process-wide config is never mutated per
    request. Returns the input object unchanged (same identity) when there is
    nothing to inject. ``stdio`` servers are left untouched; a server whose
    existing ``headers`` is not a dict is skipped with a warning rather than
    silently dropped.
    """
    if not mcp_servers or not forward_headers:
        return mcp_servers
    headers_map = {
        name: header_safe(value)
        for name, value in forward_headers.items()
        if name and value
    }
    if not headers_map:
        return mcp_servers

    result = copy.deepcopy(mcp_servers)
    for name, config in result.items():
        if not isinstance(config, dict) or config.get("type", "stdio") not in _HTTP_TYPES:
            continue
        existing = config.get("headers")
        if existing is None:
            config["headers"] = dict(headers_map)
        elif isinstance(existing, dict):
            existing.update(headers_map)
        else:
            logger.warning(
                "MCP server %r has non-dict 'headers' (%s); skipping context "
                "header injection for it",
                name,
                type(existing).__name__,
            )
    return result
