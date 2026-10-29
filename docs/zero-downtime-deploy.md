# 무중단 배포 설계 (blue/green)

> 상태: **설계 초안**, 구현 전. 기준 2026-10-29 `main`. 구현은 이 문서가 리뷰된 뒤 아래 [단계](#7-구현-단계)대로
> 나눠 올린다. 참고: ChatDRAGON UI의 같은 방식(#710, `scripts/deploy.sh`·`/health/drain`·nginx `active.conf`).

## 1. 목표와 비목표

**목표** — 새 이미지로 바꾸는 동안 사용자가 아무것도 알아채지 못한다.

1. 새 요청은 새 컨테이너로 간다.
2. 옛 컨테이너에서 이미 돌고 있는 턴(스트리밍·백그라운드)은 끝까지 돈다 — 도구를 오래 쓰는 턴은 수십 분이다.
3. 옛 컨테이너에서 시작한 대화(`previous_response_id`)를 새 컨테이너가 이어 간다.
4. 옛 컨테이너가 띄운 AskUserQuestion·계획 승인 카드에 사용자가 답하면, 답이 그 카드를 기다리는 프로세스에 닿는다.

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
| `GET /v1/sessions/{id}/pending-events` | 세션 outbox, 프로세스 메모리 | 다른 색은 404 → ChatDRAGON은 빈 응답으로 처리하고 다음 턴이 복구(이미 처리). 교체 순간의 이벤트 일부를 놓칠 수 있다 |
| 같은 세션에 CLI 둘 | 옛 색은 쉬는 세션의 CLI를 TTL(`SESSION_MAX_AGE_MINUTES` 60) 동안 쥔다. 프로세스 간 잠금 없음 | 새 색이 같은 세션을 resume하면 transcript 하나에 CLI 둘이 쓴다 |
| 기동 시 플러그인 설치 | CMD가 매 기동 `install_plugins.py` — 고정된 `<dest>.new`로 받아 `rmtree(dest)` + `os.replace`, `claude plugin …`이 `installed_plugins.json`을 고친다. 파일 잠금 없음 | 새 색 기동이 옛 색의 살아 있는 세션 밑에서 플러그인 폴더를 바꾼다. 둘이 동시에 기동하면 `.new`가 충돌 |
| 기동 시 `_tmp_` 정리 | `SESSION_MAX_AGE_MINUTES*60`보다 mtime이 오랜 익명 워크스페이스를 지운다 | 새 색이 옛 색의 **살아 있는** 익명 워크스페이스를 지울 수 있다 |
| 관리자 쿠키 | `_COOKIE_SECRET = secrets.token_bytes(32)` (`src/admin_auth.py`) 프로세스마다 새로 | 교체하면 관리자가 로그아웃된다 |
| runtime-config 오버라이드 | 메모리에만(`src/runtime_config.py`) | 새 색은 env 기본값으로 뜬다. ChatDRAGON이 주기 reconcile로 다시 건다(`gateway_runtime_pin`, `GATEWAY_RUNTIME_RESTORE`) — 그 주기 동안 기본값 |
| `data/` 파일(프롬프트·MCP·플러그인 manifest 등) | tmp + `os.replace`로 원자적이지만 잠금은 프로세스 내 `RLock` | 두 색이 동시에 고치면 마지막 쓰기가 이긴다. 관리자 쓰기를 활성 색 한 곳으로만 보내면 문제없다 |
| 포트 | `ports: "${PORT}:${PORT}"` 하나 | 두 번째 컨테이너는 같은 호스트 포트를 못 잡는다 → nginx가 호스트 포트를 잡고 색은 내부 네트워크만 |
| MCP progress relay | CLI 자식이 **자기 컨테이너의 loopback**으로 다이얼(`note_origin`) | 그대로 된다. `MCP_RELAY_BASE_URL`을 nginx·공용 주소로 두면 깨진다 — 비워 둔다 |

## 3. 구조

```
ChatDRAGON BFF ── :8000 ──> gateway-proxy (nginx) ──> gateway-blue | gateway-green (:8000, 내부망)
                                                      └ 같은 claude_home · ./data · ./workspaces · ./plugins
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
- 각 색은 `GATEWAY_INSTANCE=<blue|green>`을 받아 응답 헤더 `x-gateway-instance`와 `/health`에 싣는다 — deploy
  스크립트가 proxy를 거친 응답이 정말 새 색에서 왔는지 확인한다(200만으로는 옛 색일 수 있다).
- 마이그레이션: 지금의 단일 `gateway` 컨테이너에서 처음 옮길 때는 `scripts/deploy.sh migrate` 한 번 — blue를 먼저
  healthy로 띄우고 옛 컨테이너를 내린 뒤 proxy가 포트를 잡는다(이 한 번은 포트를 넘기는 몇 초의 끊김이 있다).

## 4. 배포 순서 (`scripts/deploy.sh`)

1. **빌드** — 새 이미지를 만든다. uid 모드(`CLAUDE_ISOLATION_MODE=uid`)면 여기서 거부하고 재시작 배포를 안내한다.
2. **유휴 색 기동** — 활성 색이 blue면 green을 같은 볼륨으로 띄운다. 플러그인 설치는 [§5.4](#54-기동-시-공유-파일)대로
   잠금 + "바뀐 게 없으면 건너뜀".
3. **준비 확인** — green 안에서 `/health/ready`가 200, `/health`의 instance가 `green`.
4. **전환** — `active.conf`를 green으로 바꾸고 reload. proxy를 거친 `/health`가 `green`을 답할 때까지 확인.
5. **drain 시작** — blue에 `POST /internal/drain`(컨테이너 안 loopback 전용 + `ADMIN_API_KEY`). blue는:
   - `/health/ready` → 503, 새 세션을 만드는 요청은 retryable 503(`x-should-retry: true` — 전환 직후 nginx가
     아직 blue로 보낸 요청이 다시 와서 green으로 간다).
   - **쉬는 세션을 바로 놓는다**: 진행 중인 턴도 카드도 없는 세션의 CLI를 disconnect하고(transcript는 남는다)
     세션 잠금을 푼다 — green이 그 세션을 resume해도 CLI가 둘이 되지 않는다([§5.2](#52-한-세션에-cli-하나)).
   - 진행 중인 턴·카드를 기다리는 세션·백그라운드 턴은 그대로 둔다. 끝나면 그 세션도 놓는다.
6. **drain 대기** — `GET /health/drain`(loopback 전용)을 주기적으로 읽어 아래가 모두 0이 될 때까지 기다린다.
   `DRAIN_TIMEOUT_SECONDS`(기본 3600)을 넘기면 남은 것을 보여 주고 멈춘다 — 강제로 내릴지는 운영자가 정한다
   (`--force`).
7. **정지** — blue에 SIGTERM. 이때는 열린 연결이 없으므로 lifespan 종료(세션 disconnect)가 곧바로 돈다.
   `stop_grace_period`는 lifespan 종료만 덮으면 된다(지금의 35s 유지).

되돌리기: 6단계 전이면 `active.conf`를 blue로 되돌리고 blue의 drain을 풀면(`DELETE /internal/drain`) 끝이다.
7단계 뒤라면 같은 스크립트로 옛 이미지를 다시 배포한다.

### `/health/drain` 계약

```json
{
  "instance": "blue",
  "draining": true,
  "active_turns": 0,
  "background_turns": 0,
  "pending_interactions": 0,
  "open_requests": 0,
  "live_sessions": 0
}
```

- `active_turns` — 스트리밍 중인 `/v1/responses`(세션 `active_response_*`), `/v1/agents/messages` 실행 포함.
- `background_turns` — `_BACKGROUND_RUNS`에 남은 것.
- `pending_interactions` — 답을 기다리는 카드(`pending_tool_call`이 있고 `input_event`가 안 풀린 세션). 300s 뒤엔
  저절로 만료되므로 drain이 무한히 걸리지 않는다.
- `open_requests` — 위에 속하지 않는 열린 HTTP 요청(업로드·파일 API·관리자 작업). ChatDRAGON `drain.py`의
  `InflightMiddleware`와 같은 방식(probe 경로는 세지 않는다).
- `live_sessions` — 아직 CLI를 쥔 세션 수. 정보용(0이 아니어도 위가 0이면 내릴 수 있다 — 놓기만 하면 된다).

## 5. 세부 설계

### 5.1 대화 이어가기

이미 되는 경로를 **유지**한다: 두 색이 같은 볼륨을 쓰고, `user`가 있으면 rehydrate + `resume`. 바꿀 것은 없고,
deploy 테스트에 "blue에서 시작한 대화를 green이 잇는다"를 넣어 고정한다. 익명 대화는 지금처럼 새로 시작된다
(ChatDRAGON은 늘 `user`를 보낸다).

### 5.2 한 세션에 CLI 하나

drain이 쉬는 세션을 바로 놓는 것으로 대부분 막히지만, **진행 중인** 세션에 같은 대화의 다음 요청이 green으로 가는
경우(ChatDRAGON은 같은 대화의 턴을 줄 세우지만 API 클라이언트는 그러지 않을 수 있다)를 위해 프로세스 간 잠금을
더한다:

- 세션이 CLI를 쥐는 동안 `claude_home/gateway-session-locks/<session_id>.lock`에 `flock(LOCK_EX)`를 잡는다(두
  컨테이너가 같은 볼륨 = 같은 커널이므로 컨테이너를 넘어 동작한다). 놓을 때 푼다. 프로세스가 죽으면 커널이 푼다.
- 다른 프로세스가 그 세션을 resume하려다 `LOCK_NB`에 걸리면 409 `session_busy_elsewhere` + `x-should-retry: true`
  (+ `Retry-After`) — 끝나면 다시 잇는다. ChatDRAGON에는 "앞 턴을 마무리하는 중" 안내로 매핑한다(별도 PR).
- 같은 프로세스 안의 기존 세션 잠금(`asyncio.Lock`)은 그대로 두고, 이 파일 잠금은 그 바깥 경계로만 쓴다.

### 5.3 카드·pending-events가 그 프로세스로 가게

카드는 그 카드를 기다리는 프로세스 메모리에만 있다. 두 안이 있다.

| | A. 응답 헤더로 색 고정 (권장) | B. gateway가 서로에게 넘기기 |
|---|---|---|
| 방식 | 응답에 `x-gateway-instance: blue`. 클라이언트가 같은 세션의 다음 요청(카드 답, `pending-events`)에 그 값을 다시 보내면 nginx `map $http_x_gateway_instance`가 그 색으로 보낸다. 그 색이 없으면 활성 색으로 | 세션 잠금 파일에 소유 색의 내부 주소를 적고, green이 남의 세션 요청을 받으면 그 색으로 내부 reverse-proxy(스트리밍 포함) |
| 클라이언트 변경 | ChatDRAGON이 대화별로 마지막 instance를 기억해 다시 보낸다(BFF 한 곳) | 없음 |
| 복잡도 | nginx `map` 몇 줄 + 헤더 한 개 | 스트리밍 프록시·인증 전달·실패 처리 — 큰 표면 |
| 헤더 없는 클라이언트 | 지금과 같다(카드 만료 → 다시 묻기) | 모두 된다 |

**권장: A.** gateway 쪽은 헤더 한 개, nginx `map`, 그리고 "그 색이 drain 중이어도 자기 세션 요청은 받는다"뿐이다.
B는 클라이언트를 안 바꿔도 되지만 gateway 안에 두 번째 프록시를 만드는 일이라 이 목적에 비해 크다. 헤더가 없는
클라이언트(공개 API 직접 사용자)는 교체 순간 300초 안의 카드가 만료될 수 있다 — 지금의 재시작보다 나쁘지 않다.

`x-gateway-instance`는 **라우팅 힌트일 뿐 권한이 아니다** — 어느 색으로 가든 세션 소유자 검사(`user`)는 그대로다.
값은 `blue`/`green`만 받고 나머지는 활성 색으로 보낸다.

### 5.4 기동 시 공유 파일

- **플러그인 설치**: `install_plugins.py`를 `claude_home/.install-plugins.lock`의 `flock`으로 감싸고, staging
  디렉터리를 `<dest>.new-<pid>`처럼 유일하게 한다. 또 manifest·marketplace 원본이 지난 설치와 같으면(`data/`에
  지난 설치의 digest를 남긴다) **아무것도 하지 않는다** — 그래야 같은 manifest로 green을 띄울 때 blue의 살아 있는
  세션 밑에서 플러그인 폴더가 바뀌지 않는다. manifest가 **바뀐** 배포는 바뀐 플러그인만 갱신하고, 옛 색의 세션은
  drain 동안 옛 파일을 볼 수 있어야 하므로 `rmtree(dest)` 대신 새 디렉터리로 바꾼 뒤 옛 것은 drain이 끝난 뒤 지운다
  (구현 단계에서 `claude plugin update`가 옛 cache 버전을 지우는지 먼저 확인).
- **`plugin_autorefresh`**: 프로세스 내 `asyncio.Lock`만 있다 — 같은 파일 잠금을 쓰고, drain 중인 색은 돌리지 않는다.
- **`_tmp_` 정리**: 익명 워크스페이스 이름에 instance를 넣거나(`_tmp_<instance>_…`), 기동 정리가 다른 instance의
  것을 건드리지 않게 한다. 다른 색이 내려간 뒤 남은 것은 다음 기동 정리가 치운다.
- **entrypoint의 chown**: 같은 uid로 다시 소유권을 맞추는 것이라 두 색이 함께 돌아도 안전하다(시간만 든다).

### 5.5 관리자 상태

- **쿠키 비밀값**: `data/admin_cookie_secret`(0600)에 한 번 만들어 두 색이 같이 읽는다 — 교체해도 로그아웃되지 않는다.
  `ADMIN_COOKIE_SECRET` env가 있으면 그것을 쓴다.
- **runtime-config**: 오버라이드를 `data/runtime_config.json`(tmp + `os.replace`)에 남기고 기동 때 읽는다 — green이
  기본값으로 뜨는 틈이 없어진다. ChatDRAGON의 reconcile은 그대로 두어도 된다(gateway가 이미 오버라이드를 들고
  있으면 건드리지 않는다).
- **관리자 쓰기**: proxy는 활성 색으로만 보내므로 `data/`를 쓰는 곳은 한 곳이다. drain 중인 색은 관리자 쓰기를 503으로
  거절한다(옛 화면의 늦은 저장이 옛 색에 쓰이지 않게).

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
| 1 | drain(`/internal/drain`, `/health/drain`, 쉬는 세션 즉시 놓기, ready 503), `GATEWAY_INSTANCE` + `x-gateway-instance`, `--timeout-graceful-shutdown`, 쿠키 비밀값·runtime-config 영속화 | 없음 |
| 2 | 세션 파일 잠금(§5.2) + 409 `session_busy_elsewhere`, 기동 공유 파일 정리(§5.4) | 409가 새로 생김(retryable) |
| 3 | compose(blue/green/proxy, 고정 볼륨 이름), nginx 설정, `scripts/deploy.sh`(+ `migrate`), 운영 문서 | 운영 절차 바뀜 |
| 4 (ChatDRAGON) | 대화별 `x-gateway-instance` 기억·전달, 409 `session_busy_elsewhere` 안내 | 카드가 교체 중에도 산다 |

PR 1·2는 단일 컨테이너 배포에서도 해가 없다(drain을 안 부르면 지금과 같다).

## 8. 검증 계획

- 단위: drain 상태 전이, `/health/drain` 카운트(턴·카드·백그라운드·열린 요청), 쉬는 세션 즉시 놓기, 파일 잠금
  409, 플러그인 설치 skip/잠금, `_tmp_` 정리가 다른 instance를 건드리지 않음, 쿠키 비밀값 공유.
- 통합(`tests/fixtures/fake_anthropic_api.py` + 번들 CLI, 두 프로세스를 같은 HOME으로): blue에서 시작한 대화를 green이
  resume, 진행 중인 세션을 green이 잡으려 하면 409, drain 중 blue의 긴 턴이 끝까지 스트리밍.
- deploy 스크립트: ChatDRAGON `tests/test_deploy_script.py`처럼 docker 호출을 가짜로 바꿔 순서·되돌리기·timeout을 고정.
- 실환경 리허설(배포 컨테이너): 긴 도구 턴 + 카드 대기 + 백그라운드 턴을 걸어 둔 채 `deploy.sh` — 끊김 없음, 카드 답이
  닿음, 교체 뒤 같은 대화가 이어짐.

## 9. 정할 것

1. §5.3 A(헤더, ChatDRAGON 변경)로 갈지, 헤더 없는 클라이언트까지 위해 B를 할지.
2. `DRAIN_TIMEOUT_SECONDS` 기본값(제안 3600)과 넘겼을 때의 동작(멈춤 vs 강제).
3. 배포 호스트에서 nginx를 gateway compose 안에 둘지, 이미 있는 사내 리버스 프록시에 upstream 전환을 맡길지.
4. uid 모드 공유 레지스트리를 이 작업 뒤에 이어서 할지.
