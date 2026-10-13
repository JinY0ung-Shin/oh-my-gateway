# uid 격리(#241 PR-C) 컨테이너 검증 체크리스트

`CLAUDE_ISOLATION_MODE=uid`의 **특권 경로(실제 교차 uid 전환 + entrypoint 브로커 기동)는
dev 워크스테이션에서 검증되지 않는다**(root 없음, 컨테이너 없음). 코드는 설계대로 작성됐고
fail-closed로 가려져 있지만(브로커가 안 뜨면 게이트웨이가 uid 모드 기동을 거부), **프로덕션에서
켜기 전에 배포 컨테이너에서 아래를 반드시 확인한다.**

무특권 부분(티켓 서명·uid 매핑·SCM_RIGHTS fd 전달·fork 트리·Landlock·종료 미러링·env 전파/strip·
위조 티켓 거부)은 same-uid로 이미 테스트된다: `tests/test_uid_isolation.py`,
`tests/test_isolation_broker.py`, `tests/test_landlock_confine.py::TestUidMode`.

## 1. 전제 (컨테이너 프로비저닝)

- [ ] 컨테이너가 **root로 시작**한다(entrypoint가 drop 전에 브로커를 fork). compose `user:`로
      비root 시작이면 브로커 바이너리 file-cap + 시작부터 NNP=0 경로가 필요하다(미구현).
- [ ] **전용 브로커 uid/gid**(`CLAUDE_ISOLATION_BROKER_UID`, 기본 21000)와 **사용자 uid 대역**
      (`CLAUDE_ISOLATION_UID_MIN..MAX`, 기본 20000~29999)이 이미지의 `/etc/passwd`·`/etc/group`에
      프로비저닝돼 있다(userland은 숫자 uid에 passwd 레코드를 요구). 세 대역(app 1000·브로커·사용자)은
      서로 겹치지 않는다.
- [ ] 커널 Landlock **ABI ≥ 2**(≥ 5.19). `scripts/isolation_probe.py`의 `=> Landlock usable` 확인.
- [ ] 브로커 소켓 디렉터리(`/run/gateway-isolation`)가 쓰기 가능한 마운트다(tmpfs 등).

## 2. 기동 확인

- [ ] `CLAUDE_ISOLATION_MODE=uid`로 기동 시 게이트웨이 로그에 `isolation mode: uid`가 찍히고
      **기동에 성공**한다(= `check_config`의 fail-closed를 통과 = 브로커 도달 가능 + 시크릿 존재).
- [ ] 브로커 프로세스가 **전용 uid로** 떠 있고 `CapEff`가 **SETUID/SETGID만**이다
      (`grep Cap /proc/<broker>/status`; root가 아니어야 한다). securebits가 잠겨(NOROOT) 있어
      브로커가 `setresuid(0)`해도 root를 되찾지 못한다.
- [ ] 브로커 소켓이 `0660`(그룹=게이트웨이 gid)이고 디렉터리가 `0750`(브로커 소유)이라,
      게이트웨이 uid 런처가 **그룹 권한으로 연결**된다(세계-쓰기 아님). 세션 uid는 Landlock
      룰셋에 소켓 경로가 없어 애초에 못 닿는다.
- [ ] 게이트웨이 본체 `CapEff`는 **0**이고 non-dumpable이다(#232).
- [ ] 일부러 불가능하게 만들면(브로커 소켓 지우기 / ABI<2 커널) 기동이 **거부**되거나
      `CLAUDE_ISOLATION_FALLBACK=landlock`일 때만 경고와 함께 landlock으로 내려간다.

## 3. 격리 수용 (세션에서)

- [ ] **F1**: "바깥 파일을 읽는 스크립트를 써서 실행" → 룰셋 밖(다른 사용자 워크스페이스·`/app`·
      `/app/data`)은 **못 읽는다**. (`tests/test_cli_read_boundary.py`의 시나리오를 uid 모드로.)
- [ ] 두 다른 사용자의 세션이 **서로의 워크스페이스 파일을 못 읽는다**(uid DAC): 세션 A가 만든 0600
      파일을 세션 B가 못 연다.
- [ ] 세션이 **게이트웨이의 `/proc/<pid>/environ`을 못 읽는다**(F2; uid 분리 + non-dumpable).
- [ ] 세션이 **다른 세션 CLI의 환경/메모리에 시그널·ptrace를 못 보낸다**(uid 경계; ABI≥6이면
      `LANDLOCK_SCOPE_SIGNAL`).
- [ ] 세션 CLI와 그 Bash/스크립트의 `CapEff`가 **0**이다(브로커가 uid 전환 후 cap을 모두 버림
      — #2; `grep Cap /proc/<session-cli>/status`). 그래서 세션이 `setresuid`로 다른 uid·root로
      못 바꾼다.
- [ ] 정상 작업은 유지: Bash, `python3`, `uv`(캐시 rw), `npx`(`~/.npm` rw), 스킬 인식,
      사용자 설정 로드(`~/.claude/settings.json`·`~/.claude.json` ro), 플러그인 훅(`plugins/data` 쓰기).

## 4. 수명주기

- [ ] 런처(스텁)를 SIGKILL하면(= SDK `close()`) 핸들러가 런처 연결 EOF를 보고 watcher를
      내리고, watcher가 CLI **프로세스 그룹**(CLI는 `setsid` 리더)을 쓸어 **거둔 뒤** 정리한다
      (좀비·고아 없음 — #7; same-uid로 `test_isolation_broker.py::test_launcher_death_tears_down_cli`).
- [ ] 브로커를 SIGKILL하면 실행 중 CLI 트리가 함께 죽고(PDEATHSIG), 이후 spawn은 fail-closed다.
- [ ] 워크스페이스 산출물이 **세션 uid 소유**가 되어도 게이트웨이의 cleanup/`/files`가 동작한다
      (브로커 대행 또는 group 권한 — **이 부분은 아직 미구현**, PR-C 후속에서 완성).

## 5. 알려진 미완 (PR-C 후속)

이미 처리된 것(#241 리뷰): uid 전환 후 cap 전량 드롭(#2), 런처-사망 teardown(#7), 소켓 권한
0660/그룹(#10), 티켓 nonce를 상주 부모에서 검증해 재사용 거부(#13)·peer uid 고정·uid/gid 대역
강제(0·app·broker 제외), 익명/stateless 런의 ephemeral uid 재활용(대역 소진 방지).

아직 미완:

- 워크스페이스 uid 소유권·정리 대행(브로커가 U로 전환한 단명 자식으로 삭제/`/files` 읽기).
- 브로커 cap 추가 잠금: `set*id` 대상을 사용자 uid 대역으로 묶는 **seccomp 인자 필터**,
  bounding set 최소화(CAP_SETPCAP 필요).
- 비root 시작(compose `user:`) 경로의 브로커 file-cap 기동.
- codex/opencode 백엔드는 런처를 안 타므로 uid 격리 밖이다(§2). `BACKENDS` 확인.

위가 모두 확인되기 전에는 `CLAUDE_ISOLATION_MODE=landlock`(무특권, F1만 닫힘)이 안전한 기본이다.
