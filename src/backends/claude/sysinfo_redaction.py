"""Redact host-identifying values and secrets from what the model and clients see.

The agent runs tools on the gateway host, so a tool result can carry the
host's name, its interface addresses, the container id, or a secret from the
gateway's environment. This module builds one redaction table at first use
and applies it in two places:

* a ``PostToolUse`` hook that replaces a tool's **successful** result before
  the CLI hands it to the model (``updatedToolOutput``; pinned against the
  bundled CLI in ``tests/test_cli_sysinfo_redaction.py``);
* the gateway's outbound stream (``StreamRedactor``), so text the client
  receives is filtered too — including streamed deltas, where a value can be
  split across chunks.

Known limit, pinned by the same test: the CLI routes a **failed** tool call
(e.g. a Bash command with a non-zero exit) through ``PostToolUseFailure``,
which cannot rewrite the result, so the model sees that output unfiltered.
The outbound filter still applies to what reaches the client. The boundary
that holds regardless is the deployment itself: a neutral container hostname
and no host networking.

Configuration (env):

* ``SYSINFO_REDACTION`` — on by default; ``false`` disables both layers.
* ``SYSINFO_REDACT_PRIVATE_IPS`` — on by default; redact RFC 1918 / CGNAT /
  link-local IPv4 and ULA / link-local IPv6 addresses by pattern.
* ``SYSINFO_REDACT_VALUES`` — comma-separated extra literals. Any literal
  (these or a secret env value) longer than ``MAX_LITERAL_LEN`` is dropped
  with a warning: the stream carry holds back the longest literal's length.
* ``SYSINFO_REDACT_PATTERNS`` — extra regexes, separated by ``;;``. An
  arbitrary regex has no static maximum length and may span whitespace, so
  with one configured streamed text is released per text block (at its end)
  instead of incrementally.
* ``SYSINFO_REDACT_DOMAINS`` — comma-separated domain suffixes; any host name
  under one is redacted.
* ``SYSINFO_REDACT_ALLOW`` — comma-separated literals never redacted (e.g. a
  deliberately neutral hostname that is also a common word).
"""

from __future__ import annotations

import ipaddress
import logging
import os
import re
import socket
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from src.env_utils import parse_bool_env

logger = logging.getLogger(__name__)

PLACEHOLDER = "[REDACTED:{label}]"
# Literals shorter than this are too likely to collide with ordinary text.
MIN_LITERAL_LEN = 4
# Secret env values shorter than this are not worth treating as secrets.
MIN_SECRET_LEN = 8
# Supported ceiling for one literal: the stream carry holds back this many
# characters, so a longer value is dropped (with a warning) rather than
# silently streamed in pieces.
MAX_LITERAL_LEN = 65536
# How far each built-in pattern can reach from where its match starts — the
# match plus its trailing lookaround — which the stream carry must keep of a
# whitespace-free run. These are properties of the REGEXES below, which are
# bounded on purpose (not just of the protocols): IPv4 15 + 1, IPv6 capped at
# 40 (the longest textual address is 39) + 1, and a domain match is confined
# to a run of at most 253 name characters (the DNS limit) + 1.
_IPV4_MAX = 16
_IPV6_MAX = 41
_DOMAIN_MAX = 254

_SECRET_NAME_RE = re.compile(
    r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|PRIVATE|COOKIE|DSN)",
    re.IGNORECASE,
)
# Env names whose values are configuration, not secrets, despite matching above.
_SECRET_NAME_EXCEPTIONS = frozenset({"SSH_AUTH_SOCK", "GPG_AGENT_INFO"})
_URL_USERINFO_RE = re.compile(
    r"^[a-z][a-z0-9+.-]*://[^/\s:@]+:[^/\s@]+@", re.IGNORECASE
)

# Gateway-only secrets the CLI child never needs. Masked (set to "") in the
# child env so a shell inside the agent cannot read them at all.
DEFAULT_CHILD_ENV_MASK = ("ADMIN_API_KEY", "API_KEY")

_ASCII_HOST_CHAR = r"A-Za-z0-9_\-"
# Characters any built-in match may contain; a cut between two others is safe.
_HOST_CHAR_RE = re.compile(r"[A-Za-z0-9_.:-]")
_PRIVATE_IPV4 = (
    r"(?<![0-9.])(?:"
    r"10(?:\.(?:25[0-5]|2[0-4]\d|1?\d?\d)){3}"
    r"|172\.(?:1[6-9]|2\d|3[01])(?:\.(?:25[0-5]|2[0-4]\d|1?\d?\d)){2}"
    r"|192\.168(?:\.(?:25[0-5]|2[0-4]\d|1?\d?\d)){2}"
    r"|100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])(?:\.(?:25[0-5]|2[0-4]\d|1?\d?\d)){2}"
    r"|169\.254(?:\.(?:25[0-5]|2[0-4]\d|1?\d?\d)){2}"
    r")(?![0-9])"
)
_PRIVATE_IPV6 = (
    r"(?<![0-9A-Fa-f:])(?i:f[cd][0-9a-f]{2}|fe[89ab][0-9a-f])"
    r":[0-9A-Fa-f:]{0,34}[0-9A-Fa-f](?![0-9A-Fa-f:])"
)


def redaction_enabled() -> bool:
    """``SYSINFO_REDACTION`` (default on), overridable from the admin console."""
    try:
        from src.runtime_config import runtime_config
    except Exception:  # pragma: no cover - import cycle guard during startup
        return parse_bool_env("SYSINFO_REDACTION", "true")
    return bool(runtime_config.get("sysinfo_redaction_enabled"))


def _csv_env(name: str) -> List[str]:
    return [part.strip() for part in os.getenv(name, "").split(",") if part.strip()]


def child_env_mask() -> Dict[str, str]:
    """Env overrides that blank gateway-only secrets in the CLI child."""
    if not redaction_enabled():
        return {}
    names = list(DEFAULT_CHILD_ENV_MASK) + _csv_env("SYSINFO_CHILD_ENV_MASK")
    return {name: "" for name in dict.fromkeys(names)}


@dataclass(frozen=True)
class _Literal:
    value: str
    label: str
    bounded: bool  # host-like: must not sit inside a longer ASCII host token


def _read(path: str) -> str:
    try:
        return Path(path).read_text(errors="replace")
    except OSError:
        return ""


def _local_ips() -> List[str]:
    """Addresses bound on this host, from /proc and the resolver."""
    found: List[str] = []
    # IPv4: /proc/net/fib_trie lists "/32 host LOCAL" right after each address.
    lines = _read("/proc/net/fib_trie").splitlines()
    for prev, line in zip(lines, lines[1:]):
        if "/32 host LOCAL" in line:
            parts = prev.strip().split()
            if parts:
                found.append(parts[-1])
    for line in _read("/proc/net/if_inet6").splitlines():
        parts = line.split()
        if parts and len(parts[0]) == 32:
            raw = parts[0]
            found.append(
                str(
                    ipaddress.IPv6Address(
                        ":".join(raw[i : i + 4] for i in range(0, 32, 4))
                    )
                )
            )
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            found.append(info[4][0])
    except OSError:
        pass
    out = []
    for raw in found:
        try:
            ip = ipaddress.ip_address(raw.split("%")[0])
        except ValueError:
            continue
        if ip.is_loopback or ip.is_unspecified:
            continue
        out.append(str(ip))
    return out


def _host_names() -> List[str]:
    names = [socket.gethostname(), _read("/etc/hostname").strip()]
    try:
        names.append(socket.getfqdn())
    except OSError:
        pass
    short = [name.split(".")[0] for name in names if "." in name]
    return [
        n
        for n in names + short
        if n and n != "localhost" and not n.startswith("localhost.")
    ]


def _container_ids() -> List[str]:
    text = _read("/proc/self/cgroup") + _read("/proc/self/mountinfo")
    ids = set(re.findall(r"(?<![0-9a-f])[0-9a-f]{64}(?![0-9a-f])", text))
    return sorted(ids) + sorted({cid[:12] for cid in ids})


def _mac_addresses() -> List[str]:
    out = []
    try:
        entries = list(Path("/sys/class/net").iterdir())
    except OSError:
        return out
    for entry in entries:
        mac = _read(str(entry / "address")).strip()
        if mac and mac != "00:00:00:00:00:00":
            out.append(mac)
    return out


def _secret_env_values(env: Dict[str, str]) -> List[str]:
    out = []
    for name, value in env.items():
        value = (value or "").strip()
        if len(value) < MIN_SECRET_LEN:
            continue
        if name.upper() in _SECRET_NAME_EXCEPTIONS:
            continue
        if _SECRET_NAME_RE.search(name) or _URL_USERINFO_RE.match(value):
            out.append(value)
    return out


class Redactor:
    """Literal + pattern redaction over text (and nested values)."""

    def __init__(
        self, literals: Iterable[_Literal], patterns: Iterable[Tuple[Any, ...]]
    ):
        """*patterns* are ``(regex, label)`` or ``(regex, label, max_len)``.

        ``max_len`` is the longest text the pattern can match; without it the
        pattern is treated as unbounded, and streamed text is then held back
        until its block ends (see ``_TextCarry``).
        """
        allow = set(_csv_env("SYSINFO_REDACT_ALLOW"))
        seen: Dict[str, _Literal] = {}
        for lit in literals:
            if len(lit.value) < MIN_LITERAL_LEN or lit.value in allow:
                continue
            if len(lit.value) > MAX_LITERAL_LEN:
                logger.warning(
                    "Ignoring a %s redaction value longer than %d characters",
                    lit.label,
                    MAX_LITERAL_LEN,
                )
                continue
            seen.setdefault(lit.value, lit)
        self._labels: Dict[str, str] = {}
        parts: List[str] = []
        # Longest first so a full value wins over its own prefix.
        for index, lit in enumerate(
            sorted(seen.values(), key=lambda item: -len(item.value))
        ):
            group = f"l{index}"
            body = re.escape(lit.value)
            if lit.bounded:
                # Host names, addresses and ids are case-insensitive on the wire.
                body = f"(?<![{_ASCII_HOST_CHAR}])(?i:{body})(?![{_ASCII_HOST_CHAR}])"
            parts.append(f"(?P<{group}>{body})")
            self._labels[group] = lit.label
        self.unbounded_patterns = False
        max_pattern_len = 0
        for index, spec in enumerate(patterns):
            regex, label = spec[0], spec[1]
            max_len = spec[2] if len(spec) > 2 else None
            try:
                re.compile(regex)
            except re.error as exc:
                logger.warning(
                    "Ignoring invalid SYSINFO redaction pattern %r: %s", regex, exc
                )
                continue
            group = f"p{index}"
            # Wrapped in its own group: a custom pattern's inner groups never
            # decide the label (see ``_sub``).
            parts.append(f"(?P<{group}>{regex})")
            self._labels[group] = label
            if max_len is None:
                self.unbounded_patterns = True
            else:
                max_pattern_len = max(max_pattern_len, max_len)
        self.literal_count = len(seen)
        self.pattern_count = sum(1 for g in self._labels if g.startswith("p"))
        self._allow = allow
        self._regex = re.compile("|".join(parts)) if parts else None
        self.max_literal_len = max((len(v) for v in seen), default=0)
        # How much of a whitespace-free run the stream carry must keep so that
        # no match still being written can start before a forced cut.
        self.stream_keep = max(256, self.max_literal_len, max_pattern_len)
        self.max_ws_literal_len = max(
            (len(v) for v in seen if any(c.isspace() for c in v)), default=0
        )

    @property
    def active(self) -> bool:
        return self._regex is not None

    def _sub(self, match: "re.Match[str]") -> str:
        if match.group(0) in self._allow:
            return match.group(0)
        for group, label in self._labels.items():
            if match.group(group) is not None:
                return PLACEHOLDER.format(label=label)
        return PLACEHOLDER.format(label="custom")

    def redact_text(self, text: str) -> str:
        if self._regex is None or not text:
            return text
        return self._regex.sub(self._sub, text)

    def spans(self, text: str) -> List[Tuple[int, int]]:
        if self._regex is None:
            return []
        return [
            m.span()
            for m in self._regex.finditer(text)
            if m.group(0) not in self._allow
        ]

    def redact_value(self, value: Any, _depth: int = 0) -> Any:
        """Redact every string inside *value*; returns a new value.

        Dicts, lists and tuples are rebuilt; other objects with a ``__dict__``
        (SDK dataclasses) are updated in place and returned.
        """
        if _depth > 64:
            return value
        if isinstance(value, str):
            return self.redact_text(value)
        if isinstance(value, dict):
            return {k: self.redact_value(v, _depth + 1) for k, v in value.items()}
        if isinstance(value, list):
            return [self.redact_value(v, _depth + 1) for v in value]
        if isinstance(value, tuple):
            return tuple(self.redact_value(v, _depth + 1) for v in value)
        if hasattr(value, "__dict__") and not isinstance(value, type):
            for key, item in list(vars(value).items()):
                if key.startswith("_") or callable(item):
                    continue
                new = self.redact_value(item, _depth + 1)
                if new is not item:
                    try:
                        setattr(value, key, new)
                    except (AttributeError, TypeError):
                        # Frozen dataclass: still never let the raw value out.
                        try:
                            object.__setattr__(value, key, new)
                        except (AttributeError, TypeError):
                            pass
            return value
        return value


def _builtin_patterns() -> List[Tuple[Any, ...]]:
    """Domain suffixes and private ranges, each with the reach it is bounded to."""
    patterns: List[Tuple[Any, ...]] = []
    for suffix in _csv_env("SYSINFO_REDACT_DOMAINS"):
        suffix = suffix.lstrip(".")
        patterns.append(
            (
                # The lookahead confines the match to a name run of at most 253
                # characters, so _DOMAIN_MAX really bounds what it can reach.
                rf"(?<![{_ASCII_HOST_CHAR}.])"
                r"(?=[A-Za-z0-9.-]{1,253}(?![A-Za-z0-9.-]))"
                rf"(?:[A-Za-z0-9-]+\.)+{re.escape(suffix)}(?![{_ASCII_HOST_CHAR}])",
                "domain",
                _DOMAIN_MAX,
            )
        )
    if parse_bool_env("SYSINFO_REDACT_PRIVATE_IPS", "true"):
        patterns.append((_PRIVATE_IPV4, "ip", _IPV4_MAX))
        patterns.append((_PRIVATE_IPV6, "ip", _IPV6_MAX))
    return patterns


def build_redactor(env: Optional[Dict[str, str]] = None) -> Redactor:
    env = dict(os.environ if env is None else env)
    literals: List[_Literal] = []
    literals += [_Literal(v, "hostname", True) for v in _host_names()]
    literals += [_Literal(v, "ip", True) for v in _local_ips()]
    literals += [_Literal(v, "container-id", True) for v in _container_ids()]
    literals += [_Literal(v, "mac", True) for v in _mac_addresses()]
    literals += [_Literal(v, "secret", False) for v in _secret_env_values(env)]
    literals += [
        _Literal(v, "custom", False) for v in _csv_env("SYSINFO_REDACT_VALUES")
    ]

    patterns: List[Tuple[Any, ...]] = _builtin_patterns()
    for regex in os.getenv("SYSINFO_REDACT_PATTERNS", "").split(";;"):
        if regex.strip():
            # Unbounded: an arbitrary regex may span whitespace and has no
            # static maximum length, so streamed text is held per block.
            patterns.append((regex.strip(), "custom"))
    return Redactor(literals, patterns)


_lock = threading.Lock()
_cached: Optional[Redactor] = None


def get_redactor() -> Optional[Redactor]:
    """The process-wide redactor, or None when redaction is disabled."""
    global _cached
    if not redaction_enabled():
        return None
    with _lock:
        if _cached is None:
            _cached = build_redactor()
            logger.info(
                "sysinfo redaction active: %d literal(s), %d pattern(s)",
                _cached.literal_count,
                _cached.pattern_count,
            )
        return _cached


def reset_redactor_cache() -> None:
    global _cached
    with _lock:
        _cached = None


def make_redaction_post_tool_use_hook(redactor: Redactor) -> Callable[..., Any]:
    """PostToolUse hook: replace a successful tool result when it carries a match."""

    async def hook(
        input_data: Dict[str, Any], tool_use_id: Any, context: Any
    ) -> Dict[str, Any]:
        response = input_data.get("tool_response")
        if response is None:
            return {}
        redacted = redactor.redact_value(response)
        # Never return an identity rewrite: hooks run in parallel and an
        # unchanged copy could clobber another hook's real rewrite.
        if redacted == response:
            return {}
        return {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "updatedToolOutput": redacted,
            }
        }

    return hook


class _TextCarry:
    """Holds back the tail of a streamed text block that may still grow a match.

    Invariant: the concatenation of everything ``feed``/``flush`` return equals
    ``redact_text`` of the whole block. A cut is safe when no match in the
    buffer straddles it and no match still being written can start before it.
    Built-in patterns never contain whitespace, so cutting just after the last
    whitespace guarantees the second condition; a literal that does contain
    whitespace additionally holds back its own length. A whitespace-free run
    longer than ``keep + 256`` is cut anyway (CJK text, base64) keeping
    ``keep`` characters — ``Redactor.stream_keep``, at least the longest
    literal and the longest built-in match, so a value still being written
    always lies inside the kept tail.

    A custom ``SYSINFO_REDACT_PATTERNS`` regex has no static maximum length and
    may span whitespace, so with one configured nothing is released before the
    block ends: the text arrives in one piece at ``flush`` (streaming latency
    is the price of an arbitrary pattern).
    """

    def __init__(self, redactor: Redactor) -> None:
        self._r = redactor
        self._buf = ""
        self._keep = redactor.stream_keep
        self._max_run = self._keep + 256

    def feed(self, text: str) -> str:
        self._buf += text
        if self._r.unbounded_patterns:
            return ""
        cut = self._safe_cut(self._buf)
        if cut <= 0:
            return ""
        out, self._buf = self._buf[:cut], self._buf[cut:]
        return self._r.redact_text(out)

    @staticmethod
    def _after_last_space(buf: str, upto: int) -> int:
        for i in range(min(upto, len(buf)) - 1, -1, -1):
            if buf[i].isspace():
                return i + 1
        return 0

    @staticmethod
    def _splittable(buf: str, p: int) -> bool:
        """A cut at *p* cannot change any match's lookaround context.

        Every built-in lookaround only inspects host characters, so a cut with
        a non-host character on either side reads the same alone as in place.
        """
        if p <= 0 or p >= len(buf):
            return True
        return not _HOST_CHAR_RE.match(buf[p - 1]) or not _HOST_CHAR_RE.match(buf[p])

    def _soft_boundary(self, buf: str, upto: int) -> int:
        """Last splittable position <= *upto*, searched over a bounded window.

        Past the window (a huge run of host characters) the cut stays at
        *upto*: splitting a match's context there can only add a placeholder,
        never let a value through, because every match still being written
        starts within the kept tail.
        """
        # Inclusive of 0: the buffer start is always a safe cut (hold it all).
        for p in range(upto, max(upto - 1024, 0) - 1, -1):
            if _TextCarry._splittable(buf, p):
                return p
        return upto

    def _safe_cut(self, buf: str) -> int:
        cut = self._after_last_space(buf, len(buf))
        forced = len(buf) - cut > self._max_run
        if forced:
            cut = self._soft_boundary(buf, len(buf) - self._keep)
        hold = self._r.max_ws_literal_len
        if hold and cut > len(buf) - hold:
            cut = self._after_last_space(buf, len(buf) - hold)
        spans = self._r.spans(buf)
        moved = True
        while moved and cut > 0:
            moved = False
            for start, end in spans:
                if start < cut < end:
                    cut = start if forced else self._after_last_space(buf, start)
                    moved = True
                    break
            if forced and not self._splittable(buf, cut):
                new = self._soft_boundary(buf, cut)
                moved = moved or new != cut
                cut = new
        return cut

    def flush(self) -> str:
        out, self._buf = self._buf, ""
        return self._r.redact_text(out)


_DELTA_TEXT_FIELD = {"text_delta": "text", "thinking_delta": "thinking"}


class StreamRedactor:
    """Per-turn redaction of converted SDK messages on the way to the client."""

    def __init__(self, redactor: Redactor) -> None:
        self._r = redactor
        self._carry: Dict[Tuple[Any, Any], Tuple[_TextCarry, str]] = {}

    def process(self, chunk: Any) -> List[Any]:
        """Return the chunk(s) to emit in place of *chunk*."""
        if not isinstance(chunk, dict):
            return [self._r.redact_value(chunk)]
        if chunk.get("type") == "stream_event" and isinstance(chunk.get("event"), dict):
            return self._process_event(chunk)
        return [self._r.redact_value(chunk)]

    def _process_event(self, chunk: Dict[str, Any]) -> List[Any]:
        event = chunk["event"]
        key = (chunk.get("parent_tool_use_id"), event.get("index"))
        etype = event.get("type")
        if etype == "content_block_delta" and isinstance(event.get("delta"), dict):
            delta = event["delta"]
            field = _DELTA_TEXT_FIELD.get(delta.get("type"))
            if field and isinstance(delta.get(field), str):
                carry, _ = self._carry.setdefault(
                    key, (_TextCarry(self._r), delta["type"])
                )
                emitted = carry.feed(delta[field])
                new = dict(chunk)
                new["event"] = {**event, "delta": {**delta, field: emitted}}
                return [new]
        if etype == "content_block_stop":
            pending = self._carry.pop(key, None)
            out: List[Any] = []
            if pending is not None:
                rest = pending[0].flush()
                if rest:
                    out.append(
                        self._synthetic_delta(
                            chunk, event.get("index"), pending[1], rest
                        )
                    )
            out.append(self._r.redact_value(chunk))
            return out
        if etype in ("message_start", "message_stop"):
            out = self._flush_parent(chunk)
            out.append(self._r.redact_value(chunk))
            return out
        return [self._r.redact_value(chunk)]

    def _flush_parent(self, chunk: Dict[str, Any]) -> List[Any]:
        parent = chunk.get("parent_tool_use_id")
        out = []
        for key in [k for k in self._carry if k[0] == parent]:
            carry, delta_type = self._carry.pop(key)
            rest = carry.flush()
            if rest:
                out.append(self._synthetic_delta(chunk, key[1], delta_type, rest))
        return out

    def flush_all(self) -> List[Any]:
        out = []
        for key, (carry, delta_type) in list(self._carry.items()):
            rest = carry.flush()
            if rest:
                out.append(
                    self._synthetic_delta(
                        {"type": "stream_event", "parent_tool_use_id": key[0]},
                        key[1],
                        delta_type,
                        rest,
                    )
                )
        self._carry.clear()
        return out

    @staticmethod
    def _synthetic_delta(
        base: Dict[str, Any], index: Any, delta_type: str, text: str
    ) -> Dict[str, Any]:
        new = {k: v for k, v in base.items() if k != "event"}
        new["type"] = "stream_event"
        new["event"] = {
            "type": "content_block_delta",
            "index": index,
            "delta": {"type": delta_type, _DELTA_TEXT_FIELD[delta_type]: text},
        }
        return new
