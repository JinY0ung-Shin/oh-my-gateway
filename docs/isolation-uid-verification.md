# uid 격리(#241 PR-C) 컨테이너 검증 체크리스트

`CLAUDE_ISOLATION_MODE=uid`의 **특권 경로(실제 교차 uid 전환 + entrypoint 브로커 기동)는
dev 워크스테이션에서 검증되지 않는다**(root 없음, 컨테이너 없음). 코드는 설계대로 작성됐고
fail-closed로 가려져 있지만(브로커가 안 뜨면 게이트웨이가 uid 모드 기동을 거부), **프로덕션에서
켜기 전에 배포 컨테이너에서 아래를 반드시 확인한다.**

무특권 부분(티켓 서명·검증 패리티·uid 매핑·SCM_RIGHTS fd 전달·supervisor 트리·Landlock·종료 상태
미러링·teardown·하위 프로세스 sweep·상주 브로커 내구성·env 전파/strip·위조 티켓 거부)은 브로커의
테스트 전용 `--test-same-uid` 플래그로 same-uid에서 이미 테스트된다: `tests/test_uid_isolation.py`,
`tests/test_isolation_broker.py`, `tests/test_landlock_launcher_hardening.py`,
`tests/test_entrypoint_broker.py`, `tests/test_landlock_confine.py::TestUidMode`. 이 플래그는 argv로만
받고 entrypoint는 절대 넘기지 않는다 — 프로덕션 브로커는 자기 uid/gid로의 전환을 거부한다.

## 1. 전제 (컨테이너 프로비저닝)

- [ ] 컨테이너가 **root로 시작**한다(entrypoint가 drop 전에 브로커를 fork). compose `user:`로
      비root 시작이면 브로커 바이너리 file-cap + 시작부터 NNP=0 경로가 필요하다(미구현).
- [ ] **전용 브로커 uid/gid**(`CLAUDE_ISOLATION_BROKER_UID`, 기본 19999)와 **사용자 uid 대역**
      (`CLAUDE_ISOLATION_UID_MIN..MAX`, 기본 20000~29999)이 이미지의 `/etc/passwd`·`/etc/group`에
      프로비저닝돼 있다(userland은 숫자 uid에 passwd 레코드를 요구). 세 대역(app 1000·브로커·사용자)은
      서로 겹치지 않는다. 브로커는 uid와 gid 모두에서 0·APP_UID·APP_GID·자기 uid/gid를 세션에 주지 않는다.
- [ ] 커널 Landlock **ABI ≥ 2**(≥ 5.19). `scripts/isolation_probe.py`의 `=> Landlock usable` 확인.
      ABI ≥ 6이면 시그널·추상 유닉스 소켓 scoping도 켜진다.
- [ ] 브로커 소켓 경로가 절대 경로이고, 그 **부모 경로가 모두 root 소유·그룹/기타 쓰기 불가**(sticky
      예외)인 실제 디렉터리다. 소켓 디렉터리(`/run/gateway-isolation`)는 entrypoint가 새로 만들거나
      이미 브로커 소유일 때만 재사용한다(`/tmp` 같은 공유 디렉터리 밑이면 브로커를 띄우지 않는다).
      쓰기 가능한 마운트(tmpfs 등)여야 한다.
- [ ] **공유 자산 경로를 세션 uid가 통과할 수 있다.** uid 모드에서 세션 uid가 통과하지 못하는 규칙
      경로는 조용히 건너뛰어진다(접근 불가). `/home/app`이 0700이면 `~/.claude`의 skills·plugins·
      CLAUDE.md, `session-env/<id>`, `plugins/data`가 모두 안 보인다 — 공유 트리를 세션 uid가 읽을 수
      있게 프로비저닝한다.

## 2. 기동 확인

- [ ] `CLAUDE_ISOLATION_MODE=uid`로 기동 시 게이트웨이 로그에 `isolation mode: uid`가 찍히고
      **기동에 성공**한다(= `check_config`의 fail-closed를 통과 = 브로커 도달 가능 + 시크릿 존재 +
      uid 레지스트리 정상).
- [ ] entrypoint가 브로커가 연결을 받을 때까지 기다린 뒤 게이트웨이를 exec한다(게이트웨이도 시작 때
      한 번 더 기다린다; 둘 다 `CLAUDE_ISOLATION_BROKER_WAIT_SECONDS`, 기본 10).
- [ ] 브로커 프로세스가 **전용 uid로** 떠 있고 `CapEff`가 **SETUID/SETGID만**이다
      (`grep Cap /proc/<broker>/status`; root가 아니어야 한다). securebits가 잠겨(NOROOT) 있어
      브로커가 `setresuid(0)`해도 root를 되찾지 못한다.
- [ ] 브로커 소켓이 `0660`(그룹=게이트웨이 gid)이고 디렉터리가 `0750`(브로커 소유)이라,
      게이트웨이 uid 런처가 **그룹 권한으로 연결**된다(세계-쓰기 아님). 세션 uid는 Landlock
      룰셋에 소켓 경로가 없어 애초에 못 닿는다.
- [ ] 게이트웨이 본체 `CapEff`는 **0**이고 non-dumpable이며, `/proc/<gateway>/environ`이 아닌
      런타임 환경(`os.environ`)에는 `GATEWAY_ISOLATION_TICKET_SECRET`이 없다(기동 때 메모리로 옮김).
- [ ] 일부러 불가능하게 만들면(브로커 소켓 지우기 / ABI<2 커널 / 손상된 `isolation_uids.json` /
      대역을 좁혀 기존 사용자 uid가 대역 밖이 된 레지스트리) 기동이 **거부**되거나,
      `CLAUDE_ISOLATION_FALLBACK=landlock`일 때만 경고와 함께 landlock으로 내려간다(레지스트리 문제는
      fallback 없이 거부). 보류된 uid는 익명 런의 ephemeral 풀에서도 빠진다.

## 3. 격리 수용 (세션에서)

- [ ] **F1**: "바깥 파일을 읽는 스크립트를 써서 실행" → 룰셋 밖(다른 사용자 워크스페이스·`/app`·
      `/app/data`)은 **못 읽는다**. (`tests/test_cli_read_boundary.py`의 시나리오를 uid 모드로.)
- [ ] 두 다른 사용자의 세션이 **서로의 워크스페이스 파일을 못 읽는다**(uid DAC): 세션 A가 만든 0600
      파일을 세션 B가 못 연다.
- [ ] 세션이 **게이트웨이의 `/proc/<pid>/environ`을 못 읽는다**(F2; uid 분리 + non-dumpable).
- [ ] 세션이 **다른 세션 CLI에 시그널·ptrace를 못 보낸다**(uid 경계; ABI≥6이면
      `LANDLOCK_SCOPE_SIGNAL`).
- [ ] 세션이 **자기 supervisor를 죽이거나 ptrace하지 못한다**: supervisor의 `Uid:`가
      `<broker> <U> <broker> <U>`이고(`grep -E 'Uid|Gid|Groups' /proc/<supervisor>/status`), 세션
      Bash에서 `kill -9 <supervisor>`가 EPERM이다(ABI < 6 커널에서도 — scoping이 아니라 자격 증명으로
      막는다).
- [ ] 세션 CLI와 그 Bash/스크립트의 uid가 `(U,U,U)`, `CapEff`가 **0**이다
      (`grep -E 'Uid|Cap' /proc/<session-cli>/status`). 그래서 세션이 `setresuid`로 다른 uid·root로
      못 바꾼다.
- [ ] 정상 작업은 유지: Bash, `python3`(multiprocessing — `/dev/shm`), `uv run`·`npx -y`(세션별 캐시
      `<워크스페이스>/.cli-tmp/{uv-cache,npm-cache}`), 스킬 인식, 사용자 설정 로드
      (`~/.claude/settings.json`·`~/.claude.json` ro), 플러그인 훅 실행(`plugins/data/<id>`는
      게이트웨이가 미리 만들어 두고 읽기 전용 — 그 안에 쓰는 훅만 실패), SessionStart 훅
      (`session-env/<session id>`만 rw).
- [ ] 슬래시/스킬 preflight가 세션 uid로 돌고(브로커 경유) 워크스페이스 hook이나 `.mcp.json` 서버를
      실행하지 않는다.

## 4. 수명주기

- [ ] 런처(스텁)를 SIGKILL하면(= SDK `close()`) supervisor가 런처 연결 EOF를 보고 CLI **프로세스
      그룹**(CLI는 `setsid` 리더)에 SIGTERM, 첫 SIGTERM 5초 뒤 SIGKILL을 보내고, CLI가 끝나면 남은
      **모든 하위 프로세스**(Bash가 따로 세션을 만든 `cmd &`·`setsid` 작업 포함)를 죽여 거둔 뒤에야
      상태를 보내고 끝난다. 확인: 세션 Bash에서 `sleep 300 &` 후 세션을 닫으면 그 `sleep`이 사라진다
      (same-uid로 `test_isolation_broker.py`의 teardown/sweep 테스트와
      `TestBundledCliThroughBroker`).
- [ ] 런처를 SIGTERM 없이 바로 SIGKILL하는 경로(`agent_messages` wedge)에서는 CLI가 최대 5초 더
      살 수 있다 — stateless 정리가 그 사이 워크스페이스를 지울 수 있다(알려진 창; 브로커 대행 정리로
      닫는다).
- [ ] supervisor가 비정상 종료하면 상주 브로커(subreaper)가 남은 프로세스를 그 uid로 전환한 단명
      자식으로 죽인다.
- [ ] 상주 브로커를 SIGKILL해도 실행 중 세션은 각자의 supervisor 아래에서 계속되고(죽지 않는다),
      이후 **새 spawn만** fail-closed다(REFUSED/연결 불가 → 런처 exit 70).
- [ ] 워크스페이스 산출물이 **세션 uid 소유**가 되어도 게이트웨이의 cleanup/`/files`가 동작한다
      (브로커 대행 또는 group 권한 — **이 부분은 아직 미구현**, PR-C 후속에서 완성).

## 5. 알려진 미완 (PR-C 후속)

이미 처리된 것(#241 리뷰 1·2): uid 전환 후 cap 전량 드롭, 런처-사망 teardown(교차 uid kill 없이
supervisor가 수행), detached 하위 프로세스 sweep, 소켓 권한 0660/그룹·소켓 디렉터리 규칙, 티켓 nonce를
상주 부모에서 검증(만료 시 정리)·peer uid 고정·uid/gid 대역 강제(0·app·broker 제외, same-uid 지름길
없음), 상주 브로커의 잘못된 헤더·정체 피어·fork 실패 내성, 종료 코드/시그널 분리 전송, 기동 준비 대기,
티켓 시크릿을 게이트웨이 환경변수에서 제거, 레지스트리 fail-closed, 공유 캐시 쓰기 제거, 익명/stateless
런의 ephemeral uid 재활용(대역 소진 방지).

아직 미완:

- 워크스페이스 uid 소유권·정리 대행(브로커가 U로 전환한 단명 자식으로 삭제/`/files` 읽기). 이것이
  들어와야 런처 직접 SIGKILL 뒤 최대 5초 창도 닫힌다.
- 사용자별 config dir(`CLAUDE_CONFIG_DIR`): uid 모드에서 `session-env/<id>`·`plugins/data`·자기
  projects 디렉터리를 세션 uid가 쓰려면 필요하다(지금은 app 소유).
- 브로커 cap 추가 잠금: `set*id` 대상을 사용자 uid 대역으로 묶는 **seccomp 인자 필터**,
  bounding set 최소화(CAP_SETPCAP 필요).
- 비root 시작(compose `user:`) 경로의 브로커 file-cap 기동.
- uid별 `USER`/`LOGNAME` 설정(설계 §3 V).
- codex/opencode 백엔드는 런처를 안 타므로 uid 격리 밖이다(§2). `BACKENDS` 확인.

위가 모두 확인되기 전에는 `CLAUDE_ISOLATION_MODE=landlock`(무특권, F1만 닫힘)이 안전한 기본이다.
