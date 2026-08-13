"""컨텍스트 게이지용 usage 확장 — `context_tokens`.

`extract_sdk_usage`의 prompt 합계는 ResultMessage의 **턴 누계**(청구용)라,
에이전틱 턴(매 API 호출이 컨텍스트 전체를 캐시로 재독)에서는 컨텍스트 윈도를
몇 배씩 넘는다 — UI가 그걸 게이지에 그리면 263k/250k 같은 표시가 된다(실측).
Claude Code 상태 줄과 `/context`는 **마지막 메인 루프 호출의
input+cache_creation+cache_read 스냅숏**을 쓴다(CLI 바이너리 검증). 같은 값을
확장 필드로 싣는다.
"""

from src.response_models import ResponseUsage
from src.streaming_utils import extract_sdk_context_tokens


def _assistant(usage, parent=None):
    return {"type": "assistant", "usage": usage, "parent_tool_use_id": parent}


def test_last_main_loop_call_snapshot_not_cumulative():
    chunks = [
        _assistant({"input_tokens": 10, "cache_creation_input_tokens": 50_000,
                    "cache_read_input_tokens": 0, "output_tokens": 300}),
        _assistant({"input_tokens": 900, "cache_creation_input_tokens": 2_000,
                    "cache_read_input_tokens": 50_000, "output_tokens": 500}),
        _assistant({"input_tokens": 1_200, "cache_creation_input_tokens": 1_000,
                    "cache_read_input_tokens": 85_800, "output_tokens": 400}),
        # ResultMessage 누계 — 게이지가 이걸 쓰면 안 된다
        {"type": "result", "usage": {"input_tokens": 2_110,
                                     "cache_creation_input_tokens": 53_000,
                                     "cache_read_input_tokens": 135_800,
                                     "output_tokens": 1_200}},
    ]
    # 마지막 호출: 1200 + 1000 + 85800 = 88000 (output은 안 더한다 — CLI 게이지 동일)
    assert extract_sdk_context_tokens(chunks) == 88_000


def test_subagent_calls_are_not_the_main_context():
    chunks = [
        _assistant({"input_tokens": 100, "cache_read_input_tokens": 60_000,
                    "cache_creation_input_tokens": 0, "output_tokens": 10}),
        # Task 서브에이전트의 마지막 호출 — 저건 서브에이전트의 컨텍스트다
        _assistant({"input_tokens": 5, "cache_read_input_tokens": 9_000,
                    "cache_creation_input_tokens": 0, "output_tokens": 3},
                   parent="toolu_task1"),
    ]
    assert extract_sdk_context_tokens(chunks) == 60_100


def test_no_per_call_usage_means_none():
    assert extract_sdk_context_tokens([
        {"type": "result", "usage": {"input_tokens": 10, "output_tokens": 5}},
    ]) is None
    assert extract_sdk_context_tokens([]) is None


def test_usage_model_carries_the_extension_without_touching_total():
    u = ResponseUsage(input_tokens=200_000, output_tokens=1_000, context_tokens=88_000)
    assert u.total_tokens == 201_000  # context_tokens는 합계에 안 들어간다
    assert u.model_dump()["context_tokens"] == 88_000
    # 없으면 None — 추정값을 지어내지 않는다
    assert ResponseUsage(input_tokens=1, output_tokens=1).context_tokens is None
