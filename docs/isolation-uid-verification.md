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
>
> **로컬 컨테이너 실측 (2026-10-15, 브랜치 `feat/uid-ownership-config-dir`의 이미지 `omg-uid:d71aaec`,
> `docker-compose.uid.yml`과 같은 env·볼륨, 가짜 Anthropic API, host 네트워크).** 워크스페이스 ACL·사용자별
> config dir·브로커 정리/transcript op·프로비저닝이 들어간 뒤 다시 돌렸다. **전부 통과**:
> - 기동: `isolation mode: uid`, 기동 프로브(세션 uid로 실제 spawn·정리) 통과, 세션 uid로 `verify()` 성공.
> - named 사용자 턴이 `claude-u<uid>`(passwd 항목, `USER`/`LOGNAME`)로 돌고, `CLAUDE_CONFIG_DIR=<base>/<uid>`, umask
>   077, 워크스페이스 쓰기·`git status`(app 소유 루트, `safe.directory`)·공유 skill 읽기가 된다.
> - landlock 시절의 transcript·memory가 첫 spawn에 사용자별 config dir로 옮겨진다. 같은 세션 이어가기, 재기동 뒤
>   만료 세션 재개(브로커 transcript op)가 된다.
> - 익명·stateless 실행 뒤 워크스페이스·사용자별 config dir·`/dev/shm` 잔여물이 지워지고 uid가 재사용된다(3개
>   ephemeral uid로 8회 연속).
> - 플러그인이 S에 설치되고 SessionStart 훅이 세션 uid로 `P/plugins/data/<id>`에 쓴다.
> - 다른 사용자의 워크스페이스·사용자별 config dir·`/app/data`·`/proc/1/environ`을 못 읽는다.
> - supervisor `Uid: 19999 U 19999 U`·cap 0이고 세션의 `kill -9`이 EPERM, CLI `(U,U,U)`·보조 그룹 없음·cap
>   0·NNP 1, 세션 삭제 때 `nohup` 작업까지 정리된다.
> - landlock 모드로 되돌리면 app uid가 세션 uid가 만든 파일을 ACL로 계속 읽고 쓴다.
> - 음성 시험: config base 미설정, 공유 config dir이 `/home/app`(0700) 안 → 이유를 남기고 기동 거부.
> - 리뷰 반영 뒤 이미지(`omg-uid:ff0266c`)로 위 전부를 다시 돌려 통과했고, SysV IPC 시험을 더했다: ephemeral
>   uid 하나로 돌린 익명 런이 SysV shm·세마포어를 남기면, 같은 uid를 받은 다음 런에는 0개가 보인다.

무특권 부분(티켓 서명·검증 패리티·uid 매핑·ephemeral uid 임대와 그 수명·SCM_RIGHTS fd 전달·supervisor
트리·Landlock·`O_NOFOLLOW` 규칙 열기·cwd 일치 검사·종료 상태 미러링·teardown·하위 프로세스 sweep·고아 킬러·
상주 브로커 내구성·env 전파/strip·위조 티켓 거부·entrypoint 소유권 보존)은 same-uid에서 이미 테스트된다
(브로커는 테스트 전용 `--test-same-uid` 플래그로): `tests/test_uid_isolation.py`,
`tests/test_isolation_broker.py`, `tests/test_landlock_launcher_hardening.py`,
`tests/test_entrypoint_broker.py`, `tests/test_landlock_confine.py::TestUidMode`,
`tests/test_uid_lease_lifetime.py`. 이 플래그는 argv로만 받고 entrypoint는 절대 넘기지 않는다 — 프로덕션
브로커는 자기 uid/gid로의 전환을 거부한다.

## 1. 전제 (컨테이너 프로비저닝)

- [ ] 컨테이너가 **root로 시작**한다(entrypoint가 drop 전에 브로커를 fork). compose `user:`로
      비root 시작이면 브로커 바이너리 file-cap + 시작부터 NNP=0 경로가 필요하다(미구현).
- [ ] **전용 브로커 uid/gid**(`CLAUDE_ISOLATION_BROKER_UID`, 기본 19999)와 **사용자 uid 대역**
      (`CLAUDE_ISOLATION_UID_MIN..MAX`, 기본 20000~29999)의 `/etc/passwd`·`/etc/group` 항목(userland은 숫자
      uid에 passwd 레코드를 요구한다). root 시작이면 entrypoint가 uid 모드에서 빠진 것만 더한다
      (`claude-broker`, `claude-u<uid>`; 대역이 65536개를 넘으면 경고만 하고 넘어가므로 그때는 NSS로 직접).
      `docker exec <c> getent passwd 20000`으로 확인.
      세 uid 대역(app 1000·브로커·사용자)은 서로 겹치지 않는다. 브로커 gid는 기본값이 APP_GID다
      (소켓 그룹이라 의도된 것이다). 브로커는 uid와 gid 모두에서 0·APP_UID·APP_GID·자기 uid/gid를
      세션에 주지 않고, 게이트웨이의 uid 할당도 같은 id를 건너뛴다(세션 gid = uid).
- [ ] 커널 Landlock **ABI ≥ 3**(≥ 6.2; ABI 2는 truncate를 못 막아 기동 거부). `scripts/isolation_probe.py`의 `=> Landlock usable (ABI>=3)` 확인.
      프로브 스크립트는 이미지의 `/usr/local/bin/isolation_probe.py`다. ABI ≥ 6이면 시그널·추상 유닉스
      소켓 scoping도 켜진다.
- [ ] 브로커 소켓 경로가 절대 경로이고, 그 **부모 경로가 모두 root 소유·그룹/기타 쓰기 불가**(sticky
      예외)인 실제 디렉터리다. 소켓 디렉터리(`/run/gateway-isolation`)는 entrypoint가 새로 만들거나
      이미 브로커 소유일 때만 재사용한다(`/tmp` 같은 공유 디렉터리 밑이면 브로커를 띄우지 않는다).
      쓰기 가능한 마운트(tmpfs 등)여야 한다.
- [ ] **`docker-compose.uid.yml`의 레이아웃**(설계 `docs/isolation-uid-ownership-design.md` §4·§11):
      공유 config dir `CLAUDE_CONFIG_DIR`(S)는 `/home/app`(0700) 밖·시스템 루트(`/opt`·`/usr`…) 밖이고
      `CLAUDE_PLUGIN_CLONE_ROOT`는 그 안이다. 세션 uid마다의 config dir 기준 `CLAUDE_ISOLATION_CONFIG_BASE`는
      S·`$HOME`·워크스페이스 base·`/app` 밖이다. 둘 다 영속 볼륨 위에 있고, 거기까지와 `USER_WORKSPACES_DIR`까지의
      상위 디렉터리가 모두 o+x다(entrypoint가 경고한다). 게이트웨이는 S의 진입점(S·asset 디렉터리·settings·
      레지스트리 파일)을 기동 때 다른 uid가 읽을 수 있게 넓힌다. 파일 자산은 링크가 하나뿐인 일반 파일이어야
      한다(하드 링크면 빠진다).
- [ ] **POSIX ACL 지원**: 워크스페이스 base와 config base의 파일시스템이 `system.posix_acl_*`를 받는다
      (ext4·xfs·overlay·tmpfs는 보통 된다). 게이트웨이가 기동 때 검사해, 지원하지 않으면 이유와 함께 기동을
      거부한다.
- [ ] 워크스페이스 base 아래에는 symlink를 두지 않는다(사용자 워크스페이스를 다른 볼륨으로 잇는 링크 포함;
      base 자체의 링크는 괜찮다) — confined spawn은 워크스페이스가 정확히 `realpath(base)/<rel>`로 풀리기를
      요구하고, 아니면 그 요청이 HTTP 500 `workspace_refused`다.
- [ ] **실제 CLI가 자기 전용 디렉터리에 있다**(`CLAUDE_CLI_PATH` 또는 SDK 번들). 런처·브로커가 그 디렉터리
      전체를 모든 세션에 ro+exec로 열므로, 그것이 `/`·`$HOME`·config dir·워크스페이스 base·`/app`·임시
      디렉터리·`/run` 같은 시스템 경로이거나 그것을 품거나 워크스페이스 base 안이면 기동이 거부된다
      (`$HOME` 안은 괜찮다 — 네이티브 설치기의 `~/.local/share/claude/versions/<버전>`).

## 2. 기동 확인

- [ ] `CLAUDE_ISOLATION_MODE=uid`로 기동 시 게이트웨이 로그에 `isolation mode: uid`가 찍히고
      **기동에 성공**한다. 즉 `check_config`의 fail-closed를 통과한다: 브로커 도달 가능, 시크릿 존재,
      uid 레지스트리 정상, 그리고 **기동 프로브 통과**다.
- [ ] **기동 프로브**: 브로커가 응답하면 게이트웨이가 세션처럼 한 번 띄워 본다.
      - 워크스페이스 매니저로 새 익명 워크스페이스를 만들고, 런처와 브로커를 거쳐 ephemeral 세션
        uid로 실제 CLI `--version`을 실행한다. 네트워크와 모델 호출은 없다.
      - 그 uid의 사용자별 config dir와 워크스페이스 ACL을 준비하고 프로브 세션 id로 띄운 뒤, 세션 uid가
        `.cli-tmp`·`projects/<name>`·`session-env/<id>`·`tasks/<id>`를 실제로 만들었는지 소유자로 확인한다(커널이
        ACL을 평가한 결과). 공유 트리 S는 ACL이 없으니 mode bit로 본다. 끝으로 그 워크스페이스를 브로커
        cleanup op로 지우고, 그 uid의 잔여물이 남지 않아야 통과다.
      - 실패하면 uid 모드는 "사용 불가"가 된다. 기동 오류로 그 이유를 내거나,
        `CLAUDE_ISOLATION_FALLBACK=landlock`일 때 이유를 담은 경고와 함께 landlock으로 내려간다.
      - `docker-compose.uid.yml` 레이아웃의 기대 결과는 통과다(2026-10-15 실측). 레이아웃이 틀리면 이유가
        찍힌다: config base 미설정(`needs CLAUDE_ISOLATION_CONFIG_BASE`, 맨 앞), 공유 config dir 도달 불가
        (`/home/app is mode 0700`), `exited 118`(`.cli-tmp`를 못 만듦), 세션 uid가 만들지 못한 디렉터리 등.
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
- [ ] 게이트웨이 본체 `CapEff`는 **0**이고 non-dumpable이다. 티켓 시크릿은 환경변수가 아니라 pipe로 온다:
      root로 본 `/proc/<gateway>/environ`(exec 시점 환경)에 `GATEWAY_ISOLATION_TICKET_SECRET`이 없고
      `GATEWAY_ISOLATION_TICKET_SECRET_FD=<fd>:<dev>:<ino>`만 있으며, 게이트웨이 로그에 "ticket secret pipe … was
      empty" 경고가 없다. CMD 단계(`sh -c`, `install_plugins.py`)의 environ에도 시크릿이 없다.
- [ ] 일부러 불가능하게 만들면 기동이 **거부**되거나, `CLAUDE_ISOLATION_FALLBACK=landlock`일 때만
      경고와 함께 landlock으로 내려간다. 시험할 조건:
      - 브로커 소켓 지우기;
      - ABI<3 커널(5.19–6.1의 ABI 2 포함);
      - 손상된 `isolation_uids.json`(아주 깊게 중첩된 JSON도 traceback 없이 같은 손상 오류다);
      - 대역을 좁혀 기존 사용자 uid가 대역 밖이 된 레지스트리;
      - `CLAUDE_ISOLATION_UID_MAX=4294967295`(상한 초과), `APP_GID`를 대역 안 uid로 둔 레지스트리 항목;
      - 같은 레지스트리를 쓰는 두 번째 게이트웨이 프로세스(`--workers 2`, `<registry>.lock`을 잡은 다른 프로세스);
      - 기동 프로브 실패;
      - 너무 넓은 실제 CLI 디렉터리(예: `CLAUDE_CLI_PATH=$HOME/claude`, §1).

      레지스트리 문제, 실제 CLI 디렉터리, ABI<3(landlock도 못 돈다), 세션이 바꿀 수 있는 런처는 fallback이
      있어도 거부된다. 보류된 uid는 익명 런의 ephemeral
      풀에서도 빠진다.
      실측 결과: 소켓이 `/tmp` 아래면 exit 3으로 거부했고, 같은 조건에 `FALLBACK=landlock`을 주면 경고
      후 landlock으로 돌았다. 손상된 레지스트리는 fallback이 있어도 거부했다.

## 3. 격리 수용 (세션에서)

2026-10-15 로컬 컨테이너에서 아래 F1·DAC·environ·supervisor·자격 증명·정상 작업 항목을 확인했다(맨 위 실측
상자). 배포 컨테이너에서 다시 돌린다.

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
- [ ] **바꿔치기한 경로는 허용되지 않는다**(비격리 writer — root `docker exec` — 로 심는다):
      - 워크스페이스 자리의 symlink(`<base>/<user>/claude -> ~/.claude`): 그 사용자의 요청이 HTTP 500
        `workspace_refused`이고 응답에는 경로가 없으며, 게이트웨이 로그에 `Refusing a confined spawn in …`이 남는다.
      - 공유 자산의 symlink·하드 링크(`~/.claude/agents -> /app`, 다른 사용자의 transcript에 하드 링크한
        `~/.claude/CLAUDE.md`): 그 항목이 세션에서 빠지고 게이트웨이 로그에 `Not granting read-only …`가 남는다.
        게이트웨이 검사 뒤에 바꿔치기한 것은 브로커 자식이 `landlock: not granting '<path>': …`(CLI stderr)로 뺀다.
- [ ] 정상 작업은 유지: Bash(결과에 `~/.profile`·`~/.bashrc`의 `Permission denied` 줄이 없다 — CLI가 bash를
      고르면 `claude-session-bash`가 맨 앞 `-c -l`을 `--norc -c`로 바꾼다), `git init`·`commit`·`status`(세션 전역
      설정 `<워크스페이스>/.cli-tmp/gitconfig`; 세션의 `git config --global` 값이 다음 spawn에도 남는다),
      `python3`(multiprocessing — `/dev/shm`), `uv run`·`uvx`·`npx -y`(세션별 캐시
      `<워크스페이스>/.cli-tmp/{uv-cache,npm-cache}`, uv의 관리형 Python·도구 환경은 `.cli-tmp/{uv-python,uv-tools}`),
      `uv run --with …`으로 뜨는 플러그인 MCP 서버의 도구 사용(#246 — 시작에 실패하면 게이트웨이 로그에
      `MCP server '<이름>' failed to start` 경고), 스킬 인식, 사용자 설정·rules 로드
      (`~/.claude/settings.json`·`~/.claude.json`·`~/.claude/rules` ro), 플러그인 훅 실행(`plugins/data/<id>`는
      게이트웨이가 미리 만들어 두고 읽기 전용 — 그 안에 쓰는 훅만 실패), SessionStart 훅과 TaskCreate
      (`session-env/<session id>`·`tasks/<session id>`만 rw).
- [ ] 슬래시/스킬 preflight는 그 사용자에게 uid가 있으면 세션 uid로(브로커 경유), 아직 없으면 게이트웨이
      uid로 landlock 형태로 돌고, 어느 쪽이든 워크스페이스 hook이나 `.mcp.json` 서버를 실행하지 않는다. 새
      사용자의 첫 요청이 알 수 없는 `/command`로 400이면 `isolation_uids.json`에 그 사용자가 생기지 않는다.

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
- [ ] supervisor가 비정상 종료하면 상주 브로커(subreaper)가 남은 프로세스를 단명 자식으로 죽인다. 그
      자식은 그 uid를 effective uid로만 쥐고(supervisor와 같은 `(broker, U, broker)`, cap 0), 브로커는 그것을
      최대 2초 기다린다 — 멈추거나 시간을 넘기면 SIGKILL하고 최대 2초 더 기다려 거두며(그래도 남으면 reaper에
      맡긴다), 그 pid들은 치우지 못했다고 로그에 남긴 뒤 다시 시도하지 않는다. 확인: 세션 Bash에서
      `setsid sh -c 'while :; do kill -STOP -1; done' &`와 `setsid sleep 300 &`를 띄우고 root 셸에서
      `kill -9 <supervisor>`. 두 작업이 모두 사라지고, 브로커 로그에
      `orphan killer … stopped`/`overran`이 없고, 다음 턴이 돈다. ABI < 6 커널에서 자격 증명 층을 보여 주는
      시험이다(ABI ≥ 6이면 시그널 scoping이 이미 막는다).
- [ ] **재기동 뒤 소유권**: named 사용자로 한 턴을 돌리고 `docker restart`. `projects/<name>`과 그 안의
      `<sid>.jsonl`이 그대로 `U:U`다(uid 모드의 root 단계는 세션 uid 소유 항목을 chown하지도 걷지도 않는다).
      그 세션을 `previous_response_id`로 이으면 재개된다 — 게이트웨이가 못 여는 0600 transcript는 브로커가
      세션 uid로 읽어 턴 수만 돌려준다(2026-10-15 실측). 브로커가 읽지 못할 때만 재시도로 풀리지 않는 HTTP 500
      `transcript_unreadable`이다. 이어서 대역을 좁혀 그 사용자의 uid가 대역 밖이 되게 하고 다시
      `docker restart`: 그 `projects/<name>`은 여전히 `U:U`이고(root 단계는 `isolation_uids.json`에 기록된 uid도
      건너뛴다), 게이트웨이는 대역을 되돌릴 때까지 기동을 거부한다. `CLAUDE_ISOLATION_FALLBACK=landlock`으로
      내려간 재기동에서는 entrypoint가 폴백을 몰라 U 소유가 그대로 남고, app uid로 도는 landlock 세션은 그
      `projects/<name>`에 쓰지 못한다(알려진 한계).
- [ ] 상주 브로커를 SIGKILL해도 실행 중 세션은 각자의 supervisor 아래에서 계속되고(죽지 않는다),
      이후 **새 spawn만** fail-closed다(REFUSED/연결 불가 → 런처 exit 70).
- [ ] 워크스페이스 산출물·transcript·`session-env/<id>`·`tasks/<id>`가 **세션 uid 소유**여도 게이트웨이의
      cleanup·`/files`·세션 재개가 동작한다: 평범하게 만든 것은 워크스페이스 ACL(`d:u:<app>`)로 게이트웨이가
      직접, 0600/0700으로 잠긴 것은 브로커가 세션 uid로 지우고(cleanup op) 읽는다(transcript op). 익명 런은 그
      워크스페이스·사용자별 config dir·`/dev/shm`이 지워진 뒤에야 uid 임대가 풀린다(2026-10-15 실측). `/files`는
      세션 uid가 0600/0700으로 잠근 파일에서 500이다(자기 것만 잠근 경우).

## 5. 알려진 미완 (PR-C 후속)

이미 처리된 것(#241 리뷰 1–4): uid 전환 후 cap 전량 드롭, 런처-사망 teardown(교차 uid kill 없이
supervisor가 수행), detached 하위 프로세스 sweep, 소켓 권한 0660/그룹·소켓 디렉터리 규칙, 티켓 nonce를
상주 부모에서 검증(만료 시 정리)·peer uid 고정·uid/gid 대역 강제(0·app·broker 제외, same-uid 지름길
없음), 상주 브로커의 잘못된 헤더·정체 피어·fork 실패 내성, 종료 코드/시그널 분리 전송, 기동 준비 대기,
티켓 시크릿을 게이트웨이 환경변수에서 제거, 레지스트리 fail-closed, 공유 캐시 쓰기 제거, 익명/stateless
런의 ephemeral uid 임대(워크스페이스 정리 때 반납 — 모든 경로가 CLI를 멈춘 뒤에 정리하고, 클라이언트 시작 중의
teardown은 그 클라이언트를 멈출 때까지 미룬다; 지우지 못한 워크스페이스는 uid를 계속 쥐고 다 차면 거절; 브로커가 다른
워크스페이스에서 살아 있는 uid와 고아가 쥔 uid를 거부), 기동 프로브, 고아 킬러의 `(broker, U, broker)` 자격 증명과 대기
상한, uid 모드 entrypoint의 세션 uid(레지스트리에 기록된 uid 포함) 소유 항목 보존, uid 없는 사용자의 preflight가
uid를 할당하지 않음, 워크스페이스·공유 항목의 symlink/하드 링크 거부(게이트웨이 필터, 런처·브로커의 `O_NOFOLLOW`
열기와 cwd 일치 검사), 실제 CLI 디렉터리 검사, 최소 Landlock ABI 3, 세션이 바꿀 수 있는 런처의 기동 거부, 격리
모드의 `--mcp-config`·시스템 프롬프트 파일화(§6.5, `--json-schema`는 남음).

아직 미완:

(2026-10-15에 들어온 것: 워크스페이스 ACL 소유권(설계 α)과 브로커 cleanup·transcript op, 사용자별 config dir,
passwd/group·공유 트리·config base 프로비저닝과 compose override, 이미지의 프로브 스크립트, uid별
`USER`/`LOGNAME`, umask 077과 익명 런의 `/dev/shm` 정리, 실제 동작으로 판정하는 기동 프로브 —
`docs/isolation-uid-ownership-design.md` §11.)

- 런처 직접 SIGKILL 뒤의 최대 5초 창: 익명 워크스페이스 정리는 이제 브로커가 그 uid의 supervisor가 끝날 때까지
  기다렸다가(park) 하므로 워크스페이스·임대는 안전하지만, SIGTERM을 무시하는 CLI는 그동안 더 산다.
- ephemeral uid 임대의 남은 틈: `/v1/agents/messages`·admin try-run은 호출자가 떠났을 때만 남은 CLI를 kill하므로
  정상 경로의 disconnect가 시간을 넘기면 그 CLI가 살아 있는 채로 임대가 끝나고, 요청이 클라이언트를 만들기 전에
  도는 슬래시 preflight 중에 삭제가 들어오면 그 짧은 카탈로그 CLI 아래에서 임대가 끝나며, kill된 CLI의 프로세스는
  브로커가 약 5초 안에 거둔다. 새 런은 안 쓴 uid부터, 그다음 가장 오래전에 반납된 uid를 받으므로 막 반납된 uid는
  이런 낙오자보다 훨씬 오래 쉰다. 그래도 낙오자가 남아 있으면 브로커가 그 uid의 새 spawn을 거부한다(살아 있는
  supervisor가 다른 워크스페이스로 쥐었거나 고아가 쥔 uid, `_check_uid_free`) — 공유 대신 그 런이 실패한다.
  익명 런이 끝나면 그 uid의 워크스페이스·사용자별 config dir·`/dev/shm`을 브로커가 지운 뒤에야 임대가 풀리고,
  임대된 uid의 첫 spawn 전에 남은 것(죽은 게이트웨이 프로세스의 잔여물)을 한 번 더 확인한다.
- 브로커의 uid 검사는 uid 하나에 워크스페이스 하나를 가정한다. 정리를 포기한 고아가 있는 named 사용자는 그
  고아가 끝날 때까지 spawn하지 못하고, 두 워크스페이스에 동시에 살아 있는 named 사용자(레거시 `base/<user>`
  재개와 `base/<user>/claude` 세션)는 두 번째가 거부된다.
- uid 모드의 CLI 자식은 룰셋과 세션 셸 래퍼를 브로커 자신의 디렉터리(entrypoint 옆)에서 가져온다. 기동 검사는
  런처 쪽 파일만 보므로, 이미지처럼 넷을 한 디렉터리(`/usr/local/bin`)에 둔다.
- named 사용자 uid 회수: 지금은 영구라 안정 대역이 차면 새 named 사용자가 HTTP 500
  `uid_band_exhausted`다(재시도로 풀리지 않는다; 90%를 넘으면 경고).
- 브로커 cap 추가 잠금: `set*id` 대상을 사용자 uid 대역으로 묶는 **seccomp 인자 필터**,
  bounding set 최소화(CAP_SETPCAP 필요).
- 비root 시작(compose `user:`) 경로의 브로커 file-cap 기동.
- SysV IPC는 세션별 namespace가 아니다: 살아 있는 세션이 world 권한으로 만든 SysV 객체는 다른 살아 있는
  세션이 열 수 있다(Landlock 비중재). 런이 끝나면 정리 op가 그 uid의 객체를 지운다. 세션별 IPC namespace가 후속이다.
  다음 런의 시작을 막는 잔여물은 그 uid가 **만든** 객체뿐이다: 다른 세션이 `IPC_SET`으로 넘겨준 객체나, 아직
  attach된 채 파괴 표시(`SHM_DEST`)된 세그먼트는 세지 않는다(그러지 않으면 다른 세션이 lease를 영구히 붙잡는다).
  `SHM_DEST` 세그먼트는 id로 아직 attach할 수 있어서, 이전 테넌트가 다른 uid에게 접근을 준 세그먼트는 다음
  테넌트도 읽을 수 있다(그 uid는 이미 갖고 있는 내용이다; 설계 §11.7).
- 보존 sweep(uid마다 하루 한 번, 공유 `settings.json`의 `cleanupPeriodDays`)은 그 uid의 세션이 살아 있으면
  브로커가 거부하므로 매시간 다시 시도한다. 마지막 실행 시각은 P의 `.oh-my-gateway-retention`에 남아 재시작에도
  이어진다. 늘 세션이 살아 있는 사용자는 정리되지 않는다(기한 뒤 이틀째부터 하루 한 번 경고).
- 소유자 마커(`P/.oh-my-gateway-owner`)가 생기기 전 빌드로 만든 P(5b5df9b 이전의 수용 컨테이너 볼륨 등)는 그
  사용자의 첫 spawn이 지운다. 그런 볼륨은 버리고 다시 돌린다(설계 §11.7).
- 보존 sweep과 같은 사용자의 동시 spawn 사이의 좁은 경쟁:
  비고 오래된 `projects/<name>`을 sweep이 지우는 순간 그 사용자의 spawn이 그 디렉터리를 쓰면 그 세션의
  transcript가 남지 않는다(감수).
- `/files`는 세션 uid가 0600/0700으로 잠근 파일에서 500이다(자기 것만 잠근 경우).
- codex/opencode 백엔드는 런처를 안 타므로 uid 격리 밖이다(§2). `BACKENDS` 확인.

배포 컨테이너에서 §1–§4를 다시 확인하기 전에는 `CLAUDE_ISOLATION_MODE=landlock`(무특권, F1만 닫힘)이 안전한
기본이다.
