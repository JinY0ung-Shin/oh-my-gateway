# uid 격리(#241 PR-C) 컨테이너 검증 체크리스트

`CLAUDE_ISOLATION_MODE=uid`의 **특권 경로(실제 교차 uid 전환 + entrypoint 브로커 기동)는 무특권
테스트로는 검증되지 않는다.** 코드는 fail-closed로 가려져 있다(브로커가 안 뜨거나 세션 uid가 CLI를
못 띄우면 게이트웨이가 uid 모드 기동을 거부). **프로덕션에서 켜기 전에 배포 컨테이너에서 아래를
반드시 확인한다.**

> **로컬 컨테이너 실측 (2026-10-14, d86a886 이미지, Docker 29.1.3, 커널 6.18, Landlock ABI 7, 기본
> seccomp, root 시작, `cap_add` 없음).**
> - **통과**:
>   - 브로커는 uid 19999·gid 1000이고, cap(Inh/Prm/Eff/Amb)이 정확히 SETUID|SETGID이며
>     NoNewPrivs 1, securebits 0x2f다. CapBnd는 Docker 기본값 그대로다(§5).
>   - 소켓은 `0660`, 디렉터리는 `0750 19999:app`이다. 게이트웨이는 CapEff 0이다.
>   - supervisor `Uid: 19999 20000 19999 20000`에 cap 0, CLI와 Bash는 `(U,U,U)`에 cap 0이다.
>   - 종료(§4)와 기동 거부(§2의 음성 시험)는 문서대로다.
> - **실패: 출하 이미지에서는 uid 모드 턴이 하나도 돌지 않았다.**
>   - 워크스페이스가 app 소유 0755라 세션 uid가 `<ws>/.cli-tmp`를 못 만들고 CLI가 exit 1로 끝났다.
>   - 워크스페이스를 열어 줘도 `/home/app`(0700) 아래 설정·스킬·플러그인을 못 읽었고 transcript가
>     남지 않았다. passwd 항목이 없어 `whoami`가 실패했다.
>   - 그런데도 기동은 통과해 요청마다 503이었다. 이제는 기동 프로브(§2)가 이 상태를 기동 오류로 낸다.
> - 세션 안에서 하는 §3 시험은 세션이 뜨지 않아 아직 돌리지 못했다.

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
      프로비저닝돼 있다(userland은 숫자 uid에 passwd 레코드를 요구). **출하 이미지에는 없다**(`app`만
      만든다). 없으면 세션의 `whoami`가 실패하고 Bash 호출마다 `.bash_profile` EACCES 줄이 찍힌다.
      세 uid 대역(app 1000·브로커·사용자)은 서로 겹치지 않는다. 브로커 gid는 기본값이 APP_GID다
      (소켓 그룹이라 의도된 것이다). 브로커는 uid와 gid 모두에서 0·APP_UID·APP_GID·자기 uid/gid를
      세션에 주지 않는다.
- [ ] 커널 Landlock **ABI ≥ 3**(≥ 6.2; ABI 2는 truncate를 못 막아 기동 거부). `scripts/isolation_probe.py`의 `=> Landlock usable (ABI>=3)` 확인.
      프로브 스크립트는 이미지에 들어 있지 않으니(`scripts/`를 복사하지 않는다) `docker cp`로 넣어
      실행한다. ABI ≥ 6이면 시그널·추상 유닉스 소켓 scoping도 켜진다.
- [ ] 브로커 소켓 경로가 절대 경로이고, 그 **부모 경로가 모두 root 소유·그룹/기타 쓰기 불가**(sticky
      예외)인 실제 디렉터리다. 소켓 디렉터리(`/run/gateway-isolation`)는 entrypoint가 새로 만들거나
      이미 브로커 소유일 때만 재사용한다(`/tmp` 같은 공유 디렉터리 밑이면 브로커를 띄우지 않는다).
      쓰기 가능한 마운트(tmpfs 등)여야 한다.
- [ ] **공유 자산 경로를 세션 uid가 통과할 수 있다.** uid 모드에서 세션 uid가 통과하지 못하는 규칙
      경로는 조용히 건너뛰어진다(접근 불가). `/home/app`이 0700이면 `~/.claude`의 skills·plugins·
      CLAUDE.md, `session-env/<id>`, `plugins/data`가 모두 안 보인다 — 공유 트리를 세션 uid가 읽을 수
      있게 프로비저닝한다(출하 이미지는 0700이다 — `useradd -m`). 기동 프로브(§2)가 설정 디렉터리
      도달 여부를 검사한다.
- [ ] **워크스페이스를 세션 uid가 쓸 수 있다.** 출하 이미지에서는 app 소유 0755라 쓸 수 없고, 그래서
      모든 턴이 실패한다. 이를 해결할 워크스페이스 소유권 작업은 아직 없다(§5).

## 2. 기동 확인

- [ ] `CLAUDE_ISOLATION_MODE=uid`로 기동 시 게이트웨이 로그에 `isolation mode: uid`가 찍히고
      **기동에 성공**한다. 즉 `check_config`의 fail-closed를 통과한다: 브로커 도달 가능, 시크릿 존재,
      uid 레지스트리 정상, 그리고 **기동 프로브 통과**다.
- [ ] **기동 프로브**: 브로커가 응답하면 게이트웨이가 세션처럼 한 번 띄워 본다.
      - 워크스페이스 매니저로 새 익명 워크스페이스를 만들고, 런처와 브로커를 거쳐 ephemeral 세션
        uid로 실제 CLI `--version`을 실행한다. 네트워크와 모델 호출은 없다.
      - 그 uid가 설정 디렉터리에 닿는지, `projects/<name>`을 만들 수 있는지를 mode bit로 확인한다.
        POSIX ACL은 보지 않는다.
      - 실패하면 uid 모드는 "사용 불가"가 된다. 기동 오류로 그 이유를 내거나,
        `CLAUDE_ISOLATION_FALLBACK=landlock`일 때 이유를 담은 경고와 함께 landlock으로 내려간다.
      - **출하 이미지의 기대 결과는 거부다**(컨테이너 exit 3). 다시 빌드한 이미지에서 확인했고, 프로브 비용은
        약 30 ms였다. 다음 두 가지가 이유로 찍힌다:
        - `exited 118`(워크스페이스에 `.cli-tmp`를 못 만듦);
        - 설정 디렉터리 도달 불가(`/home/app is mode 0700`).

        `projects/` 쓰기 불가는 설정 디렉터리에 닿을 때만 따로 보고된다.
      - 시작 시 `verify()`도 uid 모드에서는 같은 방식의 새 워크스페이스에서 돈다. 런처나 브로커가
        거부하면 npm 설치 안내 대신 그 이유를 로그에 남긴다.
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
- [ ] 일부러 불가능하게 만들면 기동이 **거부**되거나, `CLAUDE_ISOLATION_FALLBACK=landlock`일 때만
      경고와 함께 landlock으로 내려간다. 시험할 조건:
      - 브로커 소켓 지우기;
      - ABI<2 커널;
      - 손상된 `isolation_uids.json`;
      - 대역을 좁혀 기존 사용자 uid가 대역 밖이 된 레지스트리;
      - 기동 프로브 실패.

      레지스트리 문제는 fallback이 있어도 거부된다. 보류된 uid는 익명 런의 ephemeral 풀에서도 빠진다.
      실측 결과: 소켓이 `/tmp` 아래면 exit 3으로 거부했고, 같은 조건에 `FALLBACK=landlock`을 주면 경고
      후 landlock으로 돌았다. 손상된 레지스트리는 fallback이 있어도 거부했다.

## 3. 격리 수용 (세션에서)

출하 이미지에서는 세션이 뜨지 않아(§1·§2) 이 절을 돌릴 수 없다. 워크스페이스 소유권과 프로비저닝(§5)이
들어온 뒤에 확인한다.

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
- [ ] 런처를 SIGTERM 없이 바로 SIGKILL하는 경로(`agent_messages` wedge)에서는 SIGTERM을 무시하는
      CLI가 최대 5초 더 살 수 있다. stateless 정리가 그 사이 워크스페이스를 지울 수 있다(알려진 창으로,
      브로커 대행 정리로 닫는다).
      실측 결과(워크스페이스를 연 상태):
      - 세션 `DELETE`: `nohup sleep 300 &`까지 0.88초 안에 모두 정리됐다.
      - 유휴 세션에서 런처 SIGKILL: 0.90초.
      - 작업 중 세션에서 런처 SIGKILL: 자식은 0.05초, CLI와 supervisor는 0.75초.
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
런의 ephemeral uid lease(워크스페이스가 사라진 뒤에만 재사용, 다 차면 거절; 브로커가 다른 워크스페이스에서 살아 있는
uid를 거부), 최소 Landlock ABI 3, 세션이 바꿀 수 있는 런처의 기동 거부, 격리 모드의 `--mcp-config`·시스템 프롬프트
파일화(§6.5, `--json-schema`는 남음).

아직 미완:

- 워크스페이스 uid 소유권·정리 대행(브로커가 U로 전환한 단명 자식으로 삭제/`/files` 읽기). 이것이
  들어와야 런처 직접 SIGKILL 뒤 최대 5초 창도 닫힌다.
- 사용자별 config dir(`CLAUDE_CONFIG_DIR`): uid 모드에서 `session-env/<id>`·`plugins/data`·자기
  projects 디렉터리를 세션 uid가 쓰려면 필요하다(지금은 app 소유).
- 컨테이너 프로비저닝:
  - 브로커 uid와 사용자 대역의 passwd/group 항목;
  - 세션 uid가 통과할 수 있는 공유 트리(`/home/app`);
  - `scripts/isolation_probe.py`를 이미지에 넣기.
- 브로커 cap 추가 잠금: `set*id` 대상을 사용자 uid 대역으로 묶는 **seccomp 인자 필터**,
  bounding set 최소화(CAP_SETPCAP 필요).
- 비root 시작(compose `user:`) 경로의 브로커 file-cap 기동.
- uid별 `USER`/`LOGNAME` 설정(설계 §3 V).
- codex/opencode 백엔드는 런처를 안 타므로 uid 격리 밖이다(§2). `BACKENDS` 확인.

위가 모두 확인되기 전에는 `CLAUDE_ISOLATION_MODE=landlock`(무특권, F1만 닫힘)이 안전한 기본이다.
