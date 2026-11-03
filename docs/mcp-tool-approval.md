# MCP 도구 실행 전 사용자 승인 (ChatDRAGON #798)

되돌릴 수 없는 MCP 쓰기 도구(메일 발송·일정/할일 생성·수정·삭제 등)를 **사람이 승인한 뒤에만** 실행한다.
게이트는 게이트웨이(이 저장소)에 있고, 카드는 클라이언트(ChatDRAGON UI/BFF)가 그린다. 둘 다 있어야 동작한다.

## 1. 운영자 설정 — MCP 서버 항목의 `approvalTools`

`readOnlyTools`처럼 게이트웨이 전용 키다. CLI에는 전달되지 않는다(`_configure_mcp_servers`가 모든 옵션 빌드에서 뗀다).

```json
"knob": {
  "type": "http",
  "url": "https://knob-mcp.internal/mcp",
  "readOnlyTools": ["list_mail", "get_schedule", "..."],
  "approvalTools": "*"
}
```

| 값 | 의미 |
|---|---|
| `"*"` | 이 서버의 **모든 도구**가 승인 대상. 단 같은 항목의 `readOnlyTools`에 이름이 있는 도구는 제외. 서버가 도구 이름을 바꾸거나 새 쓰기 도구를 추가해도 자동 실행되지 않는다(이름 바뀐 읽기 도구도 승인 대상이 된다 — 안전한 쪽). |
| `["send_mail", ...]` | 그 이름의 도구만 승인 대상. |
| 그 밖의 값(`true`, 빈 배열, 숫자…) | 경고를 남기고 `"*"`로 취급한다. 오타로 승인이 빠지지 않게. |

판정 근거는 **운영자가 쓴 이 설정뿐**이다. MCP 서버가 스스로 내는 `readOnlyHint` 같은 annotation은 보지 않는다
(서버가 자기 쓰기 도구를 읽기라고 선언해 승인을 피할 수 없게). `readOnlyTools`와 `approvalTools`를 둘 다 `"*"`로 두는
모순된 설정은 전부 승인 대상이다.

## 2. 왜 `can_use_tool`이 아니라 PreToolUse 훅인가 (실측)

`tests/test_cli_tool_approval.py`가 번들 CLI 2.1.283으로 고정한다:

- `permission_mode=bypassPermissions`(운영 기본)이거나 요청의 `allowed_tools`가 그 도구를 포함하면, CLI는 MCP 호출에
  대해 **`can_use_tool`을 부르지 않고 바로 실행한다.** SDK도 `CanUseToolShadowedWarning`으로 "PreToolUse 훅을 쓰라"고 한다.
  그래서 `HUMAN_DECISION_TOOLS`에 도구를 넣는 방식은 운영에서 **조용히 무효**다.
- PreToolUse 훅은 모든 모드에서 호출되고, 훅이 기다리는 동안 도구는 시작하지 않으며, 훅이 받은 인자가 그대로 실행된다.

## 3. 클라이언트 계약

### 3.1 지원 선언 (capability)

카드를 그릴 수 있는 클라이언트는 **매 요청마다** 헤더를 보낸다:

```
X-Gateway-Capabilities: tool-approval
```

헤더가 없는 요청(옛 클라이언트, 다른 통합)에서 승인 대상 도구를 부르면 게이트웨이는 **멈추지 않고 거부**한다
(도구 미실행, 모델에게 "승인이 필요한데 이 클라이언트는 물을 수 없다"는 결과). 게이트웨이를 먼저 배포해도 몰래 실행되거나
카드 없이 턴이 멈춘 것처럼 보이지 않는다. 값은 요청마다 다시 읽으므로, 같은 세션의 이어지는 요청이 헤더를 빼면 그 요청부터 거부된다.

### 3.2 멈춤 — `requires_action`

AskUserQuestion·ExitPlanMode와 같은 경로다. 스트림은 `response.output_item.added`(`function_call`)와
`response.completed`(`status: "requires_action"`)로 끝나고, 그 `function_call` 항목에 **`approval`** 이 붙는다:

```json
{
  "type": "function_call",
  "id": "fc_toolu_…",
  "call_id": "toolu_…",
  "name": "mcp__knob__send_mail",
  "arguments": "{\"to\":\"alice@example.com\",\"subject\":\"…\"}",
  "status": "completed",
  "approval": {
    "kind": "tool_approval",
    "version": 1,
    "server": "knob",
    "tool": "send_mail",
    "argsDigest": "sha256:…"
  }
}
```

- 클라이언트는 `approval`이 있는지로 승인 카드를 고른다. **도구 이름 접두어로 추측하지 않는다.**
- `arguments`는 실행될 인자 그대로다. 카드에는 도구별로 필요한 최소 정보만 보이고, 원문을 그대로 렌더링하거나 로그에 남기지 않는다.
- AskUserQuestion·ExitPlanMode 항목에는 `approval` 키가 없다.

### 3.3 답 — `function_call_output`

다음 요청(`previous_response_id` = 멈춘 응답)의 입력에 같은 `call_id`로 보낸다. `output`은 JSON 문자열이다:

| 결정 | `output` | 결과 |
|---|---|---|
| 승인 | `{"decision":"approve","argsDigest":"<받은 값 그대로>"}` | 원래 인자로 **정확히 1회** 실행 |
| 거부 | `{"decision":"deny"}` | 실행 안 함, 모델은 "사용자가 거부했다"를 받는다 |

승인은 **`decision`이 정확히 `approve`이고 `argsDigest`가 멈춘 호출의 digest와 같을 때만**이다. 다른 digest, 맨 문자열
`"approve"`, 빈 값, 해석할 수 없는 값은 모두 거부다. `ASK_USER_TIMEOUT_SECONDS`(기본 300초) 안에 답이 없으면 거부다.
한 호출에는 한 번만 답할 수 있다: 이미 처리된 `call_id`나 다른 `call_id`로 보내면 기존과 같이 400이다.

### 3.4 감사 로그

게이트웨이는 `tool approval: session=… call=… tool=… digest=… decision=approve|deny|timeout` 한 줄을 남긴다.
인자 값(메일 본문·토큰)은 남기지 않는다.

## 4. 한계

- 게이트는 `/v1/responses` 세션 경로에만 있다. 상태 없는 `/v1/agents/messages`(Noah)에는 멈출 세션이 없으므로 승인 대상
  도구는 항상 거부된다.
- stdio MCP 서버도 같은 방식으로 게이트된다(훅은 전송 방식과 무관). `readOnlyTools` 제외도 이름만 보므로 그대로 적용된다.
- 같은 배치에서 다른 PreToolUse 훅이 거부할 호출도 카드가 먼저 뜰 수 있다(그 경우 승인해도 실행되지 않는다).
