"""Human approval before an MCP tool runs (ChatDRAGON #798).

An MCP server entry may carry the gateway-only key ``"approvalTools"``: a list of tool
names, or exactly ``"*"`` — every tool of that server except the ones its
``readOnlyTools`` names. A call to such a tool is parked before it runs, surfaced to the
client as ``function_call`` + ``requires_action`` with an explicit ``approval`` object,
and runs only when the client answers ``{"decision": "approve", "argsDigest": ...}`` for
exactly those arguments.

Why a PreToolUse hook and not ``can_use_tool``: measured against the bundled CLI
(``tests/test_cli_tool_approval.py``), an MCP call never reaches the permission callback
under ``permission_mode=bypassPermissions`` — the gateway's usual production mode — nor
when the request's ``allowed_tools`` lists the tool. A PreToolUse hook fires in every
mode, the tool does not start while the hook waits, and the hook sees the arguments the
CLI will run with (so what the user approves is what runs).

Fail closed, everywhere:

* the policy is the operator's MCP config, not the server's own annotations — a server
  cannot mark its writes read-only to skip approval, and ``"*"`` also gates a tool the
  server renamed or added later;
* a client that did not declare it can render approval cards
  (``X-Gateway-Capabilities: tool-approval``) gets the call **denied**, never silently
  run and never left waiting on a card it cannot show;
* anything but an explicit approval for the shown arguments (a timeout, ``deny``, an
  unreadable answer, a digest for other arguments) is a denial.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)

APPROVAL_KEY = "approvalTools"
CAPABILITY_HEADER = "x-gateway-capabilities"
CAPABILITY = "tool-approval"
APPROVAL_KIND = "tool_approval"
APPROVAL_VERSION = 1

DENIED_BY_USER = (
    "The user declined to run this tool. It was not run. Do not retry it unless the "
    "user asks again."
)
NOT_ANSWERED = "The user did not approve this tool in time. It was not run."
ANOTHER_DECISION_PENDING = (
    "Another action is already waiting for the user's decision, so this call was not run "
    "and the user was not asked about it. Wait for that decision, then call it again if "
    "it is still needed."
)
CLIENT_CANNOT_APPROVE = (
    "This tool needs the user's approval before it runs, and this client cannot ask "
    "for it. It was not run. Tell the user the action needs approval in a client that "
    "supports it."
)


def split_approval(mcp_servers: Any) -> Tuple[Any, Dict[str, frozenset]]:
    """Strip the gateway-only ``approvalTools`` key; return (servers for the CLI, rules).

    Same contract as ``mcp_progress_relay.split_read_only``: always called before the
    config reaches the CLI. A malformed value gates **every** tool of the server rather
    than none — an operator who meant to require approval must not lose it to a typo.
    """
    if not isinstance(mcp_servers, dict):
        return mcp_servers, {}
    rules: Dict[str, frozenset] = {}
    cleaned: Dict[str, Any] = {}
    for name, config in mcp_servers.items():
        if not isinstance(config, dict) or APPROVAL_KEY not in config:
            cleaned[name] = config
            continue
        value = config[APPROVAL_KEY]
        cleaned[name] = {k: v for k, v in config.items() if k != APPROVAL_KEY}
        if isinstance(value, str) and value == "*":
            rules[name] = frozenset({"*"})
        elif (
            isinstance(value, list)
            and value
            and all(isinstance(v, str) and v and v != "*" for v in value)
        ):
            rules[name] = frozenset(value)
        else:
            logger.warning(
                'MCP server %r: %s must be "*" or a list of tool names; every tool of '
                "the server now needs approval",
                name,
                APPROVAL_KEY,
            )
            rules[name] = frozenset({"*"})
    return cleaned, rules


@dataclass(frozen=True)
class ApprovalPolicy:
    """Which tool calls need a human decision, by server (operator config)."""

    rules: Mapping[str, frozenset]
    read_only: Mapping[str, frozenset]

    def target(self, tool_name: str) -> Optional[Tuple[str, str]]:
        """``(server, tool)`` when ``tool_name`` needs approval, else None.

        CLI tool names are ``mcp__<server>__<tool>`` with the server name verbatim; the
        longest matching configured server wins (``a`` vs ``a__b``)."""
        if not tool_name.startswith("mcp__"):
            return None
        match: Optional[Tuple[str, str]] = None
        for server in self.rules:
            prefix = f"mcp__{server}__"
            if tool_name.startswith(prefix) and len(tool_name) > len(prefix):
                if match is None or len(server) > len(match[0]):
                    match = (server, tool_name[len(prefix) :])
        if match is None:
            return None
        server, tool = match
        gated = self.rules[server]
        if "*" in gated:
            read_only = self.read_only.get(server, frozenset())
            return None if tool in read_only and "*" not in read_only else match
        return match if tool in gated else None

    def __bool__(self) -> bool:
        return bool(self.rules)


def args_digest(arguments: Any) -> str:
    """Canonical digest of the arguments the CLI will run with (what the user approves)."""
    canonical = json.dumps(
        arguments,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def approval_payload(server: str, tool: str, digest: str) -> Dict[str, Any]:
    """The explicit ``approval`` object on the parked ``function_call`` item."""
    return {
        "kind": APPROVAL_KIND,
        "version": APPROVAL_VERSION,
        "server": server,
        "tool": tool,
        "argsDigest": digest,
    }


def client_supports_approval(header_value: Optional[str]) -> bool:
    """Whether the request declared ``tool-approval`` in ``X-Gateway-Capabilities``."""
    if not header_value:
        return False
    tokens = {part.strip().lower() for part in header_value.split(",")}
    return CAPABILITY in tokens


def is_approved(output: Optional[str], digest: str) -> bool:
    """Only ``{"decision": "approve", "argsDigest": <digest>}`` approves; anything else denies."""
    if not output:
        return False
    try:
        parsed = json.loads(output)
    except ValueError:
        return False
    return (
        isinstance(parsed, dict)
        and parsed.get("decision") == "approve"
        and parsed.get("argsDigest") == digest
    )
