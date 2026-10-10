# 사용자별 프로세스 격리 (설계)

> **상태: 설계/계획.** 아직 구현되지 않았다. 메커니즘 선택은 prod 호스트의 커널·권한에
> 달려 있고, 그 사실은 `scripts/isolation_probe.py`를 prod 컨테이너에서 돌려 확정한다.
> 앱 계층(자격증명→워크스페이스/세션 스코프) 격리는 [security-user-isolation.md](security-user-isolation.md)를 보라.
> 이 문서는 그 아래, **OS/프로세스 계층**(한 세션이 다른 세션·게이트웨이의 파일/메모리/비밀에
> 닿지 못하게 하는 경계)을 다룬다.

## 1. 문제

게이트웨이는 사용자 채팅 턴마다 Claude CLI를 자식 프로세스로 띄우고, 그 자식에는
`Bash`/`Read`/`Write`가 기본 허용된다(`DEFAULT_ALLOWED_TOOLS`). 그 자식이 게이트웨이와
**같은 컨테이너·같은 uid(app, 1000)**에서 돌기 때문에, 한 사용자의 턴 하나로 다음이 가능하다.

### 모의해킹에서 확인된 두 가지

- **F1 — 임의 파일 읽기.** "파일을 읽는 코드를 작성해서 결과를 달라"는 프롬프트로
  `/etc/passwd`나 다른 사용자의 워크스페이스 파일 내용을 그대로 받아냈다.
- **F2 — `/proc/1/environ` 원문 노출.** PID 1의 환경변수를 가공 없이 받아냈다.

### 왜 현재 방어로 막히지 않나

- **워크스페이스 샌드박스는 명령줄의 *정적* 경로만 본다.** `src/backends/claude/workspace_sandbox.py`의
  `_check_bash`는 `cat /etc/passwd`처럼 명령줄에 리터럴로 보이는 경로를 막지만, 코드가 **런타임에
  만드는 경로**는 검사하지 못한다(코드 주석에 명시: 런타임 확장은 "OS 레벨 샌드박스로 위임"). 그래서
  워크스페이스 안에 `open('/etc/passwd')`를 담은 스크립트를 쓰고(`Write`, 워크스페이스 내부라 허용)
  `python3 r.py`로 실행하거나, `python3 -c "open(chr(47)+'etc'+...)"`, `cat $(...)` 같은 형태로
  **F1이 설계상 그대로 뚫린다.** CLI의 OS 레벨 샌드박스(`CLAUDE_SANDBOX_ENABLED`)는 **배선돼 있다** —
  `_configure_sandbox`(`client.py`)가 워크스페이스 샌드박스가 켜지면(prod 기본) 강제로 켠다. 그런데도
  F1을 못 막는 진짜 이유는 읽기를 **`Read` deny 규칙으로만** 제한하는데 게이트웨이가 그 규칙을 설정하지
  않기 때문이다(#218). 따라서 **prod 세션은 이미 CLI 자체의 bubblewrap 안에서 돌고 있을 수 있고**, 이는
  ②(아래)가 bwrap 안에 bwrap을 중첩하게 된다는 뜻이다 — ②를 택할 때 반드시 고려해야 한다.
- **PID 1 = 게이트웨이.** entrypoint가 `os.execvp`로 CMD를 exec 하므로 PID 1은 uvicorn 게이트웨이
  프로세스다. `/proc/1/environ`은 게이트웨이의 **마스킹되지 않은 전체 env**다.
- **자식 env 마스크는 F2를 못 막는다.** `child_env_mask()`(`sysinfo_redaction.py`)는 `ADMIN_API_KEY`·
  `API_KEY`를 **자식 자신의** env에서만 빈 값으로 덮는다. `/proc/1/environ`은 **부모**를 읽으므로
  마스크를 통째로 우회한다. 게다가 이 마스크는 출력 가림 스위치(`SYSINFO_REDACTION`)에 묶여 있어
  가림을 끄면 함께 꺼진다.

### 더 넓은 축: env 상속 구조

자식은 `os.environ` 전체를 상속하고, 빼는 것은 이름을 적은 소수뿐이다
(`_ISOLATION_VARS = ["OPENAI_API_KEY"]` + auth provider; `child_env_mask()`의 `ADMIN_API_KEY`/`API_KEY`).
그 결과 `USER_API_KEYS`(전체 사용자 bearer 토큰), `USAGE_LOG_DB_URL`·MySQL 비밀번호,
`CLAUDE_PLUGIN_GIT_TOKEN`, `GATEWAY_MCP_SERVER_ENV`(안의 `GITHUB_TOKEN`·`MCP_TOKEN` 등),
그리고 CLI가 쓰라고 일부러 주입하는 `ANTHROPIC_AUTH_TOKEN`이 모두 자식 env에 남는다. PR #229의
출력 가림은 **값을 표시하는 경로**만 가린다 — 값은 자식 env에 물리적으로 남아 있고, 턴은 값을 출력
없이 **그냥 사용**할 수 있다(DB 직접 접속, 상류 토큰으로 호출, 헤더에 담아 외부 요청). 따라서 출력
가림은 방어심층이지 경계가 아니다.

이 문서가 다루는 것은 "값을 가린다"가 아니라 **"다른 사용자/게이트웨이의 것에 OS가 닿지 못하게
한다"**는 경계다. 이는 CLAUDE.md의 #218(Bash OS 격리) 및 #173("two-user filesystem isolation")과
같은 범위의 작업이다.

## 2. 배포 형태 전제

운영은 **옵션 (a): 단일 고정 컨테이너**다. 테넌트별 샌드박스를 띄우는 오케스트레이터가 없으므로,
격리는 **컨테이너 안에서, 세션을 띄우는 순간** 걸어야 한다. 바깥(호스트/런타임 플래그)은 바꾸기
어렵지만, **이 레포의 이미지와 자식 spawn 경로는 바꿀 수 있다.**

주입 지점: SDK는 `cli_path=_get_cli_path()`로 CLI를 띄운다(`src/backends/claude/client.py`,
`slash_commands.py`). 따라서 **`CLAUDE_CLI_PATH`를 래퍼로 바꿔** 그 안에서 실제 `claude`를 선택한
격리 아래 re-exec 하면, SDK를 패치하지 않고 모든 세션에 격리를 건다.

> ⚠️ `_get_cli_path()`는 지금 **fail-open**이다: `CLAUDE_CLI_PATH`가 없거나 실행 불가면 경고만 찍고
> 번들 CLI로 떨어진다(매 세션 빌드마다 검사). 이미지 재빌드로 래퍼가 사라지면(+x 누락·경로 오류·볼륨
> 미마운트) **모든 새 세션이 격리 없이** 돌고 기동은 멀쩡하다. 격리 경계로 쓰려면 래퍼 부재를 기동
> `ConfigIssue(error)`나 세션 거부로 **fail-closed** 처리해야 한다.
>
> 대안: SDK엔 이미 CLI를 다른 uid로 띄우는 `ClaudeAgentOptions.user`(`types.py`)가 있다. 래퍼
> re-exec 대신 이 경로도 검토 가치가 있다(단, cap drop·Landlock·env allowlist는 여전히 직접 걸어야 한다).

## 3. 메커니즘 후보

| | ① uid-per-user + Landlock (+ /proc hidepid) | ② bubblewrap / nsjail |
|---|---|---|
| F2 (`/proc/1/environ`) | uid 분리 → 세션 uid ≠ PID1 uid라 EACCES | 새 PID 네임스페이스 → PID1이 세션 자신 |
| F1 (임의 파일 읽기) | Landlock이 open() 계층에서 강제(경로 생성 방식 무관) | 새 mount 네임스페이스 → 워크스페이스만 bind |
| 사용자 간 파일 | uid 소유권(DAC) | mount 네임스페이스에 애초에 없음 |
| 필요 권한 | `CAP_SETUID`/`SETGID`(root→1000 하락 **너머로 유지**) + 자식 종료용 `CAP_KILL`(또는 특권 헬퍼) | userns + mount — 기본 Docker seccomp가 `mount` 차단 → `seccomp=unconfined`/커스텀 또는 `CAP_SYS_ADMIN` |
| 컨테이너 친화성 | 높음(mount-ns·seccomp 안 건드림) | 낮음(바깥 경계를 넓힘) |
| 커널 요구 | Landlock ABI 2(cross-dir rename) ≥ 5.19 | unprivileged userns |

**옵션 (a)에서는 ①을 권장한다.** `CAP_SYS_ADMIN`(넓음) 대신 `CAP_SETUID`(좁음)로 끝나고, Landlock이
"코드로 파일 읽기" 우회까지 LSM 레벨에서 닫는다. ②는 ①이 불가능할 때의 폴백이다.

> **중간 선택지 — Landlock 단독(uid 전환 없음).** cap이 아예 없어도(①의 cap 유지·자식 종료 문제가
> 통째로 사라진다) Landlock은 open() 계층에서 파일 읽기를 가두고, 충분히 새 커널(ptrace scoping)에서는
> `/proc/1/environ` 읽기(F2)까지 막는다. 사용자 간 파일 격리(DAC 소유권)는 주지 못해 완전한 ①의 대체는
> 아니지만, cap 변경이 막힌 prod에선 F1/F2를 당장 닫는 현실적 1차 방어가 된다.

> 참고: 게이트웨이는 현재 entrypoint에서 **root→uid 1000**으로 떨어져 **비특권**으로 돈다(CLI가 root에서
> `--dangerously-skip-permissions`를 거부하기 때문). 그 하락이 **모든 effective/permitted cap을 지우므로**
> `cap_add: [SETUID, SETGID]`만으로는 부족하다 — cap을 하락 너머로 유지(prctl `KEEPCAPS`+ambient)하거나
> 작은 setuid-root 런처가 필요하다(단 `NoNewPrivs=1`이면 setuid 런처는 막힌다).
>
> **①의 숨은 비용 두 가지:**
> - **cap은 exec 직전에 버려야 한다.** 비-root(1000→U) `setuid`는 cap을 자동으로 비우지 않고 ambient
>   cap은 `execve`를 넘어 살아남는다. 세션에 `CAP_SETUID`가 남으면 다른 사용자·1000·0 uid로 되돌아갈 수
>   있어 경계가 무너진다. 래퍼는 uid 전환 직전 ambient를 비우고 permitted/bounding을 내리고
>   `no_new_privs`를 건 뒤 exec 해야 한다(Landlock `restrict_self`도 `no_new_privs`를 요구한다).
> - **게이트웨이가 자식을 못 죽인다.** uid 1000(CAP_KILL 없음)은 uid U 자식에 시그널을 못 보낸다.
>   SDK `close()`는 `ProcessLookupError`만 무시하므로(`subprocess_cli.py`) teardown이 `PermissionError`로
>   깨지고, 멈춘 도구·긴 Bash가 안 죽어 프로세스가 샌다(#147의 SIGTERM→SIGKILL 에스컬레이션도 무력화).
>   → 게이트웨이에 `CAP_KILL`을 주거나 죽이기를 특권 헬퍼에 위임해야 한다.

## 4. 메커니즘 선택: 역량 프로브

`scripts/isolation_probe.py`를 **prod 컨테이너 안에서, 세션과 같은 app 유저로** 돌린다. 표준
라이브러리만 쓰고 읽기 전용이며(네임스페이스/`mount`/`setuid` 테스트는 전부 fork된 자식에서만 수행
후 종료), 커널 버전·CapEff/CapBnd·seccomp·`/proc` hidepid·`/proc/1/environ` 가독성(F2 재현)·
Landlock ABI·unshare/mount 가능성·setuid 가능성·공유 자산에 다른 uid가 닿는지를 찍고 판정을 낸다.

```bash
docker compose cp scripts/isolation_probe.py gateway:/tmp/isolation_probe.py
docker compose exec -u app gateway python3 -I /tmp/isolation_probe.py
```

> `docker compose exec`는 USER 지시자가 없어 기본 root로 붙는다. 세션 맥락을 반영하려면 **`-u app`**가
> 필수다 — root는 언제나 setuid가 되므로 root로 돌리면 프로브는 **판정을 내지 않는다**. 개발 머신(WSL2)에서
> 돌린 값은 스모크 테스트일 뿐 prod가 아니다(그곳은 PID1=게이트웨이, 기본 seccomp가 `mount`를 막는 식으로
> 다르다).

판정 규칙:

- `Landlock usable = True`(ABI 2) 그리고 (`uid-per-user usable now = True` 또는 bounding set에
  SETUID/SETGID 존재) → **① uid-per-user + Landlock.** 단 **`cap_add`만으로는 안 된다**: entrypoint의
  root→1000 하락이 CapEff를 비우므로, cap을 하락 너머로 유지(KEEPCAPS+ambient)하거나 setuid 헬퍼가
  필요하다(§3). `Landlock usable = True`인데 uid 전환이 막혔다면 **Landlock 단독**(§3 중간 선택지)을 고려.
- 위가 안 되고 `bwrap/nsjail viable = True` → **② bwrap/nsjail**(seccomp를 열어야 할 수 있음).
- 둘 다 아니면 프로브가 무엇을 열어야 하는지(커널·cap·seccomp) 나열한다.

## 5. 반드시 지킬 제약: plugin/skill 공유 유지

**plugin 등으로 설치된 스킬은 모든 사용자에게 계속 공유되어야 한다.** 이 격리가 그걸 깨면 안 된다.

- `~/.claude/{plugins,skills,agents,commands,output-styles}` + user-scope `CLAUDE.md`은 기동 때
  `install_plugins.py`가 **한 곳에 한 번만** 설치한다(지금처럼). 이들은 **읽기 전용 공유 트리**로
  두고, **모든 세션 uid/감옥에 ro 접근을 허용**한다.
- **쓰기 상태만 분리**한다: `projects/`, `plans/`, SDK가 쓰는 cache/history, 그리고 워크스페이스.
- ⚠️ **`~/.claude/settings.json`은 세션별로 나누지 말 것 — 공유 *정책*이다.** admin이 관리하는 env 블록이
  여기 쓰이고(`claude_settings_env.py`, `user` setting source로 모든 세션에 적용), `claude plugin install
  --scope user`가 **플러그인 활성화(`enabledPlugins`)를 이 파일에 기록**한다. 세션마다 새 `settings.json`을
  주면 플러그인은 설치돼 있어도 **활성화가 안 돼 스킬이 사라지고**(위 필수 제약 위반) admin env 블록도
  조용히 안 먹는다. 이 파일은 공유 ro(또는 공유 + 세션 오버레이)로 두고, 리팩터 전에 prod
  `~/.claude/settings.json`의 `enabledPlugins`를 먼저 확인한다.
- 메커니즘별 실현:
  - **①**: 공유 트리는 world/group-readable(`0755`/`0644`) + 각 세션에 Landlock *ro 허용* 규칙.
    ⚠️ **트리 자체의 모드만으로는 부족하다.** 다른 uid는 경로의 **모든 상위 디렉터리**에 o+x가 있어야
    닿는데, Dockerfile의 `useradd -m`이 만든 `/home/app`은 **0700**이다(trixie `login.defs`의
    `HOME_MODE 0700`; 로컬 빌드 이미지로 확인, prod 값은 프로브가 찍는다). 그대로면 `skills/`가 0755여도
    세션 uid는 `~/.claude` 아래 공유 자산에 전혀 닿지 못하고, 세션 HOME에서 symlink로 이어도 대상 경로가
    같은 이유로 막힌다. 공유 트리를 HOME 밖(예: `/opt/claude-shared`, `0755`)으로 옮겨 세션 config dir에서
    연결하는 쪽을 권한다. `/home/app`에 o+x(`0711`)를 주는 방법도 있지만, 그러면 HOME 아래에서 경로를
    아는 o+r 파일이 전부 세션 uid에 열린다.
  - **②**: 공유 트리를 각 감옥에 **ro bind-mount**, 워크스페이스만 rw bind.
- 이는 **세션별 HOME/`CLAUDE_CONFIG_DIR` 리팩터**를 수반한다(지금은 HOME 하나를 공유하므로
  `make_claude_home_guard_hook`로 가리는 중). 공유 자산은 세션 HOME에 ro로 mount/symlink 한다.
- **검증 항목**: CLI가 세션별 config dir에서 공유 스킬/플러그인을 **여전히 인식**하는지 e2e로 확인.
  프로브의 "SHARED ASSET LAYOUT" 섹션이 자산마다 소유권·권한과 함께 **다른 uid가 닿는지**(막는 상위
  디렉터리)와 **안에서 못 쓰는 항목 수**(o+r 없는 파일, o+x 없는 실행 파일·디렉터리)를 찍으니 리팩터
  범위 산정에 쓴다.

## 6. spawn 설계 (메커니즘 공통 골격)

`CLAUDE_CLI_PATH` 래퍼가 하는 일:

1. 사용자 → **숫자 uid** 결정적 매핑(예약 대역. `/etc/passwd` 항목 불필요 — Linux는 숫자 uid로의
   `setuid`에 계정 레코드를 요구하지 않는다).
2. **세션별 HOME/`CLAUDE_CONFIG_DIR`** 준비: 쓰기 가능한 per-session `.claude`(projects/plans/
   cache/history) + 공유 자산(`settings.json` 포함)을 ro로 연결(§5 — `settings.json`은 공유 정책이라
   세션별로 나누지 않는다).
3. 격리 적용:
   - **①**: `no_new_privs` 설정 → Landlock 룰셋 적용(ro: 시스템 경로·CLI 설치·공유 자산 / rw:
     워크스페이스·per-session `.claude`) → **ambient cap 비우고 permitted/bounding 하락** →
     `setgid`/`setuid`로 사용자 uid 전환 → **`CAP_SETUID`/`SETGID` 포함 잔여 cap 전부 drop** → 실제
     `claude` exec. (cap을 남기면 세션이 임의 uid로 되돌아간다 — §3.)
   - **②**: `bwrap --unshare-pid --ro-bind <공유 자산> … --bind <워크스페이스> …`로 깨끗한 env와 함께
     실제 `claude` exec. ⚠️ **`--unshare-net`은 쓰지 말 것**: 빈 net ns는 loopback만 남아 CLI가
     `ANTHROPIC_BASE_URL`(host.docker.internal:3000)·게이트웨이 loopback MCP relay에 닿지 못해 **첫 턴부터
     실패**한다(그런데도 relay의 `reachable()` self-probe는 통과해 계속 relay로 라우팅한다). egress 통제는
     별도 레이어로(§8). 또한 prod에서 CLI 자체 bwrap이 이미 돌 수 있어 **bwrap 중첩**(§1)을 확인해야 한다.
4. **env는 allowlist로 구성**하되, CLI가 필요로 하는 것(`ANTHROPIC_*`, `PATH`/`HOME`/로케일)만이 아니라
   **게이트웨이가 자식에 심는 제어 변수까지 명시로 포함**해야 한다. `CLAUDE_CODE_HARBOR_KITE`는 **빈 값·미설정이
   곧 ON**이라(`src/constants.py`가 `=0`을 박아 두어야 꺼진다) allowlist에서 빠지면 교차 세션 메시징이 다시
   열린다 — "자연히 사라진다"가 아니라 **그 반대다.** 반드시 함께 넘길 것: `CLAUDE_CODE_HARBOR_KITE=0`,
   `MCP_TOOL_TIMEOUT`, `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS`, SDK가 주는 `CLAUDE_CODE_ENTRYPOINT`/
   `CLAUDE_AGENT_SDK_VERSION`, loopback `NO_PROXY` 항목. §1의 비밀들만 애초에 전달하지 않는다.
5. 가능하면 `/proc`를 `hidepid=2`로(교차 uid `ps`/argv 노출 차단; `--mcp-config`가 argv에 실림).

## 7. 단계별 계획

1. **(이 PR)** 역량 프로브 + 본 설계 문서. prod에서 프로브 실행 → 메커니즘 확정.
2. **세션별 HOME 리팩터** (공유 ro 자산 보존). 가장 공유-스킬 민감한 부분 — 먼저, 독립적으로.
3. **특권 spawner + `CLAUDE_CLI_PATH` 래퍼** (① uid+Landlock 또는 ② bwrap).
4. **워크스페이스 uid 소유권** + `/proc` 하드닝. ⚠️ 세션이 만든 파일이 uid U 소유가 되면 **uid 1000인
   게이트웨이가 못 지운다**: `workspace_manager.cleanup_temp_workspace`(rmtree)와
   `agent_messages._cleanup_sdk_transcript`가 조용히 실패해 stateless 정리 보장이 깨지고, 업로드·`/files`
   라우트는 U 소유 디렉터리에서 EACCES가 난다. 해결하려면 `CAP_DAC_OVERRIDE`/`FOWNER`/`CHOWN`이나 특권
   헬퍼가 필요하다(= 표의 "좁은 권한" 주장과 상충 — 이 비용을 설계에 반영해야 한다).
5. **검증**: 2-사용자 파일 격리 테스트, `/proc/1/environ` EACCES 확인, **스킬 공유 유지** e2e,
   기존 게이트웨이 스위트.
6. 중복이 된 임시 방어를 안전한 선에서 정리(`make_claude_home_guard_hook`은 방어심층으로 격하).

## 8. 열린 질문 (prod 사실에 의존)

- 커널 버전 / Landlock ABI, 부여 가능한 cap, seccomp 프로파일 → **프로브가 답한다.**
- `/proc` `hidepid` 리마운트가 이 컨테이너에서 가능한가(CAP_SYS_ADMIN 필요할 수 있음).
- uid 할당 대역과 수명(생성·회수), 동시 사용자 상한.
- 격리 단위: **사용자(테넌트)별**이 기본으로 충분(같은 사용자의 다른 세션은 기밀성 위협이 아님).
- **resume와의 상호작용**: `/v1/responses`의 세션 재개가 턴을 넘어 **같은 uid/HOME로** 매핑되어야
  한다(세션 매니저 ↔ uid 매핑의 안정성).
- 네트워크 egress 통제(①은 egress를 다루지 않음 — 별도 레이어 / #173의 zero-egress 게이트).
