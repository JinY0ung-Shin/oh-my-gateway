# ClaudeSDKClient 전환 및 AskUserQuestion 지원 설계

## Summary

세션 기반 요청을 `query()`에서 `ClaudeSDKClient`로 전환하고, `PreToolUse` 훅을 활용하여
AskUserQuestion을 OpenAI 표준 `function_call` / `function_call_output` 패턴으로 처리한다.
동시에 `/v1/chat/completions`, `/v1/messages` 엔드포인트를 제거하고 `/v1/responses`로 단일화한다.

> **NOTE**: 초기 설계에서는 `can_use_tool` 콜백을 사용했으나, CLI가 `control_request`
> 메시지를 전송하지 않아 콜백이 호출되지 않는 것으로 확인됨. `PreToolUse` 훅이
> 올바르게 동작하므로 이를 대체 메커니즘으로 사용한다.

## Motivation

- Claude Agent SDK의 `query()`는 단방향 async generator로, 실행 중 사용자 입력 주입이 불가능
- AskUserQuestion tool_use가 발생하면 클라이언트에 질문을 전달하고 응답을 SDK에 돌려줘야 하는데,
  현재 아키텍처에서는 경로가 없음 (GitHub Issue #76)
- `ClaudeSDKClient`는 양방향 통신과 `PreToolUse` 훅을 지원하여 이 문제를 해결 가능
  (`can_use_tool` 콜백은 CLI가 `control_request`를 전송하지 않아 동작하지 않음)
- `/v1/chat/completions`과 `/v1/messages`는 더 이상 사용되지 않으므로 제거하여 코드 단순화

## Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| SDK client | `ClaudeSDKClient` (세션 요청) / `query()` (비세션) | 세션 요청에만 subprocess 유지, 비세션은 기존 동작 보존 |
| AskUserQuestion 패턴 | OpenAI 표준 `function_call` → `function_call_output` | 클라이언트 호환성, 비표준 사이드 채널 불필요 |
| API surface | `/v1/responses` 단일화 | chat/messages는 미사용, 코드 단순화 |
| 기존 설계 문서 | 제거 | `docs/plans/2026-03-05-*` 문서는 outdated |

## Architecture

### Before
```
요청 → /v1/chat/completions OR /v1/messages OR /v1/responses
         ↓
       run_completion() → query() → subprocess 생성 → 응답 → subprocess 종료
       (매 요청마다 새 subprocess, resume=session_id로 연속성)
```

### After
```
요청 → /v1/responses (유일한 API surface)
         ├─ 비세션 (previous_response_id 없음)
         │    → run_completion() → query() (기존 그대로)
         │
         └─ 세션 (previous_response_id 있음)
              → run_completion_with_client()
              → Session.client (ClaudeSDKClient, subprocess 유지)
              │
              ├─ 첫 턴: ClaudeSDKClient 생성 + connect()
              ├─ 후속 턴: client.query(prompt) 재사용
              │
              └─ AskUserQuestion 발생 시:
                   1. PreToolUse 훅 호출 → asyncio.Event.wait()
                   2. 스트림 종료, function_call 포함하여 응답 반환
                   3. 클라이언트가 function_call_output으로 다음 요청
                   4. Event.set() → 훅 해제 → SDK 진행 → 새 스트림
```

## Component Design

### 1. Session 변경 (`src/session_manager.py`)

Session 데이터클래스에 ClaudeSDKClient 관련 필드 추가:

```python
@dataclass
class Session:
    # 기존 필드 유지
    session_id: str
    backend: str = "claude"
    provider_session_id: Optional[str] = None
    messages: List[Message] = field(default_factory=list)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # ...

    # 신규 필드
    client: Optional[Any] = None                # ClaudeSDKClient 인스턴스
    input_event: Optional[asyncio.Event] = None # AskUserQuestion 대기용
    input_response: Optional[str] = None        # 클라이언트 응답 저장
    pending_tool_call: Optional[Dict] = None    # 대기 중인 function_call 정보
```

세션 정리 시 `client.disconnect()` 호출:
- `_cleanup_expired_sessions()`: 만료 세션의 client disconnect
- `async_shutdown()`: 전체 client disconnect

### 2. ClaudeCodeCLI 확장 (`src/backends/claude/client.py`)

**`create_client()`**: ClaudeSDKClient를 생성하고 connect하는 팩토리 메서드.
- `PreToolUse` 훅을 `HookMatcher(matcher="AskUserQuestion")`로 등록
- `connect(prompt=None)` 호출 (context manager 아닌 수동 관리)

> **NOTE**: 초기에는 `can_use_tool` 콜백을 사용했으나, CLI가 `control_request`를
> 전송하지 않아 콜백이 호출되지 않음. `PreToolUse` 훅으로 대체.

**`run_completion_with_client()`**: 기존 client로 턴을 실행하는 메서드.
- `client.query(prompt)` → `client.receive_response()` 순회
- 메시지를 `_convert_message()`로 변환하여 yield
- 에러 시 client를 None으로 초기화 (다음 요청 시 재생성)

**PreToolUse 훅 구현**:
```python
def _make_ask_user_hook(self, session):
    async def hook(input_data, tool_use_id, context):
        tool_name = getattr(input_data, "tool_name", "")
        if tool_name != "AskUserQuestion":
            return {}  # Allow other tools
        tool_input = getattr(input_data, "tool_input", {})
        actual_tool_use_id = getattr(input_data, "tool_use_id", tool_use_id)
        session.pending_tool_call = {
            "call_id": actual_tool_use_id,
            "name": "AskUserQuestion",
            "arguments": tool_input,
        }
        session.input_event = asyncio.Event()
        await session.input_event.wait()
        session.input_response = None
        session.input_event = None
        return {"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
        }}
    return hook
```

### 3. Responses API 라우트 변경 (`src/routes/responses.py`)

**AskUserQuestion function_call 응답 처리:**

스트리밍 중 `PreToolUse` 훅이 호출되면 (= `session.pending_tool_call`이 세팅되면),
스트리밍 함수는 진행 중인 텍스트 출력을 마무리하고 `function_call` output item을 포함하여
`response.completed` 이벤트를 전송한다. 이때 응답 상태는 `"requires_action"`으로 설정한다.

**function_call_output 입력 처리:**

다음 요청의 `input`에 `function_call_output` 항목이 포함되어 있으면:
1. `session.input_response`에 output 값 저장
2. `session.input_event.set()` → 대기 중인 훅 해제
3. SDK 처리가 재개되면 `client.receive_response()`로 새 스트림 시작

### 4. Response Models 확장 (`src/response_models.py`)

기존 output_text만 지원하던 모델에 function_call 관련 타입 추가:

```python
class FunctionCallOutputItem(BaseModel):
    type: Literal["function_call"] = "function_call"
    call_id: str
    name: str
    arguments: str  # JSON string
    status: str = "completed"

class FunctionCallOutputInput(BaseModel):
    type: Literal["function_call_output"] = "function_call_output"
    call_id: str
    output: str
```

`ResponseObject.output`을 `List[Union[OutputItem, FunctionCallOutputItem]]`으로 확장.
`ResponseCreateRequest.input`이 `function_call_output` 항목을 포함할 수 있도록 확장.

### 5. Streaming 파이프라인 변경 (`src/streaming_utils.py`)

**제거:**
- `stream_chunks()` — chat completions SSE 함수
- `make_sse()` — chat completions SSE 포맷터
- chat completions 전용 헬퍼 함수들

**변경:**
- `stream_response_chunks()`에 AskUserQuestion 감지 로직 추가:
  - `session.pending_tool_call`이 세팅되면 텍스트 스트림을 마무리
  - `function_call` output item을 포함한 완료 이벤트 전송
  - 응답 상태를 `"requires_action"`으로 설정

**유지:**
- `stream_response_chunks()` — responses SSE 함수
- `make_response_sse()` — responses SSE 포맷터
- `ToolUseAccumulator`, `CollabJsonStreamFilter` 등 공통 유틸리티

### 6. 제거 대상

| 파일/코드 | 이유 |
|-----------|------|
| `src/routes/chat.py` | `/v1/chat/completions` 엔드포인트 전체 |
| `src/routes/messages.py` | `/v1/messages` 엔드포인트 전체 |
| `src/main.py` chat/messages 라우터 등록 | 라우터 제거에 따라 |
| `src/main.py` backward-compat re-exports | chat.py 제거에 따라 |
| `src/models.py` chat 전용 스키마 | ChatCompletionRequest/Response 등 |
| `src/streaming_utils.py` chat 전용 함수 | stream_chunks(), make_sse() 등 |
| `docs/plans/2026-03-05-*` | outdated 설계 문서 2개 |

### 7. 라우트 등록 (`src/routes/__init__.py`)

chat_router, messages_router export 제거. responses_router, sessions_router,
general_router, admin_router만 유지.

## AskUserQuestion 클라이언트 흐름 (상세)

### 정상 흐름 (AskUserQuestion 없음)
```
Client                          Gateway                         SDK
  │                                │                              │
  ├─ POST /v1/responses ──────────→│                              │
  │  {input: "코드 리팩토링 해줘"}  │── client.query(prompt) ────→│
  │                                │                              │
  │←── SSE: response.created ──────│                              │
  │←── SSE: response.in_progress ──│                              │
  │←── SSE: response.output_text   │←── AssistantMessage ─────────│
  │        .delta (반복) ──────────│                              │
  │←── SSE: response.completed ────│←── ResultMessage ────────────│
  │                                │                              │
```

### AskUserQuestion 흐름
```
Client                          Gateway                         SDK
  │                                │                              │
  ├─ POST /v1/responses ──────────→│                              │
  │  {input: "코드 리팩토링 해줘"}  │── client.query(prompt) ────→│
  │                                │                              │
  │←── SSE: response.output_text   │←── text deltas ──────────────│
  │        .delta (일부 텍스트)     │                              │
  │                                │←── PreToolUse hook ──────────│
  │                                │    (AskUserQuestion)         │
  │                                │    → session.pending_tool_call 설정
  │                                │    → session.input_event.wait()
  │                                │                              │
  │←── SSE: response.output_item   │  (스트림이 function_call을    │
  │        .added (function_call)  │   포함하여 종료)              │
  │←── SSE: response.completed     │                              │
  │    status: "requires_action"   │                              │
  │                                │    [훅은 여전히 대기 중]       │
  │                                │    [subprocess 살아있음]       │
  │  (사용자가 UI에서 답변 입력)     │                              │
  │                                │                              │
  ├─ POST /v1/responses ──────────→│                              │
  │  {previous_response_id: "...", │                              │
  │   input: [{                    │                              │
  │     type: "function_call_output",                             │
  │     call_id: "toolu_xxx",      │                              │
  │     output: "응 괜찮아"        │                              │
  │   }]}                          │                              │
  │                                │── session.input_response 설정 │
  │                                │── session.input_event.set() ──│
  │                                │                              │── 훅 반환
  │                                │                              │── SDK 진행
  │                                │                              │
  │←── SSE: response.created ──────│                              │
  │←── SSE: response.output_text   │←── text deltas ──────────────│
  │        .delta (이어서 진행)     │                              │
  │←── SSE: response.completed ────│←── ResultMessage ────────────│
  │                                │                              │
```

## 에러 처리

### subprocess 비정상 종료
- `client.receive_response()` 중 에러 감지 → `session.client = None` 초기화
- 대기 중인 `input_event`가 있으면 set하여 훅 해제
- 다음 요청 시 새 ClaudeSDKClient 자동 생성

### 세션 만료 (TTL)
- `_cleanup_expired_sessions()`에서 `client.disconnect()` 호출
- 대기 중인 `input_event`가 있으면 set → 훅이 해제되고 에러 전파
- subprocess graceful shutdown (5초 대기 → SIGTERM → 5초 → SIGKILL)

### PreToolUse 훅과 스트리밍 동기화
- `PreToolUse` 훅과 `receive_response()` 순회는 동일 이벤트 루프의 다른 코루틴
- SDK 내부에서 훅이 호출되면 `receive_response()`는 더 이상 메시지를 yield하지 않음
  (CLI가 tool 실행을 멈추고 훅 응답을 기다리므로)
- 따라서 스트리밍 루프가 자연스럽게 멈추는 시점에 `session.pending_tool_call`을 확인
- 이 확인은 `receive_response()`의 타임아웃 또는 idle 감지로 트리거

### 클라이언트 응답 없음
- 세션 TTL(기본 60분)에 의존하여 자동 정리
- 별도 AskUserQuestion 타임아웃은 두지 않음 (세션 TTL이 상한)

### 동시 요청
- 기존 `session.lock` (asyncio.Lock)으로 보호
- AskUserQuestion 대기 중에는 lock이 해제된 상태
  (스트리밍 응답이 끝나면서 lock 해제, 다음 요청에서 lock 재획득)

### ClaudeSDKClient 생성 실패
- 에러를 HTTP 500으로 전파, session.client는 None 유지
- 다음 요청 시 재시도

## Testing Strategy

| Test | What |
|------|------|
| `create_client()` unit | Mock ClaudeSDKClient, PreToolUse 훅 등록 확인 |
| `run_completion_with_client()` unit | Mock client.query + receive_response, 메시지 변환 확인 |
| `PreToolUse` 훅 unit | AskUserQuestion 감지, Event 대기/해제, 다른 도구 허용 확인 |
| function_call SSE 출력 | AskUserQuestion 시 function_call item 포함, status=requires_action |
| function_call_output 입력 | input에 function_call_output → Event.set() → 스트림 재개 |
| 세션 라이프사이클 | client 생성/재사용/disconnect, 만료 정리 |
| subprocess 비정상 종료 | client 재생성, 에러 전파 확인 |
| 엔드포인트 제거 | /v1/chat/completions, /v1/messages → 404 |
| 기존 responses 흐름 | 비세션/세션 정상 동작 유지 (regression) |

## Scope

### In Scope
- `ClaudeSDKClient`로 세션 요청 전환
- `PreToolUse` 훅으로 AskUserQuestion 처리
- OpenAI 표준 `function_call` / `function_call_output` 패턴
- `/v1/chat/completions`, `/v1/messages` 엔드포인트 제거
- outdated 설계 문서 제거
- chat 전용 스키마/SSE 함수 정리

### Out of Scope
- 프로그래밍 방식 자동 응답 (시나리오 B)
- WebSocket 기반 양방향 통신
- AskUserQuestion 외 다른 interactive tool 지원
- Open WebUI pipe 업데이트 (이미 제거됨)
