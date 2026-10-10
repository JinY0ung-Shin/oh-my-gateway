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
  **F1이 설계상 그대로 뚫린다.** 이를 잡아야 할 OS 레벨 샌드박스(`CLAUDE_SANDBOX_ENABLED`)는 어디에도
  배선되어 있지 않다.
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

## 3. 메커니즘 후보

| | ① uid-per-user + Landlock (+ /proc hidepid) | ② bubblewrap / nsjail |
|---|---|---|
| F2 (`/proc/1/environ`) | uid 분리 → 세션 uid ≠ PID1 uid라 EACCES | 새 PID 네임스페이스 → PID1이 세션 자신 |
| F1 (임의 파일 읽기) | Landlock이 open() 계층에서 강제(경로 생성 방식 무관) | 새 mount 네임스페이스 → 워크스페이스만 bind |
| 사용자 간 파일 | uid 소유권(DAC) | mount 네임스페이스에 애초에 없음 |
| 필요 권한 | **좁은** `CAP_SETUID`/`SETGID`(또는 작은 setuid 헬퍼) | userns + mount — 기본 Docker seccomp가 `mount` 차단 → `seccomp=unconfined`/커스텀 또는 `CAP_SYS_ADMIN` |
| 컨테이너 친화성 | 높음(mount-ns·seccomp 안 건드림) | 낮음(바깥 경계를 넓힘) |
| 커널 요구 | Landlock ≥ 5.13 | unprivileged userns |

**옵션 (a)에서는 ①을 권장한다.** `CAP_SYS_ADMIN`(넓음) 대신 `CAP_SETUID`(좁음)로 끝나고, Landlock이
"코드로 파일 읽기" 우회까지 LSM 레벨에서 닫는다. ②는 ①이 불가능할 때의 폴백이다.

> 참고: 게이트웨이는 현재 entrypoint에서 uid 1000으로 떨어져 **비특권**으로 돈다(CLI가 root에서
> `--dangerously-skip-permissions`를 거부하기 때문). 비특권 프로세스는 임의 uid로 `setuid` 할 수
> 없으므로, ①은 컨테이너에 `cap_add: [SETUID, SETGID]`를 주고 그 권한을 uid 하락 뒤에도 유지
> (ambient cap)하거나, 작은 setuid-root 런처를 두는 변경이 필요하다.

## 4. 메커니즘 선택: 역량 프로브

`scripts/isolation_probe.py`를 **prod 컨테이너 안에서, 세션과 같은 app 유저로** 돌린다. 표준
라이브러리만 쓰고 읽기 전용이며(네임스페이스/`mount`/`setuid` 테스트는 전부 fork된 자식에서만 수행
후 종료), 커널 버전·CapEff/CapBnd·seccomp·`/proc` hidepid·`/proc/1/environ` 가독성(F2 재현)·
Landlock ABI·unshare/mount 가능성·setuid 가능성·공유 자산 현황을 찍고 판정을 낸다.

```bash
docker compose cp scripts/isolation_probe.py gateway:/tmp/isolation_probe.py
docker compose exec -u app gateway python3 -I /tmp/isolation_probe.py
```

> `docker compose exec`는 USER 지시자가 없어 기본 root로 붙는다. 세션 맥락을 반영하려면 **`-u app`**가
> 필수다. 개발 머신(WSL2)에서 돌린 값은 스모크 테스트일 뿐 prod가 아니다(그곳은 PID1=게이트웨이, 기본
> seccomp가 `mount`를 막는 식으로 다르다).

판정 규칙:

- `Landlock usable = True` 그리고 (`uid-per-user usable now = True` 또는 `SETUID … grantable = True`)
  → **① uid-per-user + Landlock.** 후자만 True면 compose에 `cap_add: [SETUID, SETGID]`만 추가.
- 위가 안 되고 `bwrap/nsjail viable = True` → **② bwrap/nsjail**(seccomp를 열어야 할 수 있음).
- 둘 다 아니면 프로브가 무엇을 열어야 하는지(커널·cap·seccomp) 나열한다.

## 5. 반드시 지킬 제약: plugin/skill 공유 유지

**plugin 등으로 설치된 스킬은 모든 사용자에게 계속 공유되어야 한다.** 이 격리가 그걸 깨면 안 된다.

- `~/.claude/{plugins,skills,agents,commands}` + user-scope `CLAUDE.md`은 기동 때
  `install_plugins.py`가 **한 곳에 한 번만** 설치한다(지금처럼). 이들은 **읽기 전용 공유 트리**로
  두고, **모든 세션 uid/감옥에 ro 접근을 허용**한다.
- **쓰기 상태만 분리**한다: `projects/`, `plans/`, SDK가 쓰는 settings/cache/history, 그리고
  워크스페이스.
- 메커니즘별 실현:
  - **①**: 공유 트리는 world/group-readable(`0755`/`0644`) + 각 세션에 Landlock *ro 허용* 규칙.
  - **②**: 공유 트리를 각 감옥에 **ro bind-mount**, 워크스페이스만 rw bind.
- 이는 **세션별 HOME/`CLAUDE_CONFIG_DIR` 리팩터**를 수반한다(지금은 HOME 하나를 공유하므로
  `make_claude_home_guard_hook`로 가리는 중). 공유 자산은 세션 HOME에 ro로 mount/symlink 한다.
- **검증 항목**: CLI가 세션별 config dir에서 공유 스킬/플러그인을 **여전히 인식**하는지 e2e로 확인.
  프로브의 "SHARED ASSET LAYOUT" 섹션이 현재 소유권·권한을 찍으니 리팩터 범위 산정에 쓴다.

## 6. spawn 설계 (메커니즘 공통 골격)

`CLAUDE_CLI_PATH` 래퍼가 하는 일:

1. 사용자 → **숫자 uid** 결정적 매핑(예약 대역. `/etc/passwd` 항목 불필요 — Linux는 숫자 uid로의
   `setuid`에 계정 레코드를 요구하지 않는다).
2. **세션별 HOME/`CLAUDE_CONFIG_DIR`** 준비: 쓰기 가능한 per-session `.claude`(projects/plans/
   settings/cache) + 공유 자산을 ro로 연결(§5).
3. 격리 적용:
   - **①**: Landlock 룰셋 적용(ro: 시스템 경로·CLI 설치·공유 자산 / rw: 워크스페이스·per-session
     `.claude`) → `setgid`/`setuid`로 사용자 uid 전환 → 실제 `claude` exec.
   - **②**: `bwrap --unshare-pid --unshare-net --ro-bind <공유 자산> … --bind <워크스페이스> …`로
     깨끗한 env와 함께 실제 `claude` exec.
4. **env는 allowlist로 구성**: CLI가 필요로 하는 것(`ANTHROPIC_*`, `PATH`/`HOME`/로케일 등)만
   넘기고 나머지(§1의 비밀들)는 애초에 전달하지 않는다. 금지 목록과 달리 "빈 값=켜짐"으로 읽히는 CLI
   변수 문제(예: `CLAUDE_CODE_HARBOR_KITE`)도 자연히 사라진다.
5. 가능하면 `/proc`를 `hidepid=2`로(교차 uid `ps`/argv 노출 차단; `--mcp-config`가 argv에 실림).

## 7. 단계별 계획

1. **(이 PR)** 역량 프로브 + 본 설계 문서. prod에서 프로브 실행 → 메커니즘 확정.
2. **세션별 HOME 리팩터** (공유 ro 자산 보존). 가장 공유-스킬 민감한 부분 — 먼저, 독립적으로.
3. **특권 spawner + `CLAUDE_CLI_PATH` 래퍼** (① uid+Landlock 또는 ② bwrap).
4. **워크스페이스 uid 소유권** + `/proc` 하드닝.
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
