# 샌드박스 컨테이너 스파이크 결과 (#248)

> **상태: 스파이크(검증용 프로토타입).** 게이트웨이 코드에는 연결하지 않았다. 사용자·세션별 gVisor
> 컨테이너 안에서 실제 SDK와 번들 CLI가 끝까지 도는지 확인하고, #248 리뷰가 요구한 항목을 실측한 결과다.
> 설계 배경은 #248 본문과 댓글, 컨테이너 단위 논의는 #180을 본다.

## 구성

```
SDK (게이트웨이 프로세스)
 └─ cli_path = docker/sandbox/sandbox_launcher.py     ← 무특권 스텁, fd 0/1/2를 넘기고 종료 상태를 미러링
      └─ unix socket ─▶ docker/sandbox/sandbox_spawner.py   ← Docker 소켓을 가진 유일한 프로세스(사이드카)
            ├─ unit=user    : 사용자별 장수 컨테이너 + docker exec -i (CLI 1개당 exec 1개)
            └─ unit=session : CLI 1개당 docker run -i --rm 컨테이너 1개 (#180의 단위)
                 컨테이너 템플릿(서버 고정): --runtime runsc, --cap-drop ALL, no-new-privileges,
                 --read-only + tmpfs, --memory/--cpus/--pids-limit, uid 1000,
                 그 사용자의 workspace·config dir만 rw 마운트(게이트웨이와 같은 절대 경로), CLI 바이너리 ro,
                 샌드박스마다 전용 internal 네트워크 + 게이트웨이만 그 네트워크에 붙음
```

- 스텁↔spawner 프로토콜은 uid 브로커(`isolation_broker.py`)와 같은 모양이다. `=I` 길이 + JSON 헤더에 fd 3개를 `SCM_RIGHTS`로 싣고, 이후 `=Ii` 레코드로 시그널 중계와 EXITED/SIGNALED/REFUSED를 주고받는다.
- CLI로 가는 env는 이름 allowlist로 거른다(`ENV_ALLOW_*`). 값은 `docker exec -e NAME` 방식이라 docker 클라이언트 argv에 실리지 않는다.
- 같은 절대 경로로 마운트하므로 argv·settings 안의 경로를 번역할 필요가 없다.
- 재현 방법: `GATEWAY_SKIP_DOTENV=1 uv run python -u scripts/sandbox_spike.py`. Docker 데몬과 `runsc` 런타임 등록이 필요하다. 테스트 스위트에는 포함하지 않았다.

측정 환경은 개발용 클라우드 VM이다(커널 6.18, 4 vCPU, 15 GiB, KVM 없음, runsc release-20260714.0, 기본 플랫폼 systrap). 모델은 `tests/fixtures/fake_anthropic_api.py`(고정 응답)로 대체했다. **운영 서버 수치가 아니다.**

## 결과 요약

| 항목 | 결과 |
|---|---|
| SDK ↔ 샌드박스 CLI (initialize, Bash 도구, result) | 4개 조합(user/session × runsc/runc) 모두 통과 |
| SDK 훅·`can_use_tool` | 게이트웨이 프로세스에서 그대로 실행됨 (PreToolUse `['Bash']`, can_use_tool `['Bash']`) |
| CLI 종료 코드 미러링 | 정상 종료 0. 스텁 SIGKILL은 SDK에 -9로 보임 |
| stdio MCP | 샌드박스 안에서 기동(`status: connected`, uid 1000, host `sandbox`) |
| 다른 사용자 파일 | bob 워크스페이스 경로가 alice 샌드박스에 존재하지 않음. 반대 방향도 같음 |
| 게이트웨이 env 비밀 | 디코이 `USAGE_LOG_DB_URL` 미전달(0). **`ANTHROPIC_AUTH_TOKEN`은 전달됨(1)**: 의도적으로 남겨 둔 #247 선행 조건 |
| `/app`, `docker.sock` | 존재하지 않음 |
| PID 1 | `docker-init` (게이트웨이 아님). `/proc`에 보이는 프로세스 6–7개 |
| 인터넷 / 메타데이터 IP / DB | `Network is unreachable`, DB는 이름 해석도 실패 |
| 샌드박스 ↔ 샌드박스 TCP | **공유 internal 네트워크: OPEN.** 샌드박스별 네트워크: `Network is unreachable` |
| 같은 사용자 동시 세션 | S1을 도구 실행 중 취소해도 S2는 정상 완료(`second-ok`). user/session 단위 모두 |
| 정리(teardown) | SDK disconnect 뒤 남은 프로세스 0. 스텁 SIGKILL 뒤에도 0. session 단위는 컨테이너도 제거됨 |
| fail closed | 등록되지 않은 런타임이면 spawner가 모든 spawn을 거부하고 스텁은 exit 70. runc로 폴백하지 않음 |

## 발견한 문제와 해법

1. **gVisor 아래서는 Docker 내장 DNS가 동작하지 않는다.** user-defined 네트워크에서 같은 이미지가 runc로는 `gateway`를 해석하고 runsc로는 `Temporary failure in name resolution`이다. 내장 DNS(127.0.0.11)는 컨테이너 netns 안의 iptables 리다이렉트인데, gVisor 자체 netstack은 그 규칙을 보지 않는다.
   - 해법: spawner가 게이트웨이 주소를 `--add-host gateway:<IP>`로 고정한다(`SANDBOX_ADD_HOSTS`, 샌드박스별 네트워크에서는 자동). 샌드박스가 이름으로 찾을 대상은 게이트웨이 하나뿐이라 이것으로 충분하다.
2. **공유 internal 네트워크에서는 샌드박스끼리 통신된다.** alice 컨테이너가 bob 컨테이너의 열린 포트에 붙었다. `com.docker.network.bridge.enable_icc=false`는 샌드박스끼리뿐 아니라 게이트웨이와의 통신까지 막아서(둘 다 timeout) 해법이 아니다.
   - 해법(스파이크 구현): **샌드박스마다 전용 `--internal` 네트워크**를 만들고 게이트웨이 컨테이너만 각 네트워크에 붙인다(`SANDBOX_GATEWAY_CONTAINER`). 결과는 sibling `unreachable`, 게이트웨이 경유 API 호출은 정상이었다.
   - 운영 주의: Docker 기본 `default-address-pools`로는 브리지 네트워크를 수십 개밖에 만들 수 없다. 100명 규모라면 `daemon.json`에 작은 서브넷(예: `/28`) 풀을 지정해야 한다.
   - 대안: 공유 네트워크 + 호스트 `DOCKER-USER` iptables 규칙으로 "샌드박스→게이트웨이만 허용"을 거는 방법도 있다. 호스트 방화벽 관리가 필요하다.
3. **샌드박스는 게이트웨이 포트 전체에 닿는다.** 리뷰 지적대로 `/admin`과 `/v1`이 같은 포트면 네트워크로는 경로를 가를 수 없다. 샌드박스용 리스너(API 업스트림 프록시, MCP relay만 노출)를 별도 포트로 분리하거나 경로·토큰 검사가 필요하다. 이번 스파이크 범위 밖이다.
4. **user 단위의 teardown은 env 태그 기반(`OMG_EXEC_ID`)이라 스파이크 수준이다.** `docker exec`는 컨테이너 안 프로세스의 핸들을 주지 않는다. 그래서 env 태그로 트리를 찾아 kill한다. env를 지운 프로세스는 빠져나갈 수 있다. session 단위는 컨테이너를 통째로 지우므로 이 문제가 없다.

## 성능 (runc 대비 A/B, 같은 호스트·이미지·bind 마운트)

**세션 지연** (s; warm은 3회 측정)

| | user/runsc | user/runc | session/runsc | session/runc |
|---|---|---|---|---|
| cold connect (컨테이너 생성 포함) | 1.28 | 0.79 | 1.08 | 0.68 |
| cold 첫 턴 (Bash 1회) | 1.49 | 0.64 | 1.46 | 0.62 |
| warm connect | 0.56–0.58 | 0.43–0.45 | 1.01–1.11 | 0.65–0.85 |
| warm 첫 턴 | 0.47–0.55 | 0.24–0.27 | 0.56–0.62 | 0.24–0.26 |
| `docker run --rm true` | 0.26–0.34 | 0.20–0.24 | | |

- SDK disconnect는 모든 조합에서 약 5.3–5.6 s다. 도구 실행 중 끊으면 SDK가 stdin을 닫고 기다린 뒤 terminate하는 시간이다. 샌드박스 없는 기존 경로와는 비교하지 않았다.
- session 단위는 새 SDK client마다 컨테이너 생성 비용 약 0.5 s(runsc)를 더 낸다. `/v1/responses`는 client를 세션 동안 유지하므로 세션 시작 때 한 번이지만, `/v1/agents/messages`는 요청마다 낸다.

**워크로드** (s, 3회 중앙값, 워크스페이스 bind 마운트 위)

| | runc | runsc | 배율 |
|---|---|---|---|
| 작은 파일 2000개 쓰기 | 0.034 | 0.966 | ×28 |
| 2000개 읽기 | 0.023 | 0.143 | ×6 |
| stat walk | 0.007 | 0.038 | ×5 |
| `grep -r` | 0.009 | 0.090 | ×10 |
| 프로세스 200개 생성 | 0.223 | 1.101 | ×5 |
| 순수 CPU 루프 | 0.509 | 0.530 | ×1.04 |
| 2000개 삭제 | 0.016 | 0.049 | ×3 |

리뷰 예상대로 CPU 계산은 거의 같고, 파일 메타데이터와 프로세스 생성이 무겁다. 절대 시간은 짧지만 `npm install`·`git`처럼 작은 파일이 많은 작업은 체감될 수 있다. 실제 운영 마운트와 실제 패키지 설치로 다시 재야 한다. 이번 이미지에는 git/node가 없어서 측정하지 못했다.

**메모리** (idle CLI 세션 20개, user 단위)

| | 컨테이너당 (`docker stats`) | 호스트 사용량 증가 (20개) | 100개 환산 |
|---|---|---|---|
| runc | ~98 MiB | 2404 MiB | ~12 GiB |
| runsc | ~121 MiB | 2873 MiB | ~14 GiB |

CLI가 끝난 뒤 빈 user 컨테이너는 runsc 28 MiB, runc 1.7 MiB다. idle 회수 정책의 근거가 된다.

## 리뷰 release gate 대비 현황

| gate | 이번 스파이크 |
|---|---|
| 1. identity — `user`는 인증된 principal에서만 | **미검증.** spawner는 스텁이 준 `OMG_SANDBOX_USER`를 믿는다. #249와 묶어야 한다. Alice가 `body.user=Bob`을 보내는 공격은 게이트웨이 계층 문제다 |
| 1. secret — `ANTHROPIC_AUTH_TOKEN` 등 미전달 | 일반 env 비밀은 allowlist로 차단 확인. 업스트림 토큰은 아직 전달된다(#247: 게이트웨이 토큰 프록시 필요) |
| 2. network | 인터넷·메타데이터·DB·sibling 차단 확인(샌드박스별 네트워크). 게이트웨이 포트의 경로 분리는 미구현 |
| 3. 단위·동시성 | user/session 두 단위 모두 같은 사용자 동시 세션 독립 확인. 게이트웨이·spawner crash 뒤 orphan reconcile과 idle 회수는 미구현 |
| 4. manager 권한 | 게이트웨이에 소켓 없음, 컨테이너 템플릿은 서버 고정(호출자가 고르는 건 user뿐). spawner 자체가 고권한 TCB라는 점은 그대로다 |
| 4. 디스크 쿼터 | 미검증. cgroup CPU·메모리·PID만 걸었다 |
| 5. fail closed | 확인. 런타임 미등록이면 전부 거부, runc 폴백 없음 |

## 다음 단계 제안

1. **단위 결정.** 스파이크로는 둘 다 동작한다. 판단 근거는 아래와 같다.
   - session 단위: teardown이 확실하고(#180 ABA 문제 없음), warm connect가 약 0.5 s 더 느리다.
   - user 단위: 빠르지만 세션별 프로세스 추적을 env 태그보다 강한 방식(per-exec cgroup 등)으로 바꿔야 한다.
   - #180의 결론(session 단위)을 따르고, 지연이 문제 되면 warm pool로 보완하는 쪽을 권한다.
2. **선행 조건:**
   - #249(인증된 principal → user)
   - #247(업스트림 토큰을 샌드박스 밖으로 빼는 게이트웨이 프록시)
   - 샌드박스 전용 게이트웨이 리스너
3. **게이트웨이 통합:** `CLAUDE_ISOLATION_MODE=container`로 `_get_cli_path`가 이 스텁을 가리키게 한다. 이때 바꿔야 할 것이 셋 있다.
   - `options.env`에 사용자 신원을 넣는다(현재는 env, 운영에서는 uid 모드처럼 서명된 티켓으로).
   - MCP relay를 샌드박스 네트워크 주소로 열도록 바꾼다(#248 MCP 댓글).
   - 볼륨을 호스트 경로 기준으로 매핑한다.
4. **운영 서버 재측정:** 실제 커널·스토리지에서 이 스크립트를 다시 돌린다. git/node가 든 이미지로 `npm install`·`git` 벤치를 추가한다.
