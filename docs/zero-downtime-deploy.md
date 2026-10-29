# 무중단 배포 설계 (blue/green)

> 상태: **설계 초안 6판**, 구현 전. 기준 2026-10-29 `main` `d2fcd21`(#264 포함). 구현은 이 문서가 리뷰된 뒤
> [단계](#7-구현-단계)대로 나눠 올린다. 참고: ChatDRAGON UI의 같은 방식(#710, `scripts/deploy.sh`·`/health/drain`·
> nginx `active.conf`).
>
> 2판에서 바뀐 것(#265 1차 리뷰): 전환 직후 쉬는 세션에 409가 구조적으로 나던 순서를 없애고, 세션 소유권을 **서버가
> 넘겨받는**(handoff) 방식으로 바꿨다 — 클라이언트 재시도·헤더에 기대지 않는다([§5.2](#52-세션-소유권과-넘겨받기)).
> 플러그인 갱신은 #264의 `replace_tree()`를 바탕으로 남은 일만 적었다([§5.4](#54-기동-시-공유-파일-플러그인)).
>
> 3판(#265 2차 리뷰): 소유 경합을 클라이언트 503이 아니라 gateway 안의 bounded 재판정으로 풀고, 503은 내부망 장애의
> 최종 fallback으로만 남겼다. #264의 `.old-*` grace 시작점은 이미 맞다(`os.utime`) — 2판의 잘못된 진단을 지웠다.
>
> 4판(#265 3차 리뷰): "응답 헤더 전 실패면 다시 보낸다"를 지웠다 — 헤더 전에도 소유자는 턴을 시작했을 수 있다. 재판정은
> 요청 바이트를 보내기 전 단계와, 소유자가 dispatch id로 "받지 않음"을 원자적으로 확정해 준 경우만. 그 밖의 끊김은 불확실한
> 결과로 클라이언트에 알리고 다시 보내지 않는다([§5.3](#dispatch-id와-admission)).
>
> 5판(#265 4차 리뷰): ledger를 TTL로 지우지 않는다 — `admitted`는 턴이 끝날 때까지, 끝난 상태와 `refused`는 그 뒤
> `DISPATCH_RETENTION` 동안. id에 대상 boot id·발급 시각을 넣어, 늦은 도착은 tombstone 없이도 거절하고, 판정 근거가 없는
> settle은 언제나 "모름"(재전송 없음)이다.
>
> 6판(#265 5차 리뷰): drain 중인 색이 새 세션·울타리 세션·관리자 쓰기를 보낼 곳을 "활성 색"(nginx 상태)이 아니라 drain을
> 걸 때 검증·고정하는 **drain target**(instance + boot id)으로 정했다. 자기 자신은 target이 될 수 없고, target이 사라지면
> 추측하지 않고 503으로 멈춘다.

## 1. 목표와 비목표

**목표** — 새 이미지로 바꾸는 동안 사용자가 아무것도 알아채지 못한다. 구체적으로, 클라이언트(ChatDRAGON이든 헤더를 모르는
공개 API 사용자든)가 **아무것도 바꾸지 않아도**:

1. 새 요청은 새 컨테이너로 간다.
2. 옛 컨테이너에서 이미 돌고 있는 턴(스트리밍·백그라운드)은 끝까지 돈다 — 도구를 오래 쓰는 턴은 수십 분이다.
3. 옛 컨테이너에서 시작한 대화(`previous_response_id`)의 다음 턴이 오류 없이 이어진다 — 전환 직후 옛 컨테이너가 그
   세션을 아직 쥐고 있어도.
4. 옛 컨테이너가 띄운 AskUserQuestion·계획 승인 카드에 답하면, 답이 그 카드를 기다리는 프로세스에 닿는다.
5. 같은 턴이 두 번 실행되지 않는다(한 세션에는 언제나 CLI 하나).

**비목표**

- 여러 replica를 상시로 돌리는 수평 확장. 두 색이 함께 도는 것은 교체하는 동안뿐이다(그 사이 동시 턴 한도는 색마다
  따로 세므로 잠깐 두 배가 된다 — 받아들인다).
- `CLAUDE_ISOLATION_MODE=uid`의 무중단 교체. uid 레지스트리가 `flock(LOCK_NB)`로 한 프로세스만 허용하고
  (`src/backends/claude/uid_isolation.py`), 익명 uid 풀과 브로커의 orphan 판정이 프로세스·컨테이너마다 따로다.
  uid 모드에서는 deploy 스크립트가 거부하고 지금처럼 재시작으로 배포한다([§6](#6-uid-모드)).
- stale 백엔드(OpenCode·Codex frozen). 동작은 건드리지 않고, 두 색이 함께 돌아도 깨지지 않는지만 본다.

## 2. 지금 상태 — 무엇이 이미 되고 무엇이 막히나

| 항목 | 지금 | 교체 시 |
|---|---|---|
| 대화 이어가기(`previous_response_id`) | response id는 `resp_{session_id}_{turn}`로 프로세스 정보가 없다. 캐시에 없으면 `_resolve_response_session` → `get_session_async` → `_try_rehydrate_from_jsonl`이 `~/.claude/projects/<cwd>/<sid>.jsonl`로 세션을 다시 세우고 `create_client`가 `resume=`으로 잇는다 | **named user면 이미 된다** — 두 색이 같은 `claude_home`·`workspaces` 볼륨을 쓸 때. 익명(`user` 없음)은 404 → ChatDRAGON은 전체 기록으로 새로 보낸다(이미 처리) |
| 진행 중인 턴 | SIGTERM → uvicorn이 소켓을 닫고 열린 연결을 기다리지만, `stop_grace_period: 35s` 뒤 SIGKILL | 35초 넘는 턴은 끊긴다. 백그라운드 턴(`_BACKGROUND_RUNS`, 최대 3600s)은 lifespan 종료에서 죽는다 |
| drain | 없음. `/health`(정적)·`/health/ready`(인증·DB)만 | 새 턴을 막고 진행 중인 것만 끝내는 단계가 없다 |
| 카드(AskUserQuestion·ExitPlanMode) | 세션의 `pending_tool_call` + 프로세스 내 `asyncio.Event`에서 기다린다(`ASK_USER_TIMEOUT_SECONDS` 300) | 답이 다른 색으로 가면 400 `no pending tool call` → ChatDRAGON은 "카드 만료" |
| 세션에 묶인 다른 요청 | `GET/cancel /v1/responses/{id}`, `GET /v1/sessions/{id}/pending-events`, 세션 삭제 — 프로세스 메모리 | 다른 색은 404 |
| 같은 세션에 CLI 둘 | 옛 색은 쉬는 세션의 CLI를 TTL(`SESSION_MAX_AGE_MINUTES` 60) 동안 쥔다. 프로세스 간 잠금 없음 | 새 색이 같은 세션을 resume하면 transcript 하나에 CLI 둘이 쓴다 |
| 플러그인 트리 게시 | **#264로 해결됨**: `plugin_local_copy.replace_tree()` — 대상별 `flock`(프로세스 간), 요청마다 새 `<dest>.new-*`, `renameat2(RENAME_EXCHANGE)` 한 번, 옛 트리는 `<dest>.old-*`로 `OLD_GRACE_SECONDS`(600) 보존, 실패하면 옛 트리 유지. 관리자 refresh와 기동 replay가 같이 쓴다 | 반쯤 지워진 트리·겹친 staging은 더 없다 |
| 플러그인 레지스트리·버전 | 기동마다 `install_plugins.py`가 manifest를 replay — `claude plugin marketplace add`·`install`·`update`가 `installed_plugins.json`·`known_marketplaces.json`·`plugins/cache`를 고친다. 이 단계에는 프로세스 간 잠금이 없고, 바뀐 게 없어도 매번 다시 돈다 | 새 색 기동이 옛 색의 살아 있는 세션 밑에서 레지스트리·cache 버전을 바꾸고, 두 색의 CLI가 레지스트리 파일을 동시에 고칠 수 있다 |
| 기동 시 `_tmp_` 정리 | `SESSION_MAX_AGE_MINUTES*60`보다 mtime이 오랜 익명 워크스페이스를 지운다 | 새 색이 옛 색의 **살아 있는** 익명 워크스페이스를 지울 수 있다 |
| 관리자 쿠키 | `_COOKIE_SECRET = secrets.token_bytes(32)` (`src/admin_auth.py`) 프로세스마다 새로 | 교체하면 관리자가 로그아웃된다 |
| runtime-config 오버라이드 | 메모리에만(`src/runtime_config.py`) | 새 색은 env 기본값으로 뜬다. ChatDRAGON이 주기 reconcile로 다시 건다(`gateway_runtime_pin`) — 그 주기 동안 기본값 |
| `data/` 파일(프롬프트·MCP·플러그인 manifest 등) | tmp + `os.replace`로 원자적이지만 잠금은 프로세스 내 `RLock` | 두 색이 동시에 고치면 마지막 쓰기가 이긴다. 관리자 쓰기를 활성 색 한 곳으로만 보내면 문제없다 |
| 포트 | `ports: "${PORT}:${PORT}"` 하나 | 두 번째 컨테이너는 같은 호스트 포트를 못 잡는다 → nginx가 호스트 포트를 잡고 색은 내부 네트워크만 |
| MCP progress relay | CLI 자식이 **자기 컨테이너의 loopback**으로 다이얼(`note_origin`) | 그대로 된다. `MCP_RELAY_BASE_URL`을 nginx·공용 주소로 두면 깨진다 — 비워 둔다 |

## 3. 구조

```
클라이언트 ── :8000 ──> gateway-proxy (nginx) ──> gateway-blue | gateway-green (:8000, 내부망)
                                                  └ 같은 claude_home · ./data · ./workspaces · ./plugins
                                                  └ 서로를 내부망 이름으로 부를 수 있다 (§5.2 넘겨받기·전달)
```

- `docker-compose.yml`의 `gateway` 서비스를 `gateway-blue`·`gateway-green`(같은 이미지, profile로 골라 띄움)과
  `gateway-proxy`(nginx)로 나눈다. 호스트 포트 `${PORT}`는 proxy만 잡는다. 색은 `ports`를 내지 않는다.
- named volume `claude_home`은 고정 `name:`을 준다 — compose project가 달라도 같은 볼륨을 쓴다(project별
  `<project>_claude_home`이 생기면 rehydrate가 404가 된다).
- 활성 색은 `docker/nginx/state/active.conf` 한 줄(`set $gateway_upstream gateway-<색>:8000;`). deploy 스크립트가
  바꾸고 `nginx -s reload` — reload는 graceful이라 열린 연결(SSE 턴 스트림)은 옛 worker가 끝까지 옛 색에 잇는다.
- nginx 설정은 ChatDRAGON #710과 같은 원칙: `proxy_buffering off`, `proxy_request_buffering off`,
  `proxy_read_timeout`을 턴 길이보다 길게(SSE keepalive가 있으므로 3600s), `resolver 127.0.0.11`로 색 이름을
  요청마다 푼다(그 색이 없어도 nginx가 죽지 않는다), `client_max_body_size 0`(업로드 한도는 앱이 본다).
  **라우팅은 활성 색 하나로만 한다** — 세션을 어느 색이 쥐고 있는지는 nginx가 아니라 gateway가 안다([§5.2](#52-세션-소유권과-넘겨받기)).
- 각 색은 `GATEWAY_INSTANCE=<blue|green>`을 받아 응답 헤더 `x-gateway-instance`와 `/health`에 싣는다 — deploy
  스크립트가 proxy를 거친 응답이 정말 새 색에서 왔는지 확인한다(200만으로는 옛 색일 수 있다). 클라이언트에게는 정보일
  뿐이고 라우팅에 쓰지 않는다.
- 색끼리 부르는 내부 경로(`/internal/sessions/…`)는 내부망의 다른 색에서만 받는다: nginx는 `/internal/`을 밖으로
  내지 않고(404), 앱은 `ADMIN_API_KEY`가 아니라 기동 때 만든 공유 비밀값(`data/instance_secret`, 0600)의 HMAC로 확인한다.

## 4. 배포 순서 (`scripts/deploy.sh`)

1. **빌드** — 새 이미지를 만든다. uid 모드(`CLAUDE_ISOLATION_MODE=uid`)면 여기서 거부하고 재시작 배포를 안내한다.
   **활성 색이 handoff를 아는 버전인지 확인한다**([§7 배포 gate](#배포-gate)) — 모르는 버전이면 무중단 교체를 거부한다.
2. **유휴 색 기동** — 활성 색이 blue면 green을 같은 볼륨으로 띄운다. 플러그인 replay는 [§5.4](#54-기동-시-공유-파일-플러그인)대로
   잠금 + "바뀐 게 없으면 건너뜀".
3. **준비 확인** — green 안에서 `/health/ready`가 200, `/health`의 instance가 `green`, green이 blue의 내부 주소에
   닿는다(`/internal/ping`).
4. **blue를 drain으로 둔다** — `POST /internal/drain {"target": "green", "target_boot_id": "<3단계에서 읽은 값>"}`.
   blue는 먼저 target을 **검증**한다: 자기 자신이 아니고, instance 등록과 잠금으로 살아 있고, `/internal/ping`이 같은 boot
   id를 답해야 한다 — 하나라도 아니면 drain을 거절하고(400) 아무것도 바꾸지 않는다. 검증되면 그 target을 **drain target**으로
   고정한다. 이 순간부터 blue는:
   - **새 세션을 만들지 않는다**: 세션을 새로 여는 요청(`previous_response_id` 없는 `/v1/responses`,
     `/v1/agents/messages`)은 drain target으로 내부 전달한다(아직 nginx가 blue로 보내는 동안의 요청 — [§5.2](#52-세션-소유권과-넘겨받기)의 "전달").
   - **쉬는 세션을 놓는다**: 진행 중인 턴도 카드도 없는 세션의 CLI를 disconnect하고(transcript는 남는다) 소유를
     푼다. 놓은 세션은 drain 동안 blue에서 다시 열지 않는다(울타리) — 그 세션 요청이 blue에 오면 drain target으로 전달한다.
   - 관리자 쓰기도 drain target으로 전달한다([§5.5](#55-관리자-상태)).
   - 진행 중인 턴·카드를 기다리는 세션·백그라운드 턴은 그대로 둔다. 끝나면 그 세션도 놓는다.
   - `/health/ready` → 503.
5. **전환** — `active.conf`를 green으로 바꾸고 reload. proxy를 거친 `/health`가 `green`을 답할 때까지 확인.
   4와 5 사이에 blue로 온 요청은 위대로 drain target에 전달되거나 blue가 아직 쥔 세션이면 blue가 처리한다 — 어느 쪽도
   409가 아니다. 전환은 drain target을 바꾸지 않는다: blue에게 "어디로 보낼지"는 `active.conf`가 아니라 4단계에서 고정한
   drain target 하나뿐이다(`active.conf`는 nginx만 읽는다).
6. **drain 대기** — `GET /health/drain`(loopback 전용)을 주기적으로 읽어 아래가 모두 0이 될 때까지 기다린다.
   `DRAIN_TIMEOUT_SECONDS`(기본 3600)을 넘기면 남은 것을 보여 주고 멈춘다 — 강제로 내릴지는 운영자가 정한다
   (`--force`).
7. **정지** — blue에 SIGTERM. 이때는 열린 연결이 없으므로 lifespan 종료(세션 disconnect)가 곧바로 돈다.
   `stop_grace_period`는 lifespan 종료만 덮으면 된다(지금의 35s 유지). 마지막으로 drain 동안 보존한 옛 플러그인
   트리를 치운다([§5.4](#54-기동-시-공유-파일-플러그인)).

drain(4)을 전환(5)보다 **먼저** 하는 이유: 전환을 먼저 하면 green이 받은 다음 턴이 blue가 아직 쥔 쉬는 세션과 부딪힌다.
drain을 먼저 걸면 blue가 쉬는 세션을 놓기 시작한 뒤에 green으로 요청이 가고, 그래도 아직 놓지 못한 세션은 §5.2의
넘겨받기가 그 자리에서 해결한다.

되돌리기: 6단계 중이면 `active.conf`를 blue로 되돌리고 `DELETE /internal/drain`(blue는 drain target을 지우고 다시 새
세션을 받으며 울타리도 걷는다). green이 그 사이 연 세션은 green이 계속 쥐고, 그 세션 요청이 blue로 오면 일반 소유자 전달
([§5.2](#52-세션-소유권과-넘겨받기))로 green이 처리한다. green을 내리려면 같은 절차로 `drain(green, target=blue)`를 건다. 7단계 뒤라면
같은 스크립트로 옛 이미지를 다시 배포한다.

**drain target이 사라지면**(green이 죽었거나 boot id가 바뀜 — 전달 전에 매번 instance 잠금·boot id로 확인한다): blue는
새 세션·울타리 세션·관리자 쓰기를 **자기에게 다시 열지도, 다른 곳으로 추측해 보내지도 않는다** — retryable 503
(`drain_target_unavailable`)로 답하고 deploy 스크립트가 경보를 띄운다. 운영자의 선택은 둘이다: 되돌리기(위), 또는 새
target으로 drain을 다시 걸기(`POST /internal/drain`은 drain 중에도 target을 검증된 새 값으로 바꿀 수 있다). drain target이
자기 자신으로 풀리는 경로는 없다 — 자기 앞으로의 전달은 구현에서 assert로 막고(재귀 전달 금지와 별개), 테스트로 고정한다.

### `/health/drain` 계약

```json
{
  "instance": "blue",
  "draining": true,
  "active_turns": 0,
  "background_turns": 0,
  "pending_interactions": 0,
  "forwarding": 0,
  "open_requests": 0,
  "owned_sessions": 0
}
```

- `active_turns` — 스트리밍 중인 `/v1/responses`(세션 `active_response_*`), `/v1/agents/messages` 실행 포함.
- `background_turns` — `_BACKGROUND_RUNS`에 남은 것.
- `pending_interactions` — 답을 기다리는 카드(`pending_tool_call`이 있고 `input_event`가 안 풀린 세션). 300s 뒤엔
  저절로 만료되므로 drain이 무한히 걸리지 않는다.
- `forwarding` — blue가 green으로 내부 전달 중인 요청(스트림 포함). 0이 되기 전에 blue를 내리면 그 스트림이 끊긴다.
- `open_requests` — 위에 속하지 않는 열린 HTTP 요청(업로드·파일 API·관리자 작업). ChatDRAGON `drain.py`의
  `InflightMiddleware`와 같은 방식(probe 경로는 세지 않는다).
- `owned_sessions` — 아직 소유를 쥔 세션 수. 위가 모두 0이면 이것도 0이어야 한다(놓기는 즉시) — 0이 아니면 스크립트가
  이름을 보여 주고 멈춘다.

## 5. 세부 설계

### 5.1 대화 이어가기

이미 되는 경로를 **유지**한다: 두 색이 같은 볼륨을 쓰고, `user`가 있으면 rehydrate + `resume`. 바꿀 것은 없고,
deploy 테스트에 "blue에서 시작한 대화를 green이 잇는다"를 넣어 고정한다. 익명 대화는 지금처럼 새로 시작된다
(ChatDRAGON은 늘 `user`를 보낸다).

### 5.2 세션 소유권과 넘겨받기

한 세션의 CLI는 언제나 한 프로세스만 쥔다. 그 사실을 디스크의 잠금으로 두고, 요청이 소유하지 않은 색에 오면
**소유한 색과 그 자리에서 정리**한다 — 클라이언트에게 409를 돌려주지 않는다.

**소유 잠금.** 세션이 CLI를 쥐는 동안 `data/session-owners/<session_id>.lock`(두 색이 같이 쓰는 `./data`)에
`flock(LOCK_EX)`를 잡고, 파일에 소유 instance 이름을 적는다(두 컨테이너가 같은 볼륨 = 같은 커널이므로 컨테이너를 넘어
동작한다). 놓을 때 푼다. 프로세스가 죽으면 커널이 푼다 — 그래서 "파일에 이름이 있는데 잠금이 없다"는 "소유자가 죽었다"이고
누구나 가져갈 수 있다. 같은 프로세스 안의 기존 세션 잠금(`asyncio.Lock`)은 그대로 두고, 이 파일 잠금은 그 바깥 경계로만
쓴다. instance 생존은 `data/instances/<name>.lock`의 `flock`으로 같은 방식으로 본다.

**요청 처리.** 세션 id로 귀결되는 모든 요청(`previous_response_id`가 있는 `/v1/responses`, `GET`·`cancel`
`/v1/responses/{id}`, `/v1/sessions/{id}/…`)에서, 그 세션을 이 프로세스가 쥐고 있지 않으면:

| 소유 상태 | 처리 |
|---|---|
| 아무도 안 쥠 | 지금처럼 잠금을 잡고 rehydrate + resume |
| 다른 instance가 쥠, 그 세션이 **쉬는 중** | **넘겨받기**: 소유자에게 `POST /internal/sessions/<sid>/release`. 소유자는 세션 잠금 안에서 "진행 중인 턴·카드·백그라운드 없음"을 확인하고 CLI를 disconnect한 뒤 소유를 푼다. 받은 쪽은 그 잠금을 잡고 resume. 놓은 뒤 그 세션 요청이 옛 소유자에게 오면, 옛 소유자는 잠금의 현재 소유자에게 전달한다(아무도 안 쥐었으면 drain 중일 때는 drain target으로, 아니면 직접 연다 — 잠금이 CLI 하나를 지킨다) |
| 다른 instance가 쥠, 그 세션이 **진행 중**(턴·카드·백그라운드) | **전달**: 요청을 소유자에게 그대로 내부 reverse-proxy한다(본문·헤더·스트리밍 응답 그대로). 카드 답(`function_call_output`)은 카드를 기다리는 프로세스에 닿고, 진행 중인 턴의 `GET`/`cancel`/`pending-events`도 그 프로세스가 답한다 |
| 소유자가 죽음(잠금 없음) | 아무도 안 쥠과 같다 |

- **중복 실행 방지**: release는 소유자의 세션 잠금 안에서 "진행 중 없음"을 확인하고 놓기까지를 한 번에 한다 — 그 사이
  소유자에게 새 턴이 시작될 수 없다(같은 잠금). 놓은 뒤 소유자는 울타리 때문에 그 세션을 다시 열지 않으므로, 한 턴이 두
  색에서 동시에 돌 수 없다. release가 "진행 중"을 답하면 넘겨받기 대신 전달로 간다. 전달된 요청은
  `x-gateway-forwarded`를 달고, 전달받은 쪽은 다시 전달하지 않는다(루프 방지).
- **소유 경합은 gateway 안에서 다시 푼다**: 소유가 옮겨 가는 순간(release와 다른 요청의 잠금 시도가 겹침, 전달받은 쪽이
  막 소유를 놓음, 소유자가 막 죽음)에는 "누가 소유자인가"를 다시 판정해야 한다. 이 판정과 재시도는 **요청을 받은 gateway가
  한다**: 소유 잠금 → release/전달을 처음부터 다시 판정하는 bounded loop(총 기한 `HANDOFF_DEADLINE` 10s, 50ms에서 시작해
  두 배씩 최대 1s backoff, 클라이언트가 끊으면 즉시 멈춤). 이 loop가 되풀이하는 것은 **아무 데서도 실행되지 않았음이 확정된**
  단계뿐이다:
  - 소유 판정, release 요청(부작용은 쉬는 CLI를 놓는 것뿐이고 두 번 해도 같다), 이 프로세스의 세션 잠금 시도.
  - 전달의 **pre-dispatch**: 소유자에게 연결을 맺기 전, 또는 연결은 맺었지만 요청 바이트를 한 바이트도 보내기 전의 실패.
  - 요청 바이트를 보내기 시작한 뒤의 실패는 [§5.3의 dispatch 판정](#dispatch-id와-admission)을 거쳐 소유자가 **그 요청을
    받아들이지 않았다고 확정해 준 경우에만** 되풀이한다. 응답 헤더를 받았는지는 판정 기준이 아니다 — 헤더 전에도 소유자는
    이미 세션 잠금을 잡고 CLI·도구를 실행했을 수 있다.
  - 이 프로세스가 세션 잠금을 잡고 턴을 시작한 뒤는 지금처럼 그 턴의 일이다(되풀이 없음).
- **최종 fallback**: 기한 안에 소유자를 확정하지 못하면(예: 두 색의 내부망이 끊김) retryable 503 + `Retry-After`를 돌린다 —
  정상 전환에서는 나오지 않는 응답이고, 나왔다면 운영 장애로 본다(`handoff_deadline_exceeded` 로그·메트릭).
- **대기 한도**: release는 쉬는 CLI를 멈추는 시간(capped disconnect)만큼 걸릴 수 있어 45초까지 기다리고, 그 안에 답하지 않거나 "busy"면 소유자가 살아 있는 한 전달로 간다. drain 중에는 쉬는 세션을 미리 놓으므로 요청이 이 대기를 만나는 일은 드물다.
- **전달의 범위**: drain 중에만 생기는 일이 아니다 — 같은 세션에 대한 요청이 어쩌다 다른 색으로 와도 같은 규칙이다. 다만
  평상시(한 색만 돌 때)에는 소유자가 언제나 자기 자신이라 이 경로를 타지 않는다.
- **새 세션 요청**(세션 id가 없는 요청)이 drain 중인 색에 오면 그 색의 **drain target**으로 전달한다(§4의 4단계). drain
  중이 아닌 색은 새 세션을 직접 연다. "활성 색"(nginx의 `active.conf`)은 gateway의 판단에 쓰지 않는다.

**가용성 수준.** 클라이언트가 무엇을 아느냐에 따라 달라지지 않는다 — 판정·전달·경합 재시도가 모두 서버에 있다. 아래
"오류 없음"은 두 색이 서로 닿는 정상 전환을 전제로 하며, 그때 클라이언트가 보는 것은 길어야 `HANDOFF_DEADLINE` 안의
지연이다. 두 색의 내부망이 끊긴 장애에서는 503 fallback이 클라이언트에 보인다.

| 클라이언트 | 전환 중 다음 턴 | 전환 중 카드 답 | 진행 중인 턴 |
|---|---|---|---|
| ChatDRAGON | 오류 없음(넘겨받기) | 소유자에게 닿음(전달) | 끝까지 |
| 헤더를 모르는 공개 API | 같음 | 같음 | 같음 |
| 익명(`user` 없음) | 지금과 같다 — 새 대화(전체 기록 재전송) | 진행 중 세션은 전달로 닿음 | 끝까지 |

1판의 "응답 헤더로 색 고정 + ChatDRAGON 변경"(옛 §5.3 A)은 빼었다: 위 서버 쪽 전달이 같은 일을 클라이언트 변경 없이 하고,
헤더를 배우기 전의 요청·헤더 없는 클라이언트에서도 같다.

### 5.3 전달(reverse-proxy)의 계약

- 대상은 `data/instances/<name>.json`의 내부 주소(`http://gateway-<색>:8000`), 살아 있음은 instance 잠금으로 본다.
- 원 요청의 메서드·경로·쿼리·본문·`Authorization` 등 헤더를 그대로 보내고, 응답(상태·헤더·SSE 스트림)을 바이트 그대로
  돌려준다. 전달하는 쪽은 본문을 해석하지 않는다 — 인증·세션 소유자 검사(`user`)는 받는 쪽이 원래대로 한다.
- 클라이언트가 끊으면 전달 연결도 끊는다(받는 쪽은 지금처럼 끊김을 취소로 본다).
- `x-gateway-forwarded: <보낸 instance>`와 instance 비밀값 HMAC를 단다. 받는 쪽은 HMAC가 맞을 때만 그 헤더를 믿고,
  밖에서 온 `x-gateway-forwarded`는 지운다. 이 HMAC는 **호출자 인증**일 뿐 중복 방지 키가 아니다 — 중복 방지는 다음의
  dispatch id가 맡는다.

#### dispatch id와 admission

전달 연결이 요청 바이트를 보낸 뒤 끊기면 결과는 **모른다**: 소유자가 그 요청을 받아 턴(도구·MCP의 외부 부작용 포함)을
시작했을 수도, 본문을 다 받기 전에 끊겼을 수도 있다. 그래서 다시 보내기 전에 소유자에게 묻고, 소유자가 원자적으로 답한다.

- 전달하는 쪽은 전달마다 새 dispatch id를 단다: `x-gateway-dispatch-id: <대상 boot id>.<발급 시각 ms>.<무작위 128비트>`.
  `boot id`는 대상 프로세스가 기동 때 만든 값(`/internal/ping`과 instance 등록에 실린다). 같은 원 요청의 재판정 loop 안에서는
  새로 보낼 때마다 새 id다. 두 색은 같은 호스트라 같은 시계를 쓴다(발급 시각을 대상이 잰다).
- **받아들이기(admission)**: 소유자는 본문을 다 받은 뒤, 세션 잠금과 **ledger 잠금**(settle과 같은 잠금) 안에서 턴 시작과
  한 번에 ledger에 `id → admitted`를 적는다 — settle과 admission이 같은 id에 대해 동시에 일어나도 한쪽만 이긴다. 다음이면 받아들이지 않고 `refused`를 적은 뒤 거절한다(전달하는 쪽은 "받지 않음"으로 보고 재판정한다):
  - id의 boot id가 자기 것이 아님(다른 프로세스 앞으로 보낸 요청),
  - 발급 시각이 `DISPATCH_ACCEPT_WINDOW`(기본 `HANDOFF_DEADLINE`, 10s)보다 오래됨 — 늦게 도착한 요청은 ledger에 흔적이
    없어도 절대 받아들여지지 않는다,
  - ledger에 이미 그 id가 있음(어떤 상태든).
- **ledger의 수명** — 시간으로 지우는 것은 끝난 항목뿐이다:
  - `admitted`는 그 요청의 턴이 **끝날 때까지 지우지 않는다**(스트리밍·백그라운드 모두, 몇 시간이 걸려도).
  - 끝나면 `completed`/`failed`/`cancelled`로 바뀌고, 끝난 시각부터 `DISPATCH_RETENTION`(기본 `max(10 분, 3 ×
    DISPATCH_ACCEPT_WINDOW)`) 동안 남는다.
  - `refused`는 적은 시각부터 `DISPATCH_RETENTION` 동안 남는다. 그 뒤에는 발급 시각이 accept window를 한참 넘었으므로 같은
    id가 늦게 와도 위의 "발급 시각" 규칙이 거절한다 — tombstone이 사라져도 울타리는 남는다(`DISPATCH_RETENTION` >
    `DISPATCH_ACCEPT_WINDOW`가 그 조건이고, 기동 시 검사한다).
- **settle** — 요청 바이트를 보낸 뒤 연결이 끊기면 전달하는 쪽은 `POST /internal/dispatch/<id>/settle`을 부른다. 소유자는
  ledger 잠금 안에서 원자적으로:

  | ledger | 답 | 전달하는 쪽 |
  |---|---|---|
  | `admitted` / `completed` / `failed` / `cancelled` | 받음 | 다시 보내지 않음 → 불확실한 결과 |
  | `refused` | 받지 않음 | 재판정 loop |
  | 없음, boot id가 자기 것이고 발급 시각이 `DISPATCH_RETENTION` 안 | `refused`를 적고 받지 않음 | 재판정 loop |
  | 없음, 그 밖(boot id가 다름 = 소유자가 재기동했다, 또는 발급 시각이 retention보다 오래됨) | **모름** | 다시 보내지 않음 → 불확실한 결과 |

  세 번째 줄이 안전한 이유: 받아들였다면 턴이 끝날 때까지 `admitted`로, 끝난 뒤 `DISPATCH_RETENTION` 동안 끝난 상태로
  남으므로, 발급 시각이 retention 안인데 항목이 없다면 받아들인 적이 없다 — 그리고 `refused`를 적었으니 앞으로도 받지
  않는다. 판정할 근거가 없는 경우(네 번째 줄)는 언제나 **모름**이고, "받지 않음"으로 보지 않는다.
- **불확실한 결과**: "받음"·"모름"이거나 settle 자체가 기한 안에 답하지 않으면(소유자가 죽었거나 닿지 않음), 전달하는 쪽은
  클라이언트에게 그대로 알린다 — 스트림이 이미 열렸으면 지금의 스트림 오류(`response.failed`,
  `error.code = "forward_interrupted"`), 아직 응답을 시작하지 않았으면 502 `forward_interrupted` + `x-should-retry: false`.
  절대 자동으로 다시 보내지 않는다. 소유자 쪽 턴은 지금 직접 연결된 클라이언트가 끊겼을 때와 같은 규칙으로 처리된다(스트리밍
  턴은 취소, 백그라운드 턴은 계속) — 이미 일어난 부작용은 한 번이다.
- 이어 붙이기(끊긴 스트림을 소유자의 진행 중인 턴에 다시 attach해 남은 이벤트를 받는 것)는 하지 않는다 — 이벤트 journal이
  없는 지금의 `/v1/responses`에서는 새 계약이다. 불확실한 결과의 빈도가 문제가 되면 별도 과제로 한다.
- ledger는 소유자 프로세스 메모리에 둔다(받아들이는 것도 settle을 답하는 것도 그 프로세스뿐이고, 프로세스가 바뀌면 boot id가
  바뀌어 위 표의 "모름"으로 간다). 크기는 진행 중인 전달 + `DISPATCH_RETENTION` 동안의 전달 수로 묶인다.
- 직접 연결된 요청(전달이 아닌 것)은 이 경로와 무관하다 — 지금과 같다.

### 5.4 기동 시 공유 파일 (플러그인)

**바탕(#264, 이미 main)**: 트리 게시는 `replace_tree()`가 맡는다 — 대상별 프로세스 간 `flock`, 유일한 staging,
`RENAME_EXCHANGE` 한 번, 옛 트리는 `<dest>.old-*`. 이 부분은 다시 만들지 않는다.

남은 것은 그 바깥이다.

1. **replay 전체 잠금**: `install_plugins.py`의 manifest replay 전체(`marketplace add`·`install`·`update`·`uninstall`)를
   `claude_home/.plugin-replay.lock` `flock`으로 감싼다. 관리자 경로(`plugin_admin_service`)와 `plugin_autorefresh`도
   같은 잠금을 쓴다 — 두 색의 `claude plugin …`이 `installed_plugins.json`·`known_marketplaces.json`을 동시에
   고치지 않게. (`replace_tree`의 대상별 잠금과는 다른, 더 바깥 잠금이다.)
2. **바뀐 게 없으면 건너뛰기**: replay가 끝나면 `data/plugin-replay.digest`에 입력(manifest 내용 + 원격 marketplace별
   clone한 커밋 + 로컬 원본의 트리 digest + CLI 버전)의 digest를 남긴다. 다음 기동의 입력 digest가 같으면 아무것도 하지
   않는다 — 같은 manifest로 green을 띄울 때 blue의 살아 있는 세션 밑에서 레지스트리·cache가 바뀌지 않는다. 원격의 새 커밋을
   알려면 `git ls-remote`만 하고 clone은 다르면 한다.
3. **바뀐 배포에서 옛 트리의 수명**: manifest·원본이 **바뀐** 배포는 replay가 돌고, `replace_tree`가 옛 트리를
   `.old-*`로 옮긴다. 옛 색(drain 중)의 CLI는 그 경로를 이미 연 fd로만 옛 내용을 보고, 경로로 새로 여는 것은 새 트리다 —
   drain 동안 옛 색의 세션이 새 플러그인 내용을 볼 수 있다는 뜻이고, 이것은 받아들인다(세션 시작 때 읽는 정의는 이미
   읽혀 있다). 지워지면 안 되는 것은 `.old-*` 자체다(옛 fd·cwd가 그 안을 가리킨다):
   - `OLD_GRACE_SECONDS`(600)를 env `PLUGIN_OLD_GRACE_SECONDS`로 열고, deploy 중에는 `DRAIN_TIMEOUT_SECONDS` 이상이
     되게 한다. 또는 drain 중인 instance가 살아 있는 동안 `_sweep`이 `.old-*`를 지우지 않게 한다(instance 잠금으로 판정) —
     구현 PR에서 둘 중 하나로 정한다.
   - deploy 스크립트는 7단계(옛 색 정지) 뒤에 `.old-*`를 치운다.
   - grace의 시작점은 #264가 이미 맞춰 두었다: `_publish`가 옛 트리를 `.old-*`로 옮긴 직후 `os.utime(old)`로 mtime을
     찍고(`src/plugin_local_copy.py` 363–364), `_sweep`이 그 mtime으로 잰다 — 600초는 게시 시점부터다. 남는 문제는
     **길이**뿐이다: grace 600초 < drain 최대 3600초.
   - **확인할 것**: `claude plugin update`가 `plugins/cache/<mp>/<plugin>/<옛 버전>`을 지우는지. 지운다면 옛 색의 CLI가
     그 경로를 참조하므로, replay 잠금 안에서 update가 지우려는 옛 버전을 drain이 끝날 때까지 남기는 방법이 필요하다.
4. **`_tmp_` 정리**: 익명 워크스페이스 이름에 instance를 넣거나(`_tmp_<instance>_…`), 기동 정리가 다른 살아 있는 instance의
   것을 건드리지 않게 한다. 다른 색이 내려간 뒤 남은 것은 다음 기동 정리가 치운다.
5. **entrypoint의 chown**: 같은 uid로 다시 소유권을 맞추는 것이라 두 색이 함께 돌아도 안전하다(시간만 든다).

### 5.5 관리자 상태

- **쿠키 비밀값**: `data/admin_cookie_secret`(0600)에 한 번 만들어 두 색이 같이 읽는다 — 교체해도 로그아웃되지 않는다.
  `ADMIN_COOKIE_SECRET` env가 있으면 그것을 쓴다.
- **runtime-config**: 오버라이드를 `data/runtime_config.json`(tmp + `os.replace`)에 남기고 기동 때 읽는다 — green이
  기본값으로 뜨는 틈이 없어진다. ChatDRAGON의 reconcile은 그대로 두어도 된다(gateway가 이미 오버라이드를 들고
  있으면 건드리지 않는다).
- **관리자 쓰기**: proxy는 활성 색으로만 보내므로 `data/`를 쓰는 곳은 한 곳이다. drain 중인 색은 관리자 쓰기를 drain target으로
  전달한다(옛 화면의 늦은 저장이 옛 색에만 쓰이지 않게).

### 5.6 종료

- drain 뒤 SIGTERM이면 열린 연결이 없으므로 지금의 lifespan 종료가 그대로 돈다.
- 스크립트 없이 `docker stop`한 경우를 위해 uvicorn에 `--timeout-graceful-shutdown`(예: 25s)을 주어 lifespan
  종료가 SIGKILL 전에 반드시 돈다 — 지금은 열린 SSE가 있으면 lifespan 종료가 아예 안 돈다.

## 6. uid 모드

uid 모드는 레지스트리 잠금 때문에 두 번째 프로세스가 기동을 거부한다. deploy 스크립트는 uid 모드를 감지하면 무중단
교체를 거부하고 "재시작 배포(짧은 끊김)"를 안내한다. 공유 uid 레지스트리·풀(프로세스 간 잠금이 있는 파일 레지스트리 +
브로커가 다른 컨테이너의 프로세스를 보는 방법)은 별도 과제다.

## 7. 구현 단계

| PR | 내용 | 클라이언트 영향 |
|---|---|---|
| 1 | 세션 소유 잠금 + instance 등록·생존, 넘겨받기(`/internal/sessions/<sid>/release`)·울타리, 전달(reverse-proxy, `x-gateway-forwarded`+HMAC), `GATEWAY_INSTANCE`·`x-gateway-instance` | 없음(한 색만 돌면 경로를 안 탄다) |
| 2 | drain(`/internal/drain`, `/health/drain`, 새 세션 전달, 쉬는 세션 즉시 놓기, ready 503), `--timeout-graceful-shutdown`, 쿠키 비밀값·runtime-config 영속화 | 없음 |
| 3 | 플러그인 replay 전체 잠금·digest skip·옛 트리 수명(§5.4), `_tmp_` 정리 | 없음 |
| 4 | compose(blue/green/proxy, 고정 볼륨 이름), nginx 설정, `scripts/deploy.sh`(+ `migrate`), 운영 문서 | 운영 절차 바뀜 |

ChatDRAGON 변경은 필요 없다. PR 1–3은 단일 컨테이너 배포에서도 해가 없다(drain을 안 부르고 색이 하나면 지금과 같다).

### 배포 gate

- **첫 무중단 교체는 PR 1–3을 가진 버전에서 그 다음 버전으로만** 한다. 지금 단일 컨테이너에서 처음 옮기는 배포
  (`scripts/deploy.sh migrate`)는 재시작 배포다: blue를 먼저 healthy로 띄우고 옛 컨테이너를 내린 뒤 proxy가 포트를 잡는다
  (이 한 번은 포트를 넘기는 몇 초의 끊김과, 옛 컨테이너의 진행 중 턴·카드를 잃을 수 있다 — 한가한 시간에).
- deploy 스크립트는 활성 색의 `/health`가 handoff 지원 버전(`capabilities: ["handoff", "drain"]`)을 답하지 않으면 무중단
  교체를 거부하고 재시작 배포를 안내한다 — handoff를 모르는 옛 색과 새 색이 함께 돌면 §5.2가 성립하지 않는다.

## 8. 검증 계획

- **단위**: 소유 잠금(같은 프로세스·다른 프로세스·죽은 소유자), release의 원자성(진행 중이면 거부, 놓은 뒤 울타리),
  전달(본문·헤더·SSE 바이트 그대로, 클라이언트 끊김 전파, 재전달 금지, 밖에서 온 forwarded 헤더 제거), drain 상태 전이와
  `/health/drain` 카운트, 새 세션 전달, 쿠키 비밀값 공유, replay 잠금·digest skip, `.old-*` 수명(grace가 drain보다 길거나 drain 중 sweep 보류),
  `_tmp_` 정리가 다른 instance를 건드리지 않음.
- **두 프로세스 통합**(`tests/fixtures/fake_anthropic_api.py` + 번들 CLI, 같은 HOME·data·workspaces, 두 uvicorn) — 리뷰
  acceptance 그대로:
  1. blue에서 턴을 마친 직후 drain + 전환, blue가 아직 쉬는 세션을 쥔 채 다음 턴을 green으로 → 409·실패 없이 완료,
     transcript에 CLI 하나만 쓴 흔적.
  2. blue에서 긴 턴이 도는 중에 같은 세션의 요청이 green으로 → blue로 전달, 턴은 끝까지, 중복 실행 없음.
  3. blue의 카드 대기 중 답이 green으로 → blue로 전달되어 카드가 풀림.
  4. 헤더를 모르는 클라이언트로 1–3 반복.
  4b. 소유 경합 강제: release와 다른 요청의 잠금 시도, 전달받은 쪽의 소유 해제, 소유자 종료를 테스트 hook으로 겹치게 해
     수백 번 반복 — 클라이언트는 503 없이 모두 성공하고, 각 턴은 정확히 한 번 실행된다(transcript·fake API 호출 수로 확인).
     내부망을 끊은 경우에만 기한 뒤 503이 나온다.
  4c. 헤더 전 끊김(리뷰 acceptance): 전달받은 소유자가 요청을 받아들여 fake API로 **부작용이 있는 도구를 한 번 실행한 뒤**,
     응답 헤더를 보내기 전에 전달 연결을 끊는다 → settle이 "받음", 클라이언트는 `forward_interrupted`(재시도 금지)를 받고,
     도구 부작용·CLI 실행 횟수는 정확히 한 번. 같은 실험을 "본문 일부만 보낸 뒤 끊김"으로 바꾸면 settle이 "받지 않음",
     늦게 도착한 같은 id는 거절되고, 재판정으로 한 번만 실행된다.
  4d. ledger 수명(리뷰 acceptance): 소유자가 id K를 받아들인 뒤 가짜 시계를 `DISPATCH_RETENTION`·accept window 너머로
     진행시키되 턴은 아직 실행 중(실제 비동기 대기) → 전달 연결 단절 → `settle(K)`는 "받음"(절대 "받지 않음" 아님),
     fake API 부작용 1회. 턴을 끝낸 뒤 retention 안의 settle도 "받음", retention 뒤는 "모름"(재전송 없음). 같은 K의 늦은
     도착은 tombstone이 있을 때도 없을 때도 거절. 소유자를 재기동한 뒤의 settle은 "모름".
  5. 두 색이 동시에 플러그인 replay(같은 manifest → 둘 다 skip / 다른 manifest → 한쪽씩 잠금, 레지스트리 온전).
  6. replay 뒤에도 옛 색의 CLI가 연 fd·cwd로 옛 플러그인 트리를 drain 끝까지 읽는다.
  7. 반복 배포(blue→green→blue)와 되돌리기(6단계 중 rollback).
  8. 전환 창(리뷰 acceptance): nginx가 아직 blue인 상태에서 `drain(blue, target=green)` → blue 프록시로 신규 세션 POST,
     기존 쉬는 세션의 새 턴, 관리자 저장을 보낸다 → 신규 세션은 green에서 정확히 한 번 생기고, 쉬는 세션은 green이 넘겨받아
     잇고, 관리자 저장은 green의 `data/`에 한 번 쓰이며, blue가 자기 자신에게 보낸 전달은 0건. 그 뒤 nginx 전환·중단·
     rollback(`DELETE /internal/drain` 후 green 소유 세션은 소유자 전달로 계속)을 반복. `drain(blue, target=blue)`와 boot id가
     맞지 않는 target은 400, target이 죽으면 503 `drain_target_unavailable`(자기 자신으로 열지 않음).
- **deploy 스크립트**: ChatDRAGON `tests/test_deploy_script.py`처럼 docker 호출을 가짜로 바꿔 순서(drain → 전환)·gate·
  되돌리기·timeout을 고정.
- **실환경 리허설**(배포 컨테이너): 긴 도구 턴 + 카드 대기 + 백그라운드 턴을 걸어 둔 채 `deploy.sh` — 끊김 없음, 카드 답이
  닿음, 교체 뒤 같은 대화가 이어짐.

## 9. 정할 것

1. `DRAIN_TIMEOUT_SECONDS` 기본값(제안 3600)과 넘겼을 때의 동작(멈춤 vs 강제).
2. 배포 호스트에서 nginx를 gateway compose 안에 둘지, 이미 있는 사내 리버스 프록시에 upstream 전환을 맡길지(사내 프록시면
   reload가 graceful한지, `/internal/`을 막을 수 있는지 확인).
3. §5.4-3 옛 트리 수명: grace를 env로 늘릴지, drain 중인 instance가 있는 동안 sweep을 멈출지.
4. uid 모드 공유 레지스트리를 이 작업 뒤에 이어서 할지.
