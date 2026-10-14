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
      (`CLAUDE_ISOLATION_UID_MIN..MAX`, 기본 20000~29999)이 이미지의 `/etc/passwd`·`/etc/group`에
      프로비저닝돼 있다(userland은 숫자 uid에 passwd 레코드를 요구). **출하 이미지에는 없다**(`app`만
      만든다). 없으면 세션의 `whoami`가 실패한다.
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
      CLAUDE.md, `session-env/<id>`·`tasks/<id>`, `plugins/data`가 모두 안 보인다 — 공유 트리를 세션 uid가
      읽을 수 있게 프로비저닝한다(출하 이미지는 0700이다 — `useradd -m`). 공유 트리를 HOME 밖(예:
      `/opt/claude-shared`)에 두고 `~/.claude`에서 symlink로 이으면 그 대상이 그대로 허용된다(`$HOME`·`/app`·
      임시 디렉터리 안을 가리키는 링크는 경고와 함께 빠진다). 파일 자산(`CLAUDE.md`·`settings*.json`·
      `.claude.json`)은 링크가 하나뿐인 일반 파일이어야 한다(하드 링크면 빠진다). 사용자별 config dir를 위해
      `CLAUDE_CONFIG_DIR`를 `~/.claude` 밖으로 옮기면 기본 clone 루트(`$HOME/.claude/plugin-marketplaces`)는
      `$HOME` 안이라 빠지므로, `CLAUDE_PLUGIN_CLONE_ROOT`를 symlink가 아닌 경로로 설정한다. 기동 프로브(§2)가
      설정 디렉터리 도달 여부를 검사한다.
- [ ] **`~/.claude/session-env`·`~/.claude/tasks`에 세션 uid가 쓸 수 있다.** uid 모드에서는 게이트웨이가
      세션별 `<session id>` 디렉터리를 만들지 않고(게이트웨이 소유 0700은 세션 uid가 못 쓴다) 브로커 자식이
      세션 uid로 만든다. 부모에 못 쓰면 SessionStart 훅과 TaskCreate가 실패한다. 기동 프로브는 이것을 보지
      않는다.
- [ ] **워크스페이스를 세션 uid가 쓸 수 있다.** 출하 이미지에서는 app 소유 0755라 쓸 수 없고, 그래서
      모든 턴이 실패한다. 이를 해결할 워크스페이스 소유권 작업은 아직 없다(§5). 워크스페이스 base 아래에는
      symlink를 두지 않는다(사용자 워크스페이스를 다른 볼륨으로 잇는 링크 포함; base 자체의 링크는 괜찮다) —
      confined spawn은 워크스페이스가 정확히 `realpath(base)/<rel>`로 풀리기를 요구하고, 아니면 그 요청이
      HTTP 500 `workspace_refused`다.
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
      - 그 uid가 설정 디렉터리에 닿는지, `projects/<name>`을 만들 수 있는지를 mode bit로 확인한다.
        POSIX ACL은 보지 않는다.
      - 실패하면 uid 모드는 "사용 불가"가 된다. 기동 오류로 그 이유를 내거나,
        `CLAUDE_ISOLATION_FALLBACK=landlock`일 때 이유를 담은 경고와 함께 landlock으로 내려간다.
      - **출하 이미지의 기대 결과는 거부다**(컨테이너 exit 3). 다시 빌드한 이미지에서 확인했고, 프로브 비용은
        약 30 ms였다. 다음 두 가지가 이유로 찍힌다:
        - `exited 118`(`<워크스페이스>/.cli-tmp`를 세션 uid가 쓸 수 있는 디렉터리로 못 만듦 — 대개
          워크스페이스에 쓸 수 없어서);
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
      - ABI<3 커널(5.19–6.1의 ABI 2 포함);
      - 손상된 `isolation_uids.json`(아주 깊게 중첩된 JSON도 traceback 없이 같은 손상 오류다);
      - 대역을 좁혀 기존 사용자 uid가 대역 밖이 된 레지스트리;
      - 기동 프로브 실패;
      - 너무 넓은 실제 CLI 디렉터리(예: `CLAUDE_CLI_PATH=$HOME/claude`, §1).

      레지스트리 문제, 실제 CLI 디렉터리, ABI<3(landlock도 못 돈다), 세션이 바꿀 수 있는 런처는 fallback이
      있어도 거부된다. 보류된 uid는 익명 런의 ephemeral
      풀에서도 빠진다.
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
- [ ] **바꿔치기한 경로는 허용되지 않는다**(비격리 writer — root `docker exec` — 로 심는다):
      - 워크스페이스 자리의 symlink(`<base>/<user>/claude -> ~/.claude`): 그 사용자의 요청이 HTTP 500
        `workspace_refused`이고 응답에는 경로가 없으며, 게이트웨이 로그에 `Refusing a confined spawn in …`이 남는다.
      - 공유 자산의 symlink·하드 링크(`~/.claude/agents -> /app`, 다른 사용자의 transcript에 하드 링크한
        `~/.claude/CLAUDE.md`): 그 항목이 세션에서 빠지고 게이트웨이 로그에 `Not granting read-only …`가 남는다.
        게이트웨이 검사 뒤에 바꿔치기한 것은 브로커 자식이 `landlock: not granting '<path>': …`(CLI stderr)로 뺀다.
- [ ] 정상 작업은 유지: Bash(결과에 `~/.profile`·`~/.bashrc`의 `Permission denied` 줄이 없다 — CLI가 bash를
      고르면 `claude-session-bash`가 맨 앞 `-c -l`을 `--norc -c`로 바꾼다), `git init`·`commit`·`status`(세션 전역
      설정 `<워크스페이스>/.cli-tmp/gitconfig`; 세션의 `git config --global` 값이 다음 spawn에도 남는다),
      `python3`(multiprocessing — `/dev/shm`), `uv run`·`npx -y`(세션별 캐시
      `<워크스페이스>/.cli-tmp/{uv-cache,npm-cache}`), 스킬 인식, 사용자 설정·rules 로드
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
      그 세션을 `previous_response_id`로 이으면 지금은 404가 아니라 재시도로 풀리지 않는 HTTP 500
      `transcript_unreadable`이다 — 게이트웨이가 0600 transcript를 못 읽는다. 재개 자체는 transcript 대행
      후속(§5)이 들어온 뒤에야 통과한다. 이어서 대역을 좁혀 그 사용자의 uid가 대역 밖이 되게 하고 다시
      `docker restart`: 그 `projects/<name>`은 여전히 `U:U`이고(root 단계는 `isolation_uids.json`에 기록된 uid도
      건너뛴다), 게이트웨이는 대역을 되돌릴 때까지 기동을 거부한다. `CLAUDE_ISOLATION_FALLBACK=landlock`으로
      내려간 재기동에서는 entrypoint가 폴백을 몰라 U 소유가 그대로 남고, app uid로 도는 landlock 세션은 그
      `projects/<name>`에 쓰지 못한다(알려진 한계).
- [ ] 상주 브로커를 SIGKILL해도 실행 중 세션은 각자의 supervisor 아래에서 계속되고(죽지 않는다),
      이후 **새 spawn만** fail-closed다(REFUSED/연결 불가 → 런처 exit 70).
- [ ] 워크스페이스 산출물·transcript·`session-env/<id>`·`tasks/<id>`가 **세션 uid 소유**가 되어도 게이트웨이의
      cleanup·`/files`·세션 재개가 동작한다(브로커 대행 또는 group 권한 — **이 부분은 아직 미구현**, PR-C
      후속에서 완성). 지금은 stateless transcript 정리가 경고만 남기고, 세션 uid가 쓴 파일이 든 세션 디렉터리는
      지워지지 않고 남는다.

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

- 워크스페이스 uid 소유권·정리 대행(브로커가 U로 전환한 단명 자식으로 삭제/`/files` 읽기). 이것이
  들어와야 런처 직접 SIGKILL 뒤 최대 5초 창도 닫힌다. 둘은 함께 들어와야 한다: 세션 uid가 워크스페이스에
  쓰게 되면 게이트웨이가 익명 워크스페이스를 지우지 못해 모든 임대가 남고(지우지 못한 워크스페이스는 uid를
  쥔다), `CLAUDE_ISOLATION_EPHEMERAL_UIDS`번의 런 뒤에는 익명 요청이 전부 503이 된다.
- 브로커가 세션 uid로 transcript를 읽고 지우는 대행: 그 전에는 메모리에 없는(만료·재기동) uid 모드 세션의
  재개가 재시도로 풀리지 않는 HTTP 500 `transcript_unreadable`(`TranscriptUnreadable`)이고, stateless
  transcript 정리와 세션 uid가 쓴 `session-env/<id>`·`tasks/<id>` 정리가 되지 않는다.
- ephemeral uid 임대의 남은 틈: `/v1/agents/messages`·admin try-run은 호출자가 떠났을 때만 남은 CLI를 kill하므로
  정상 경로의 disconnect가 시간을 넘기면 그 CLI가 살아 있는 채로 임대가 끝나고, 요청이 클라이언트를 만들기 전에
  도는 슬래시 preflight 중에 삭제가 들어오면 그 짧은 카탈로그 CLI 아래에서 임대가 끝나며, kill된 CLI의 프로세스는
  브로커가 약 5초 안에 거둔다. 새 런은 안 쓴 uid부터, 그다음 가장 오래전에 반납된 uid를 받으므로 막 반납된 uid는
  이런 낙오자보다 훨씬 오래 쉰다. 그래도 낙오자가 남아 있으면 브로커가 그 uid의 새 spawn을 거부한다(살아 있는
  supervisor가 다른 워크스페이스로 쥐었거나 고아가 쥔 uid, `_check_uid_free`) — 공유 대신 그 런이 실패한다.
  런의 `/dev/shm` 객체는 아무도 지우지 않아, 같은 uid를 다음에 임대한 런이 읽을 수 있다.
- 브로커의 uid 검사는 uid 하나에 워크스페이스 하나를 가정한다. 정리를 포기한 고아가 있는 named 사용자는 그
  고아가 끝날 때까지 spawn하지 못하고, 두 워크스페이스에 동시에 살아 있는 named 사용자(레거시 `base/<user>`
  재개와 `base/<user>/claude` 세션)는 두 번째가 거부된다.
- uid 모드의 CLI 자식은 룰셋과 세션 셸 래퍼를 브로커 자신의 디렉터리(entrypoint 옆)에서 가져온다. 기동 검사는
  런처 쪽 파일만 보므로, 이미지처럼 넷을 한 디렉터리(`/usr/local/bin`)에 둔다.
- 사용자별 config dir(`CLAUDE_CONFIG_DIR`): uid 모드에서 `session-env/<id>`·`tasks/<id>`·`plugins/data`·자기
  projects 디렉터리를 세션 uid가 쓰려면 필요하다(지금은 app 소유).
- named 사용자 uid 회수: 지금은 영구라 안정 대역이 차면 새 named 사용자가 HTTP 500
  `uid_band_exhausted`다(재시도로 풀리지 않는다; 90%를 넘으면 경고).
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
