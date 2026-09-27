"""Skill-name rules shared by the Claude backend and the Responses route.

claude-agent-sdk 0.2.129+ validates every ``ClaudeAgentOptions.skills`` entry
while it builds the CLI command, and raises ``ValueError`` out of ``connect()``
for a name it rejects. By then the gateway has committed to creating the
session, so the request failed as a "backend unavailable, retry shortly" 503
that no retry could ever fix. The gateway applies the same rules earlier, and
per source:

* a request ``Skill(<name>)`` rule naming an unusable skill is a 400
  (:func:`invalid_skill_rule`), checked before any client is created or updated;
* a catalog-derived name the SDK would reject — a discovered skill directory
  such as ``weird (v2)``, or an MCP prompt command such as
  ``server:prompt (MCP)`` — is left out of the allowlist instead
  (:func:`skill_name_problem`).

The rules are a copy of the SDK's private
``claude_agent_sdk._internal.transport.subprocess_cli._validate_skill_name``;
``tests/test_skill_name_validation.py`` pins the copy against the installed SDK
so a rule change there fails loudly.
"""

from __future__ import annotations

import re
from typing import Iterable, Optional, Tuple

# Granular per-skill rule: ``Skill(<name>)`` / ``Skill(<name>:*)``. The name may
# be bare (``summarize``) or plugin-qualified (``docs-helper:summarize``) — the
# form the CLI actually registers a plugin skill under — but must not START with
# ``:`` so the ``Skill(:*)`` catch-all never matches.
GRANULAR_SKILL_RULE_RE = re.compile(r"^Skill\((?P<name>[^:)][^)]*?)(?::\*)?\)$")

# ``Skill(*)`` / ``Skill(*:*)`` read as "every skill", so the gateway treats them
# as a bare ``Skill`` entry. Passed through, the SDK would reject ``*`` as a
# skills name (it wants ``skills="all"``).
SKILL_WILDCARD_RULES = frozenset({"Skill(*)", "Skill(*:*)"})

# Parentheses and commas delimit rules in the CLI's --allowedTools tokenizer;
# control characters (C0, DEL, C1) never appear in a skill directory name; the
# CLI trims U+FEFF as whitespace but Python's str.strip() does not.
_INVALID_CHARS_RE = re.compile(r"[(),\x00-\x1f\x7f-\x9f﻿]")

# Every surrogate in a Python str is unpaired: a well-formed astral character
# is a single code point.
_SURROGATE_RE = re.compile("[\ud800-\udfff]")


def skill_name_problem(name: object) -> Optional[str]:
    """Why the SDK would reject *name* as a ``skills`` entry, or None if it would not.

    Same checks, same order as the SDK's ``_validate_skill_name``.
    """
    if not isinstance(name, str):
        return "skill names must be strings"
    if not name.strip():
        return "skill names must be non-empty"
    if _SURROGATE_RE.search(name):
        return "contains a surrogate code point"
    if name != name.strip():
        return "has leading or trailing whitespace"
    if _INVALID_CHARS_RE.search(name):
        return "contains parentheses, commas, control characters or a byte-order mark"
    if name == "*":
        return "'*' is not a skill name"
    if name.endswith(":*") or name.endswith(" *"):
        return "wildcard-suffix names are not allowed"
    if name.startswith("/"):
        return "skill names may not start with '/'"
    if "\\\\" in name:
        return "contains consecutive backslashes"
    if name.endswith("\\"):
        return "ends with an unpaired backslash"
    return None


def invalid_skill_rule(tools: Optional[Iterable[object]]) -> Optional[Tuple[str, str]]:
    """First ``Skill(<name>)`` / ``Skill(<name>:*)`` rule naming an unusable skill.

    Returns ``(rule, reason)``, or None when every such rule is usable. The
    name is everything between the outer parentheses, so ``Skill(weird (v2))``
    is caught too although :data:`GRANULAR_SKILL_RULE_RE` cannot parse it.
    :data:`SKILL_WILDCARD_RULES` mean "every skill"; a name starting with ``:``
    (the ``Skill(:*)`` catch-all) is never a skill name; other entries are not
    this check's business.
    """
    for rule in tools or ():
        if not isinstance(rule, str) or rule in SKILL_WILDCARD_RULES:
            continue
        if not (rule.startswith("Skill(") and rule.endswith(")")):
            continue
        name = rule[len("Skill(") : -1]
        if name.startswith(":"):
            continue
        if name.endswith(":*"):
            name = name[: -len(":*")]
        problem = skill_name_problem(name)
        if problem is not None:
            return rule, problem
    return None
