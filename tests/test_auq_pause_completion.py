"""파킹된 AskUserQuestion 턴은 `completed`를 쏘지 않는다 (issue #13).

증상은 UI 쪽에서 났다: 질문 카드가 뜨지 않고 "결과 (중단됨 — 결과 미수신)"만
남았다가 300초 뒤 게이트웨이가 `AskUserQuestion timed out`으로 끝났다.

원인은 여기다. `can_use_tool`이 세션을 세워 두고 답을 기다리는 동안 SDK 메시지
반복자가 먼저 끝날 수 있고(모델이 thinking만 내고 질문한 턴), 그러면
`stream_break_event` 레이스에서 `get_next`가 이겨 스트림 루프가 "정상 종료"로
빠져나온다. 그 결과 종료 시퀀스가 `status="completed"`를 먼저 쏘고, 라우트가
뒤이어 `requires_action`을 **한 번 더** 쏜다. 클라이언트는 첫 완료를 보고 턴을
닫으므로 두 번째는 도착해도 소용이 없다.

기존 가드(`not content_sent and not thinking_seen`)는 **내용의 유무**를 물었다.
물어야 할 것은 파킹 여부다 — thinking이 하나라도 있으면 그 가드는 통과한다.
"""

import json

import pytest

from src.streaming_utils import stream_response_chunks


class _Session:
    """세션 대역 — 여기서 중요한 건 `pending_tool_call` 하나뿐이다."""

    def __init__(self, pending=None):
        self.pending_tool_call = pending


def _thinking_then_end():
    """모델이 thinking만 내고 질문한 턴 — 실측된 그 모양."""

    async def gen():
        yield {
            "type": "assistant",
            "content": [{"type": "thinking", "thinking": "어느 환경인지 물어봐야겠다"}],
        }

    return gen()


async def _collect(source, session):
    result: dict = {}
    lines: list[str] = []
    async for line in stream_response_chunks(
        source,
        model="test-model",
        response_id="resp_1",
        output_item_id="msg_1",
        chunks_buffer=[],
        logger=__import__("logging").getLogger(__name__),
        stream_result=result,
        request_context={"session": session, "use_sdk_client": True},
    ):
        lines.append(line)
    return lines, result


def _completions(lines: list[str]) -> list[dict]:
    out = []
    for line in lines:
        for part in line.split("\n"):
            if not part.startswith("data:"):
                continue
            try:
                ev = json.loads(part[5:].strip())
            except ValueError:
                continue
            if ev.get("type") == "response.completed":
                out.append(ev)
    return out


@pytest.mark.asyncio
async def test_parked_turn_emits_no_completed_event():
    """파킹 중이면 완료 이벤트는 라우트(requires_action)에게 맡긴다."""
    session = _Session(pending={"call_id": "toolu_1", "name": "AskUserQuestion", "arguments": {}})
    lines, result = await _collect(_thinking_then_end(), session)

    assert _completions(lines) == [], "파킹된 턴이 completed를 쐈다 — 카드가 안 뜬다"
    assert result.get("paused") is True
    assert result.get("success") is False


@pytest.mark.asyncio
async def test_thinking_only_turn_completes_normally_when_not_parked():
    """파킹이 아니면 예전 그대로 — thinking만 있는 턴도 정상 종료한다."""
    session = _Session(pending=None)
    lines, result = await _collect(_thinking_then_end(), session)

    done = _completions(lines)
    assert len(done) == 1
    assert done[0]["response"]["status"] == "completed"
    assert result.get("success") is True
    assert not result.get("paused")


@pytest.mark.asyncio
async def test_parked_turn_still_closes_open_items():
    """완료를 미룬다고 열어 둔 reasoning 항목까지 흘리지는 않는다."""
    session = _Session(pending={"call_id": "toolu_1", "name": "AskUserQuestion", "arguments": {}})
    lines, _ = await _collect(_thinking_then_end(), session)

    kinds = []
    for line in lines:
        for part in line.split("\n"):
            if part.startswith("data:"):
                try:
                    kinds.append(json.loads(part[5:].strip()).get("type"))
                except ValueError:
                    pass
    opened = kinds.count("response.output_item.added")
    closed = kinds.count("response.output_item.done")
    assert opened == closed, f"열린 항목 {opened}개, 닫힌 항목 {closed}개"


@pytest.mark.asyncio
async def test_no_session_in_context_behaves_as_before():
    """세션을 안 넘기는 경로(다른 백엔드 등)는 영향을 받지 않는다."""
    result: dict = {}
    lines: list[str] = []
    async for line in stream_response_chunks(
        _thinking_then_end(),
        model="test-model",
        response_id="resp_1",
        output_item_id="msg_1",
        chunks_buffer=[],
        logger=__import__("logging").getLogger(__name__),
        stream_result=result,
        request_context={"use_sdk_client": True},
    ):
        lines.append(line)
    assert len(_completions(lines)) == 1
    assert result.get("success") is True
