# Codex 공식 Python SDK 이전 검토

- 작성일: 2026-06-05
- 대상: `src/backends/codex/` (인트리 `CodexJsonRpcClient` → 공식 `openai-codex`)
- 상태: **검토 전용 (Review only)** — 구현 결정 아님
- 근거: 설치한 `openai-codex==0.1.0b3` wheel을 직접 인스펙션 (릴리스 노트 paraphrase 아님)

---

## 1. 요약 (TL;DR)

OpenAI 공식 Codex Python SDK가 PyPI에 존재한다. 우리 인트리 클라이언트가 직접 구현한 것과
**동일한 `codex app-server --listen stdio://` JSON-RPC 프로토콜**을 감싸며, 우리가 쓰는 RPC를
대부분 커버한다. 다만 다음 두 가지 때문에 **현 시점 전면 교체는 권장하지 않는다**:

1. **베타 프리릴리스**(0.1.0b3, 2026-06-03 출시)다. stable 1.0이 아니다.
2. 우리 게이트웨이의 핵심 기능인 **human-in-the-loop 승인 흐름**(턴 일시정지 → `requires_action`
   노출 → 별도 HTTP 요청으로 재개)을 공식 SDK의 고수준 async API가 **지원하지 않는다.**

현실적 경로는 **하이브리드 점진 도입**(타입·인증·`model_list` 먼저, transport·승인은 유지)이다.

---

## 2. 패키지 사실관계 (검증됨)

| 항목 | 값 |
|------|-----|
| 패키지명 | `openai-codex` (import `openai_codex`) |
| 최신 버전 | **0.1.0b3 (베타 프리릴리스, 2026-06-03)** — `pip install --pre` 필요 |
| 발행자 | OpenAI 공식 (repo `github.com/openai/codex`, `sdk/python`) |
| 런타임 의존성 | `openai-codex-cli-bin==0.137.0a4` (CLI 런타임을 pip 의존성으로 동봉), `pydantic>=2.12` |
| Python | `>=3.10` (우리 3.12 OK) |
| Transport | **stdio 전용** (이 빌드 기준) |
| 라이선스 | OpenAI 공식 |

> 주의: `codex-app-server-sdk`(import `codex_app_server`)는 emsi 개인의 **AGPL 서드파티**로 공식이 아니다. 혼동 금지.

### pydantic 호환성
우리는 현재 `pydantic==2.12.5` (claude-agent-sdk 경유)를 핀하고 있고, SDK 요구사항 `>=2.12`를
충족한다. 충돌 없음.

### Docker 영향
공식 SDK는 `openai-codex-cli-bin`으로 CLI 런타임을 pip 의존성으로 가져온다. 현재 `Dockerfile.codex`의
npm `@openai/codex@${CODEX_VERSION}` 글로벌 설치를 대체할 수 있어 빌드가 단순해질 여지가 있다.
(단, 버전 핀이 SDK에 묶이므로 CLI 버전 독립 제어는 어려워진다.)

---

## 3. API 매핑 (우리 코드 ↔ 공식 SDK)

저수준 `CodexClient`/`AsyncCodexClient`는 우리 `CodexJsonRpcClient`와 거의 1:1이고,
고수준 `Codex`/`AsyncCodex` + `Thread`는 더 높은 추상화를 제공한다.

| 우리 (`src/backends/codex/client.py`) | 공식 SDK | 비고 |
|---|---|---|
| `CodexJsonRpcClient` (인트리 transport) | `CodexClient` / `AsyncCodecClient` (stdio JSON-RPC) | 동일 프로토콜 |
| `_initialize` | `initialize()` / `_ensure_initialized()` | ✅ |
| `thread_start` | `thread_start(...)` | ✅ + `ephemeral`, `personality`, `service_tier` 등 추가 |
| `thread_resume` | `thread_resume(thread_id, ...)` | ✅ |
| `turn_start` | `turn_start(...)` / `Thread.run()` / `Thread.turn()` | ✅ + `turn_interrupt` / `turn_steer` |
| `model_list` | `model_list()` / `models()` | ✅ |
| 수동 `next_notification` 루프 | `register_turn_notifications` / `next_turn_notification` / `wait_for_turn_completed` / `stream_text` | ✅ **per-turn 라우팅** |
| (없음) | `thread_fork` / `thread_compact` / `thread_archive` / `thread_list` | 신규 |
| (없음) | `login_api_key` / `login_chatgpt` / `login_chatgpt_device_code` / `account` / `logout` | 신규 인증 API |
| `_translate_model_params` (temperature/topP/maxOutputTokens) | `turn_start` params (generated types) | ⚠️ 필드명 재확인 필요 |
| 이미지/멀티모달 input items | `_inputs.py` (확인 필요) | ⚠️ 미검증 |

### 기대 이점
- **per-turn notification 라우팅** → README에 명시된 우리 한계("턴이 단일 공유 프로세스로 직렬화,
  동시 멀티플렉싱 미구현") 해소 가능.
- 생성된(generated) pydantic 타입 → 우리가 in-tree 픽스처로 추측하던 페이로드 필드명을 SDK가 핀.
- 프로토콜 변경 시 SDK 업그레이드로 추적 (현재는 수동 재확인 필요, README "Current Limits" 참조).

---

## 4. 핵심 걸림돌: 승인(Approval) 처리

우리 게이트웨이의 차별 기능은 **모든 승인 요청을 가로채서**:
- 툴 정책(`allowed_tools`/`disallowed_tools`/`DISALLOWED_TOOLS`) 기반 **auto-deny**,
- `acceptEdits`일 때 파일변경 **auto-accept**,
- 그 외는 `AskUserQuestion`/`requires_action`으로 **사용자에게 노출하고 턴을 멈춘 뒤,
  별도 HTTP 요청(`resume_approval_with_client`)으로 결정을 받아 재개**

하는 것이다. 공식 SDK의 승인 모델과 비교:

| 경로 | 승인 처리 방식 | 우리 흐름 적합성 |
|---|---|---|
| 저수준 sync `CodexClient(approval_handler=...)` | `Callable[[method, params], decision]` — 리더 스레드 안에서 **즉시** 결정 반환 | auto-deny/auto-accept ✅ / 사용자 노출-재개 ❌ (스레드 블로킹 필요) |
| 고수준 `AsyncCodex` | `ApprovalMode` = **`auto_review`**(서버측 모델 리뷰어가 자동 결정) 또는 **`deny_all`**(`never`) 둘뿐 | ❌ 클라이언트가 승인을 보지 못함 |

**결론**: 고수준 async API는 승인을 app-server 내부(`approvals_reviewer=auto_review`)에서 자동 해결
하거나 전부 거부할 뿐, **개별 승인을 외부 사용자에게 노출/재개하는 경로가 없다.** 저수준 sync
`approval_handler`는 콜백이 즉답을 요구해 HTTP 왕복 동안 리더 스레드를 블로킹해야 하므로
(해당 연결의 모든 notification 라우팅 정지) 비현실적이다.

→ 우리 `resume_approval_with_client` 기능을 유지하려면 SDK의 transport/승인 레이어를 **우회**하거나,
SDK 위에 pending-approval 어댑터를 별도 구현해야 한다.

---

## 5. 미검증 항목 (이전 결정 전 확인 필요)

- [ ] `turn_start`의 generation control 필드명 (temperature / max_output_tokens 대응)
- [ ] 멀티모달(`input_image`) 입력 아이템 표현 (`_inputs.py`)
- [ ] async 경로가 inbound 승인 요청을 콜백 없이 어떻게 처리하는지 (서버측 reviewer 가정 확인)
- [ ] `_metadata_env` 변경 시 프로세스 재시작 / transport 리셋 동작이 SDK에서 어떻게 매핑되는지
- [ ] 베타→stable 사이 API 안정성 (b1→b3 변경 폭, breaking change 빈도)

---

## 6. 권장 (검토 결론)

1. **지금 전면 교체 보류.** b3 베타 + 승인 흐름 충돌 = 리스크 과다.
2. **하이브리드 점진 도입 후보** (낮은 위험순):
   - generated pydantic 타입을 참조용으로 채택 (우리 in-tree 페이로드 필드 검증).
   - `model_list` / 인증 API를 보조 경로로 시범 사용.
   - per-turn 라우팅 아이디어를 우리 transport에 역이식 (SDK 의존 없이 동시성 개선).
3. **transport·승인 전면 교체는 stable 1.0 + 승인 어댑터 설계 완료 이후 재검토.**
4. 게이트웨이 pass-through 철학(다운스트림으로 SDK 변경 전파, 호환 어댑터 미도입)과의 정합성도
   설계 단계에서 함께 판단.

---

## 부록: 참고 링크

- SDK 문서: https://developers.openai.com/codex/sdk
- 소스: https://github.com/openai/codex (`sdk/python`)
- PyPI: https://pypi.org/project/openai-codex/
- Changelog: https://developers.openai.com/codex/changelog
