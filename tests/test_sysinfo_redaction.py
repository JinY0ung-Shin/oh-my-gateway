"""Unit tests for host/secret redaction (``src/backends/claude/sysinfo_redaction``).

The bundled-CLI half (the PostToolUse rewrite really reaches the model) lives
in ``tests/test_cli_sysinfo_redaction.py``. Here: what gets collected, what a
match is (and is not), the streamed-delta carry invariant under random
chunking, and the gateway wiring (hooks, child env, both outbound paths).
"""

from __future__ import annotations

import dataclasses
import os
import random
import re
from types import SimpleNamespace
from typing import Any, Dict, List
from unittest.mock import patch

import pytest

from src.backends.claude import sysinfo_redaction as sr
from src.backends.claude.sysinfo_redaction import (
    Redactor,
    StreamRedactor,
    _Literal,
    _TextCarry,
)

HOST = "gw-prod-01"
SECRET = "sk-live-9f8e7d6c5b4a"


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    for name in (
        "SYSINFO_REDACT_VALUES",
        "SYSINFO_REDACT_PATTERNS",
        "SYSINFO_REDACT_DOMAINS",
        "SYSINFO_REDACT_ALLOW",
        "SYSINFO_REDACT_PRIVATE_IPS",
        "SYSINFO_CHILD_ENV_MASK",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SYSINFO_REDACTION", "true")
    sr.reset_redactor_cache()
    yield
    sr.reset_redactor_cache()


def _redactor(**extra) -> Redactor:
    literals = [
        _Literal(HOST, "hostname", True),
        _Literal("10.20.30.40", "ip", True),
        _Literal("3f2a9c1b7d4e", "container-id", True),
        _Literal("02:42:ac:11:00:02", "mac", True),
        _Literal(SECRET, "secret", False),
        _Literal("pass phrase with spaces", "secret", False),
    ] + extra.get("literals", [])
    patterns = [
        (sr._PRIVATE_IPV4, "ip", sr._IPV4_MAX),
        (sr._PRIVATE_IPV6, "ip", sr._IPV6_MAX),
    ] + extra.get("patterns", [])
    return Redactor(literals, patterns)


def _host_only() -> Dict[str, Any]:
    """Patch the host probes so build_redactor sees a known machine."""
    return {
        "_host_names": lambda: [HOST + ".corp.example", HOST],
        "_local_ips": lambda: ["10.20.30.40", "2001:db8::77"],
        "_container_ids": lambda: ["3f2a9c1b7d4e"],
        "_mac_addresses": lambda: ["02:42:ac:11:00:02"],
    }


def _build(env=None):
    with patch.multiple(sr, **_host_only()):
        return sr.build_redactor(env or {})


# ---------------------------------------------------------------------------
# What a match is
# ---------------------------------------------------------------------------


class TestMatching:
    @pytest.mark.parametrize(
        "text,expected",
        [
            (f"host {HOST} up", "host [REDACTED:hostname] up"),
            (f"{HOST.upper()} is up", "[REDACTED:hostname] is up"),
            (f"{HOST}에서 실행", "[REDACTED:hostname]에서 실행"),
            (f"({HOST}).", "([REDACTED:hostname])."),
            (f"https://{HOST}:8080/x", "https://[REDACTED:hostname]:8080/x"),
            (f"user@{HOST}", "user@[REDACTED:hostname]"),
            ("ip=10.20.30.40;", "ip=[REDACTED:ip];"),
            ("ether 02:42:AC:11:00:02 txq", "ether [REDACTED:mac] txq"),
            ("docker/3f2a9c1b7d4e/x", "docker/[REDACTED:container-id]/x"),
            (f"Bearer {SECRET}", "Bearer [REDACTED:secret]"),
            (f"x{SECRET}y", "x[REDACTED:secret]y"),
            ("pass phrase with spaces!", "[REDACTED:secret]!"),
        ],
    )
    def test_redacts(self, text, expected):
        assert _redactor().redact_text(text) == expected

    @pytest.mark.parametrize(
        "text",
        [
            f"{HOST}2",  # a different, longer host
            f"x{HOST}",
            f"{HOST}-canary",
            f"{HOST}_old",
            "a3f2a9c1b7d4e",
            "10.20.30.401",
            "127.0.0.1 localhost",
            "8.8.8.8",
            "172.15.0.1 and 172.32.0.1",
            "192.169.1.1",
            "100.63.0.1 and 100.128.0.1",
            "version 1.10.0.12",
            "sk-live-9f8e7d6c5b4",  # a strict prefix of the secret
            SECRET.upper(),  # secrets are case-sensitive
            "pass phrase with  spaces",
        ],
    )
    def test_leaves_alone(self, text):
        assert _redactor().redact_text(text) == text

    @pytest.mark.parametrize(
        "ip",
        [
            "10.0.0.1",
            "10.255.255.255",
            "172.16.5.4",
            "172.31.255.1",
            "192.168.0.10",
            "100.64.0.1",
            "100.127.255.254",
            "169.254.169.254",
            "fd12:3456:789a::1",
            "fe80::1ff:fe23:4567:890a",
        ],
    )
    def test_private_ranges_by_pattern(self, ip):
        assert _redactor().redact_text(f"at {ip}.") == "at [REDACTED:ip]."

    def test_private_ranges_can_be_turned_off(self, monkeypatch):
        monkeypatch.setenv("SYSINFO_REDACT_PRIVATE_IPS", "false")
        r = _build()
        assert r.redact_text("192.168.0.10") == "192.168.0.10"
        assert r.redact_text("10.20.30.40") == "[REDACTED:ip]"  # own address stays

    def test_longest_literal_wins(self):
        r = Redactor(
            [_Literal("gw-prod", "hostname", True), _Literal("gw-prod.corp.example", "hostname", True)],
            [],
        )
        assert r.redact_text("gw-prod.corp.example") == "[REDACTED:hostname]"

    def test_allow_list_and_min_length(self, monkeypatch):
        monkeypatch.setenv("SYSINFO_REDACT_ALLOW", "gateway")
        r = Redactor(
            [_Literal("gateway", "hostname", True), _Literal("abc", "hostname", True)],
            [],
        )
        assert not r.active
        assert r.redact_text("gateway abc") == "gateway abc"

    def test_allow_list_beats_a_pattern(self, monkeypatch):
        monkeypatch.setenv("SYSINFO_REDACT_ALLOW", "192.168.0.1")
        r = _redactor()
        assert r.redact_text("192.168.0.1 192.168.0.2") == "192.168.0.1 [REDACTED:ip]"

    def test_custom_values_patterns_domains(self, monkeypatch):
        monkeypatch.setenv("SYSINFO_REDACT_VALUES", "Project-Nebula, ")
        monkeypatch.setenv("SYSINFO_REDACT_PATTERNS", r"EMP-\d{6};;(?P<x>bad(;;[")
        monkeypatch.setenv("SYSINFO_REDACT_DOMAINS", "corp.internal,.lab.example")
        r = _build()
        out = r.redact_text(
            "Project-Nebula EMP-123456 db1.corp.internal a.b.lab.example corp.internal"
        )
        assert out == (
            "[REDACTED:custom] [REDACTED:custom] [REDACTED:domain] "
            "[REDACTED:domain] corp.internal"
        )

    def test_custom_pattern_with_named_groups_keeps_its_label(self, monkeypatch):
        monkeypatch.setenv("SYSINFO_REDACT_PATTERNS", r"(?P<emp>EMP)-(?P<num>\d+)")
        assert _build().redact_text("EMP-42") == "[REDACTED:custom]"

    def test_placeholders_are_stable_under_reredaction(self):
        r = _redactor()
        once = r.redact_text(f"{HOST} 10.0.0.1 {SECRET}")
        assert r.redact_text(once) == once


# ---------------------------------------------------------------------------
# What gets collected
# ---------------------------------------------------------------------------


class TestCollection:
    def test_host_probe_values_are_redacted(self):
        r = _build()
        text = f"{HOST}.corp.example {HOST} 2001:db8::77 3f2a9c1b7d4e 02:42:ac:11:00:02"
        assert r.redact_text(text) == (
            "[REDACTED:hostname] [REDACTED:hostname] [REDACTED:ip] "
            "[REDACTED:container-id] [REDACTED:mac]"
        )

    def test_secret_env_values(self):
        env = {
            "ANTHROPIC_AUTH_TOKEN": "tok-aaaaaaaaaaaa",
            "ADMIN_API_KEY": "admin-bbbbbbbbbbb",
            "DB_PASSWORD": "pw-cccccccccc",
            "DATABASE_URL": "postgres://app:hunter2hunter2@db:5432/x",
            "MY_SECRET_THING": "ddddddddddddd",
            "SHORT_TOKEN": "tiny",  # under MIN_SECRET_LEN
            "GIT_AUTHOR_NAME": "Jane Example",  # not a secret name
            "SSH_AUTH_SOCK": "/tmp/ssh-xyz/agent.123",
            "PATH": "/usr/bin:/bin",
        }
        r = _build(env)
        for value in (
            "tok-aaaaaaaaaaaa",
            "admin-bbbbbbbbbbb",
            "pw-cccccccccc",
            "postgres://app:hunter2hunter2@db:5432/x",
            "ddddddddddddd",
        ):
            assert r.redact_text(f"[{value}]") == "[[REDACTED:secret]]", value
        for value in ("tiny", "Jane Example", "/tmp/ssh-xyz/agent.123", "/usr/bin:/bin"):
            assert r.redact_text(value) == value

    def test_real_probes_never_raise_and_skip_loopback(self):
        r = sr.build_redactor({})
        assert r.redact_text("127.0.0.1 ::1 localhost") == "127.0.0.1 ::1 localhost"
        assert isinstance(sr._local_ips(), list)
        assert all(ip not in ("127.0.0.1", "::1") for ip in sr._local_ips())

    def test_disabled_means_no_redactor_and_no_mask(self, monkeypatch):
        monkeypatch.setenv("SYSINFO_REDACTION", "false")
        sr.reset_redactor_cache()
        assert sr.get_redactor() is None
        assert sr.child_env_mask() == {}

    def test_admin_console_override_wins_over_env(self):
        from src.runtime_config import runtime_config

        assert sr.get_redactor() is not None
        try:
            runtime_config.set("sysinfo_redaction_enabled", False)
            assert sr.get_redactor() is None and sr.child_env_mask() == {}
        finally:
            runtime_config.reset("sysinfo_redaction_enabled")
        assert sr.get_redactor() is not None

    def test_redactor_is_cached(self):
        assert sr.get_redactor() is sr.get_redactor()


class TestChildEnvMask:
    def test_defaults_blank_gateway_only_secrets(self):
        assert sr.child_env_mask() == {"ADMIN_API_KEY": "", "API_KEY": ""}

    def test_extra_names(self, monkeypatch):
        monkeypatch.setenv("SYSINFO_CHILD_ENV_MASK", "USAGE_LOG_DB_URL, API_KEY")
        assert sr.child_env_mask() == {
            "ADMIN_API_KEY": "",
            "API_KEY": "",
            "USAGE_LOG_DB_URL": "",
        }


# ---------------------------------------------------------------------------
# Nested values
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class _Block:
    text: str
    meta: Dict[str, Any]


@dataclasses.dataclass(frozen=True)
class _Frozen:
    text: str


class TestRedactValue:
    def test_nested_containers_are_rebuilt(self):
        r = _redactor()
        value = {"a": [HOST, (SECRET, 1)], "b": {"c": "10.0.0.1"}, "n": None, "i": 3}
        out = r.redact_value(value)
        assert out == {
            "a": ["[REDACTED:hostname]", ("[REDACTED:secret]", 1)],
            "b": {"c": "[REDACTED:ip]"},
            "n": None,
            "i": 3,
        }
        assert value["a"][0] == HOST  # input untouched

    def test_sdk_style_objects_are_updated_in_place(self):
        r = _redactor()
        block = _Block(text=f"on {HOST}", meta={"ip": "10.20.30.40"})
        frozen = _Frozen(text=SECRET)
        out = r.redact_value([block, frozen])
        assert out[0] is block and block.text == "on [REDACTED:hostname]"
        assert block.meta == {"ip": "[REDACTED:ip]"}
        assert frozen.text == "[REDACTED:secret]"

    def test_keys_are_not_rewritten_and_depth_is_bounded(self):
        r = _redactor()
        deep: Any = HOST
        for _ in range(200):
            deep = [deep]
        r.redact_value(deep)  # must not recurse without bound
        assert r.redact_value({HOST: 1}) == {HOST: 1}


# ---------------------------------------------------------------------------
# Streamed text: the carry invariant
# ---------------------------------------------------------------------------


def _chunked(text: str, rng: random.Random, max_size: int) -> List[str]:
    out, i = [], 0
    while i < len(text):
        n = rng.randint(1, max_size)
        out.append(text[i : i + n])
        i += n
    return out


class TestTextCarry:
    WORDS = [
        HOST,
        HOST.upper(),
        HOST + "2",
        "x" + HOST,
        "10.0.0.12",
        "10.0.0.1",
        "x10.0.0.1",
        "1.10.0.0.5",
        SECRET,
        "pass phrase with spaces",
        "fd12::1",
        "hello",
        "세계",
        "가" * 300,
        "漢字" * 200,
        "QUJDREVGR0g=" * 60,
        "\n",
        "  ",
        "\t",
    ]
    GLUE = ["", " ", "이", ".", "\n", ":", "/", "-"]

    # Each case streams a text of up to ~15k chars one to forty chars at a time;
    # raise REDACTION_FUZZ_CASES for a deeper local soak (CI keeps the default).
    @pytest.mark.parametrize("seed", range(3))
    def test_concatenated_output_equals_whole_text_redaction(self, seed):
        rng = random.Random(seed)
        r = _redactor()
        for _ in range(int(os.getenv("REDACTION_FUZZ_CASES", "80"))):
            text = "".join(
                rng.choice(self.WORDS) + rng.choice(self.GLUE)
                for _ in range(rng.randint(1, 25))
            )
            carry = _TextCarry(r)
            out = "".join(carry.feed(c) for c in _chunked(text, rng, rng.choice([1, 3, 9, 40])))
            out += carry.flush()
            assert out == r.redact_text(text), text[:300]

    def test_no_raw_value_is_ever_emitted_mid_stream(self):
        r = _redactor()
        text = f"connect to {HOST} then {SECRET} done"
        carry = _TextCarry(r)
        emitted = ""
        for ch in text:
            emitted += carry.feed(ch)
            assert HOST not in emitted and SECRET not in emitted
        assert emitted + carry.flush() == r.redact_text(text)

    def test_holdback_is_bounded(self):
        r = _redactor()
        carry = _TextCarry(r)
        for _ in range(5000):
            carry.feed("가")
        assert len(carry._buf) <= carry._max_run
        for _ in range(5000):
            carry.feed("word ")
        assert len(carry._buf) <= 64

    # --- #229 review: values longer than the forced-cut tail, and custom regexes

    LONG_SECRET = ("eyJhbGciOiJIUzI1NiJ9." + "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo" * 30)[:1000]

    def _stream(self, r, text, size):
        carry = _TextCarry(r)
        emitted = []
        for i in range(0, len(text), size):
            emitted.append(carry.feed(text[i : i + size]))
        emitted.append(carry.flush())
        return emitted

    @pytest.mark.parametrize("size", [1, 7, 64, 100])
    def test_whitespace_free_secret_longer_than_the_run_cut(self, size):
        """A 1000-char token (a JWT under *_TOKEN) streamed in <=100-char chunks:
        no raw prefix may leave before the whole literal is in the buffer."""
        r = Redactor([_Literal(self.LONG_SECRET, "secret", False)], [])
        assert len(self.LONG_SECRET) == 1000 and r.stream_keep >= 1000
        text = "token=" + self.LONG_SECRET + " end"
        emitted = self._stream(r, text, size)
        assert "".join(emitted) == "token=[REDACTED:secret] end"
        assert not any(self.LONG_SECRET[:32] in piece for piece in emitted)

    def test_long_secret_inside_a_long_cjk_run(self):
        r = Redactor([_Literal(self.LONG_SECRET, "secret", False)], [])
        text = "가" * 900 + self.LONG_SECRET + "나" * 900
        emitted = self._stream(r, text, 50)
        assert "".join(emitted) == r.redact_text(text)
        assert not any(self.LONG_SECRET[:32] in piece for piece in emitted)

    def test_literals_beyond_the_supported_ceiling_are_dropped_loudly(self, caplog):
        huge = "z" * (sr.MAX_LITERAL_LEN + 1)
        with caplog.at_level("WARNING"):
            r = Redactor([_Literal(huge, "secret", False)], [])
        assert not r.active
        assert "longer than" in caplog.text

    def test_custom_regex_spanning_whitespace_split_mid_match(self):
        r = Redactor([], [(r"secret\s+value", "custom")])
        assert r.unbounded_patterns
        carry = _TextCarry(r)
        assert carry.feed("secret ") == ""  # nothing released mid-block
        assert carry.feed("value") == ""
        assert carry.flush() == "[REDACTED:custom]"

    def test_custom_regex_holds_the_block_until_stop(self):
        r = _redactor(patterns=[(r"EMP-\d+", "custom")])
        out = TestStreamRedactor()._run(
            [_delta("id EMP-"), _delta("123456 ok"), _stop()]
        )
        assert out  # sanity: the helper ran
        s = StreamRedactor(r)
        first = s.process(_delta("id EMP-")) + s.process(_delta("123456 ok"))
        assert _streamed_text(first) == ""
        rest = s.process(_stop())
        assert _streamed_text(rest) == "id [REDACTED:custom] ok"

    # --- #229 review: a declared max_len must bound what the REGEX can match

    def _domain_redactor(self, monkeypatch):
        monkeypatch.setenv("SYSINFO_REDACT_DOMAINS", "corp.internal")
        return _build()

    @pytest.mark.parametrize(
        "prefix", ["", "가 ", "가" * 600], ids=["bare", "after-space", "in-cjk-run"]
    )
    def test_overlong_ipv6_like_run_streams_like_the_whole(self, prefix, monkeypatch):
        r = self._domain_redactor(monkeypatch)
        value = "fd00:" + "a:" * 1500 + "a"
        assert len(value) > sr._IPV6_MAX
        text = prefix + value + " 끝"
        emitted = self._stream(r, text, 20)
        assert "".join(emitted) == r.redact_text(text)

    @pytest.mark.parametrize(
        "prefix", ["", "가 ", "가" * 600], ids=["bare", "after-space", "in-cjk-run"]
    )
    def test_overlong_domain_run_streams_like_the_whole(self, prefix, monkeypatch):
        r = self._domain_redactor(monkeypatch)
        value = ".".join(["abcdefghij"] * 300) + ".corp.internal"
        assert len(value) > 253
        text = prefix + value + " 끝"
        emitted = self._stream(r, text, 20)
        assert "".join(emitted) == r.redact_text(text)
        # A run beyond the DNS limit is not a host name: never matched at all,
        # so there is no partial value to leak either way.
        assert "[REDACTED" not in r.redact_text(text)

    def test_real_names_and_addresses_at_their_limits_are_still_redacted(self, monkeypatch):
        r = self._domain_redactor(monkeypatch)
        longest_name = ".".join(["a" * 63] * 3) + "." + "b" * (253 - 3 * 64 - 14) + ".corp.internal"
        assert len(longest_name) == 253
        full_v6 = "fd12:3456:789a:bcde:f012:3456:789a:bcde"
        assert len(full_v6) == 39
        out = r.redact_text(f"{longest_name} {full_v6} FE80::1 db1.corp.internal.")
        assert out == "[REDACTED:domain] [REDACTED:ip] [REDACTED:ip] [REDACTED:domain]."

    @pytest.mark.parametrize(
        "name,alphabet,starts",
        [
            ("ipv4", "0123456789.", ["10.", "172.16.", "192.168.", "100.64.", "169.254."]),
            ("ipv6", "0123456789abcdefABCDEF:", ["fd00:", "fc12:", "fe80:", "FEBF:"]),
            ("domain", "abcdefghij0123456789.-", ["a.", "x-y."]),
        ],
    )
    def test_no_builtin_match_reaches_past_its_declared_bound(
        self, name, alphabet, starts, monkeypatch
    ):
        """Random runs over each pattern's own alphabet: every match (plus the
        one lookahead character) fits the max_len the carry relies on."""
        monkeypatch.setenv("SYSINFO_REDACT_DOMAINS", "corp.internal")
        regex, bound = {
            "ipv4": (sr._PRIVATE_IPV4, sr._IPV4_MAX),
            "ipv6": (sr._PRIVATE_IPV6, sr._IPV6_MAX),
            "domain": (None, sr._DOMAIN_MAX),
        }[name]
        if regex is None:
            regex = next(
                spec[0] for spec in sr._builtin_patterns() if spec[1] == "domain"
            )
        compiled = re.compile(regex)
        rng = random.Random(name)
        for _ in range(400):
            body = "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 600)))
            text = rng.choice(starts) + body + rng.choice(["", ".corp.internal", " "])
            for match in compiled.finditer(text):
                assert len(match.group(0)) + 1 <= bound, (name, len(match.group(0)))

    @pytest.mark.parametrize("seed", range(3))
    def test_fuzz_with_long_secret_and_custom_regex(self, seed):
        rng = random.Random(500 + seed)
        long_r = _redactor(literals=[_Literal(self.LONG_SECRET, "secret", False)])
        custom_r = _redactor(patterns=[(r"secret\s+value|EMP-\d{3,}", "custom")])
        words = self.WORDS + [self.LONG_SECRET, "secret value", "secret\n value", "EMP-1234"]
        for _ in range(int(os.getenv("REDACTION_FUZZ_CASES", "80")) // 2):
            text = "".join(
                rng.choice(words) + rng.choice(self.GLUE) for _ in range(rng.randint(1, 20))
            )
            for r in (long_r, custom_r):
                emitted = self._stream(r, text, rng.choice([1, 5, 33, 100]))
                assert "".join(emitted) == r.redact_text(text), text[:200]
                if r is long_r:
                    assert not any(self.LONG_SECRET[:32] in piece for piece in emitted)


# ---------------------------------------------------------------------------
# StreamRedactor over converted SDK chunks
# ---------------------------------------------------------------------------


def _delta(text: str, index: int = 0, parent=None, kind: str = "text_delta") -> Dict[str, Any]:
    field = "text" if kind == "text_delta" else "thinking"
    return {
        "type": "stream_event",
        "parent_tool_use_id": parent,
        "event": {"type": "content_block_delta", "index": index, "delta": {"type": kind, field: text}},
    }


def _stop(index: int = 0, parent=None) -> Dict[str, Any]:
    return {
        "type": "stream_event",
        "parent_tool_use_id": parent,
        "event": {"type": "content_block_stop", "index": index},
    }


def _streamed_text(chunks: List[Any], kind: str = "text_delta", index: int = 0, parent=None) -> str:
    field = "text" if kind == "text_delta" else "thinking"
    out = ""
    for c in chunks:
        ev = c.get("event") if isinstance(c, dict) else None
        if (
            ev
            and ev.get("type") == "content_block_delta"
            and ev.get("index") == index
            and c.get("parent_tool_use_id") == parent
            and ev["delta"].get("type") == kind
        ):
            out += ev["delta"][field]
    return out


class TestStreamRedactor:
    def _run(self, chunks):
        s = StreamRedactor(_redactor())
        out: List[Any] = []
        for c in chunks:
            out.extend(s.process(c))
        out.extend(s.flush_all())
        return out

    @pytest.mark.parametrize("kind", ["text_delta", "thinking_delta"])
    def test_value_split_across_deltas_is_redacted(self, kind):
        text = f"the host is {HOST} and key {SECRET}."
        pieces = [text[i : i + 2] for i in range(0, len(text), 2)]
        out = self._run([_delta(p, kind=kind) for p in pieces] + [_stop()])
        streamed = _streamed_text(out, kind)
        assert streamed == _redactor().redact_text(text)
        assert HOST not in str(out) and SECRET not in str(out)

    def test_flush_is_emitted_before_the_block_stop(self):
        out = self._run([_delta(f"bye {HOST}"), _stop()])
        kinds = [c["event"]["type"] for c in out]
        assert kinds[-1] == "content_block_stop"
        assert _streamed_text(out) == "bye [REDACTED:hostname]"

    def test_parallel_blocks_and_subagents_do_not_mix(self):
        chunks = [
            _delta("gw-pr", index=0),
            _delta("gw-", index=1),
            _delta("gw-pr", index=0, parent="toolu_sub"),
            _delta("od-01 a", index=0),
            _delta("prod-01 b", index=1),
            _delta("od-01 c", index=0, parent="toolu_sub"),
            _stop(0),
            _stop(1),
            _stop(0, parent="toolu_sub"),
        ]
        out = self._run(chunks)
        assert _streamed_text(out, index=0) == "[REDACTED:hostname] a"
        assert _streamed_text(out, index=1) == "[REDACTED:hostname] b"
        assert _streamed_text(out, index=0, parent="toolu_sub") == "[REDACTED:hostname] c"

    def test_message_stop_and_end_of_stream_flush_leftovers(self):
        out = self._run([_delta(f"a {HOST}"), {"type": "stream_event", "event": {"type": "message_stop"}}])
        assert _streamed_text(out) == "a [REDACTED:hostname]"
        out = self._run([_delta(f"b {HOST}")])  # stream ended without a stop
        assert _streamed_text(out) == "b [REDACTED:hostname]"

    def test_whole_messages_are_deep_redacted(self):
        block = SimpleNamespace(text=f"done on {HOST}")
        out = self._run(
            [
                {"type": "assistant", "content": [block]},
                {"type": "user", "content": [{"type": "tool_result", "content": f"{SECRET}"}]},
                {"type": "result", "result": "10.20.30.40", "is_error": False},
                {"type": "tool_progress", "data": {"message": f"ping {HOST}"}},
            ]
        )
        assert block.text == "done on [REDACTED:hostname]"
        assert out[1]["content"][0]["content"] == "[REDACTED:secret]"
        assert out[2]["result"] == "[REDACTED:ip]"
        assert out[3]["data"]["message"] == "ping [REDACTED:hostname]"

    def test_tool_input_json_deltas_are_redacted_too(self):
        chunk = {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "input_json_delta", "partial_json": f'{{"cmd": "ssh {HOST}"}}'},
            },
        }
        out = self._run([chunk])
        assert HOST not in str(out)

    @pytest.mark.parametrize("seed", range(4))
    def test_random_chunking_matches_full_redaction(self, seed):
        rng = random.Random(100 + seed)
        r = _redactor()
        for _ in range(150):
            text = " ".join(rng.choice(TestTextCarry.WORDS[:12]) for _ in range(rng.randint(1, 15)))
            chunks = [_delta(p) for p in _chunked(text, rng, 6)] + [_stop()]
            assert _streamed_text(self._run(chunks)) == r.redact_text(text)


# ---------------------------------------------------------------------------
# PostToolUse hook
# ---------------------------------------------------------------------------


class TestPostToolUseHook:
    async def test_rewrites_only_when_something_matched(self):
        hook = sr.make_redaction_post_tool_use_hook(_redactor())
        clean = {"stdout": "ok", "stderr": "", "interrupted": False}
        assert await hook({"tool_response": clean}, "t", None) == {}
        assert await hook({"tool_response": None}, "t", None) == {}
        dirty = {"stdout": f"{HOST}\n", "stderr": "", "interrupted": False, "isImage": False}
        out = await hook({"tool_response": dirty}, "t", None)
        assert out == {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "updatedToolOutput": {
                    "stdout": "[REDACTED:hostname]\n",
                    "stderr": "",
                    "interrupted": False,
                    "isImage": False,
                },
            }
        }
        assert dirty["stdout"] == f"{HOST}\n"

    async def test_string_and_list_results_keep_their_shape(self):
        hook = sr.make_redaction_post_tool_use_hook(_redactor())
        out = await hook({"tool_response": f"{SECRET}"}, "t", None)
        assert out["hookSpecificOutput"]["updatedToolOutput"] == "[REDACTED:secret]"
        out = await hook({"tool_response": [{"type": "text", "text": HOST}]}, "t", None)
        assert out["hookSpecificOutput"]["updatedToolOutput"] == [
            {"type": "text", "text": "[REDACTED:hostname]"}
        ]


# ---------------------------------------------------------------------------
# Gateway wiring
# ---------------------------------------------------------------------------


@pytest.fixture
def cli():
    with patch("src.auth.validate_claude_code_auth") as mock_validate:
        with patch("src.auth.auth_manager") as mock_auth:
            mock_validate.return_value = (True, {"method": "anthropic"})
            mock_auth.get_claude_code_env_vars.return_value = {"ANTHROPIC_AUTH_TOKEN": "t"}
            from src.backends.claude.client import ClaudeCodeCLI

            yield ClaudeCodeCLI(cwd="/tmp")


def _use(redactor):
    return patch("src.backends.claude.client.get_redactor", return_value=redactor)


class TestGatewayWiring:
    def test_child_env_masks_gateway_secrets(self, cli, monkeypatch):
        monkeypatch.setenv("ADMIN_API_KEY", "should-not-reach-child")
        options = cli._build_sdk_options()
        assert options.env["ADMIN_API_KEY"] == ""
        assert options.env["API_KEY"] == ""

    def test_child_env_untouched_when_disabled(self, cli, monkeypatch):
        monkeypatch.setenv("SYSINFO_REDACTION", "false")
        options = cli._build_sdk_options()
        assert "ADMIN_API_KEY" not in options.env

    def test_post_tool_use_hook_matches_every_tool(self, cli):
        with _use(_redactor()):
            matchers = cli._post_tool_use_hooks()
        assert len(matchers) == 1 and matchers[0].matcher == ""

    def test_no_hook_when_disabled_or_empty(self, cli):
        with _use(None):
            assert cli._post_tool_use_hooks() == []
        with _use(Redactor([], [])):
            assert cli._post_tool_use_hooks() == []

    async def test_create_client_installs_the_hook(self, cli, monkeypatch, tmp_path):
        from src import session_manager
        from src.backends.claude import client as mod
        from src.session_manager import Session

        monkeypatch.setattr(session_manager, "_PROJECTS_ROOT", tmp_path)
        captured = {}

        class FakeSDKClient:
            def __init__(self, *, options):
                captured["options"] = options

            async def connect(self, prompt=None):
                return None

        monkeypatch.setattr(mod, "ClaudeSDKClient", FakeSDKClient)
        with _use(_redactor()):
            await cli.create_client(session=Session(session_id="s-1", workspace=str(tmp_path)), cwd=str(tmp_path))
        hooks = captured["options"].hooks
        assert "PreToolUse" in hooks and len(hooks["PostToolUse"]) == 1
        assert captured["options"].env["ADMIN_API_KEY"] == ""

    async def test_turn_output_is_redacted_and_split_values_never_stream(self, cli):
        async def raw(client, prompt, session):
            for piece in ("ssh gw-", "prod-", "01 now"):
                yield _delta(piece)
            yield _stop()
            yield {"type": "assistant", "content": [SimpleNamespace(text=f"ssh {HOST} now")]}

        cli._run_completion_with_client_raw = raw
        with _use(_redactor()):
            out = [c async for c in cli.run_completion_with_client(None, "hi", None)]
        assert _streamed_text(out) == "ssh [REDACTED:hostname] now"
        assert out[-1]["content"][0].text == "ssh [REDACTED:hostname] now"

    async def test_receive_response_path_is_redacted(self, cli):
        async def raw(client, session):
            yield {"type": "result", "result": f"key {SECRET}"}

        cli._receive_response_from_client_raw = raw
        with _use(_redactor()):
            out = [c async for c in cli.receive_response_from_client(None, None)]
        assert out == [{"type": "result", "result": "key [REDACTED:secret]"}]

    async def test_disabled_passes_chunks_through_untouched(self, cli):
        chunk = {"type": "result", "result": HOST}

        async def raw(client, prompt, session):
            yield chunk

        cli._run_completion_with_client_raw = raw
        with _use(None):
            out = [c async for c in cli.run_completion_with_client(None, "hi", None)]
        assert out == [chunk] and out[0] is chunk

    async def test_consumer_break_closes_the_inner_generator_immediately(self, cli):
        closed = []

        async def raw(client, prompt, session):
            try:
                yield {"type": "result", "result": "a"}
                yield {"type": "result", "result": "b"}
            finally:
                closed.append(True)

        cli._run_completion_with_client_raw = raw
        for redactor in (_redactor(), None):
            closed.clear()
            with _use(redactor):
                gen = cli.run_completion_with_client(None, "hi", None)
                async for _ in gen:
                    break
                await gen.aclose()
            assert closed == [True]


class TestIdleOutbox:
    def test_between_turn_events_are_redacted(self, monkeypatch):
        from src import session_outbox

        monkeypatch.setattr(
            session_outbox,
            "_message_to_event",
            lambda message: {"type": "assistant_text", "text": f"bg done on {HOST}"},
        )
        session = SimpleNamespace(session_id="s", touch=lambda: None)
        with patch.object(sr, "get_redactor", return_value=_redactor()):
            session_outbox._handle_idle_message(session, {"type": "x"})
        events = session_outbox.get_outbox(session).events_after(0)
        assert events[-1]["text"] == "bg done on [REDACTED:hostname]"
