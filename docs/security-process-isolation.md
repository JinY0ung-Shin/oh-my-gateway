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
  런타임에 경로를 만드는 코드를 워크스페이스에 쓰고(`Write`, 워크스페이스 내부라 허용) 실행하면 **F1이
  설계상 그대로 뚫린다.**
  CLI의 OS 레벨 샌드박스(`CLAUDE_SANDBOX_ENABLED`)는 코드에 **배선돼 있지만**(`_configure_sandbox`,
  `client.py`가 워크스페이스 샌드박스가 켜지면 강제로 켠다) **이 레포의 Dockerfile이 만든 이미지에서는
  실제로 돌지 않는다.** 두 가지가 겹친다. (1) 샌드박스는 `bubblewrap`/`socat` 바이너리를 요구하는데
  Dockerfile은 둘 다 설치하지 않는다(`apt-get` 줄에 없음). (2) `SandboxSettings`에 `failIfUnavailable`을
  아무도 세팅하지 않는다. 그래서 번들 CLI 2.1.283은 의존성 부재를 "disabled"로 넘기고 **stderr에 경고만
  찍은 뒤 Bash를 샌드박스 없이** 실행한다(게이트웨이는 그 stderr를 소비하지 않아 컨테이너 로그에만 남는다).
  결과:
  - #218의 `Read` deny 규칙만으로는 Bash에 효과가 없다 — 이 이미지에선 샌드박스가 돌지 않는다.
  - 다만 비공개 `GATEWAY_BUILD_INSTALL_SCRIPT`가 prod에서 `bubblewrap`/`socat`을 설치했을 수 있다. CLI의
    가용성 검사는 `bwrap`·`socat`이 PATH에 있는지만 본다(ripgrep은 번들된 것을 쓴다). 그래서 둘이 있고
    기본 seccomp처럼 userns가 막혀 있으면 "Sandbox disabled" 없이 **모든 Bash 호출이
    `bwrap: No permissions to create new namespace`로
    실패한다**(게이트웨이 기본값 `allowUnsandboxedCommands=false`, CLI 2.1.283으로 확인). **prod 컨테이너에서
    `command -v bwrap socat`로 확인**한다: 있고 userns가 거부되면 Bash는 이미 실패 중이고, 없으면 Bash는
    제한 없이 돈다(그리고 둘이 있으면서 userns가 열려 있다면 ②가 CLI 샌드박스를 중첩한다).
- **PID 1 = 게이트웨이(기본 구성).** entrypoint가 `os.execvp`로 CMD를 exec 하므로 PID 1은 uvicorn 게이트웨이
  프로세스다. `/proc/1/environ`은 게이트웨이의 **마스킹되지 않은 전체 env**다. 단 compose `init: true`/
  `--init`(#147 고아 회수의 그럴듯한 해법)을 쓰면 PID 1은 root인 docker-init이고 게이트웨이는 그 자식이다 —
  그때 F2의 대상은 PID 1이 아니라 **게이트웨이 자식 프로세스**다(이하 "PID 1"은 이 경우 게이트웨이 프로세스로
  읽는다. 프로브는 init shim을 감지해 PID 1의 자식을 따라간다).
- **자식 env 마스크는 F2를 못 막는다.** `child_env_mask()`(`sysinfo_redaction.py`)는 `ADMIN_API_KEY`·
  `API_KEY`를 **자식 자신의** env에서만 빈 값으로 덮는다. `/proc/1/environ`은 **부모**를 읽으므로
  마스크를 통째로 우회한다. 게다가 이 마스크는 출력 가림 스위치(`SYSINFO_REDACTION`)에 묶여 있어
  가림을 끄면 함께 꺼진다.

### 더 넓은 축: env 상속 구조

자식은 `os.environ` 전체를 상속하고, 빼는 것은 이름을 적은 소수뿐이다
(`_ISOLATION_VARS = ["OPENAI_API_KEY"]` + auth provider; `child_env_mask()`의 `ADMIN_API_KEY`/`API_KEY`).
그 결과 사용자 API 키, DB 접속 정보, 플러그인/MCP 자격증명 같은 게이트웨이 전용 비밀과, CLI가 쓰라고
일부러 주입하는 `ANTHROPIC_AUTH_TOKEN`이 모두 자식 env에 남는다. PR #229의
출력 가림은 **값을 표시하는 경로**만 가린다 — 값은 자식 env에 물리적으로 남아 있고, 턴은 값을 출력
없이 **그냥 사용**할 수 있다(상류 토큰으로 호출하거나, 자격증명을 헤더에 담아 요청). 따라서 출력
가림은 방어심층이지 경계가 아니다.

> 참고: `child_env_mask()`가 가리는 이름은 `SYSINFO_CHILD_ENV_MASK`(CSV)로 늘릴 수 있다. 위 게이트웨이
> 전용 비밀은 게이트웨이 쪽에서만 읽히므로 이 노브로 **지금도 자식 env에서 지울 수 있다**(코드 변경 없이). 다만
> 이 마스크는 `SYSINFO_REDACTION`에 묶여 있어 가림을 끄면 함께 꺼지고, `/proc/1/environ`에는 **부모**
> 값이 그대로라 F2를 못 막는다 — §6.4의 allowlist가 이 노브를 대체해야 한다.

### 또 하나의 축: 게이트웨이 코드가 세션과 같은 uid 소유다

Dockerfile(`chown -R app:app /app /home/app`)과 entrypoint가 **`/app`** — 게이트웨이 코드 `/app/src`,
`/app/docs`, `/app/data`, `pyproject.toml`·`uv.lock` — 를 uid 1000 소유로 만든다(의존성은 venv가 아니라
root 소유 `/usr/local/lib/python3.12/site-packages`에 있다). 세션 Bash도 uid 1000이라, 한 세션이
**게이트웨이 자신의 코드를 덮어쓸 수 있다** — 재기동 뒤 이후 모든 세션에 영향을 주는 **지속적 발판**이다. 따라서 격리 설계는 비밀을
읽는 것뿐 아니라 **코드 쓰기 경로**도 닫아야 한다(§3·§5·§6: 같은-uid 옵션이면 `/app`을 읽기 전용 집합에,
uid 분리면 세션 uid를 `app`과 다르게 두고 `/app`에 쓰기 금지; 이미지가 애초에 쓰기 상태만 app 소유로
넘기도록 바꾸는 선택지도 있다). 같은 맥락에서 root 단계(entrypoint의 소유권 복구)는 #233에서
하드닝된다: symlink를 따라가지 않고, 대상 uid 소유가 아닌 다중 링크 비디렉터리는 건너뛰며(그래서
호스트 sysctl `fs.protected_hardlinks`에 의존하지 않는다), root 단계 인터프리터를 격리 모드(`python -I`)로
돌려 app 사용자의 site 디렉터리를 읽지 않고, 읽기 전용·예기치 않은 경로가 있어도 기동을 실패시키지 않는다.

> 운영 주의: 이미지는 `HOME=/home/app`을 전역으로 설정하므로 root로 `docker exec` 하면 그 HOME을 물려받아
> root 도구가 app 소유 dotfile과 user site를 읽을 수 있다. 점검·디버깅은 `-u app`으로 붙는다(프로브 안내와 동일).

이 문서가 다루는 것은 "값을 가린다"가 아니라 **"다른 사용자/게이트웨이의 것에 OS가 닿지 못하게
한다"**는 경계다. 이는 CLAUDE.md의 #218(Bash OS 격리) 및 #173("two-user filesystem isolation")과
같은 범위의 작업이다.

## 2. 배포 형태 전제

운영은 **옵션 (a): 단일 고정 컨테이너**다. 테넌트별 샌드박스를 띄우는 오케스트레이터가 없으므로,
격리는 **컨테이너 안에서, 세션을 띄우는 순간** 걸어야 한다. 바깥(호스트/런타임 플래그)은 바꾸기
어렵지만, **이 레포의 이미지와 자식 spawn 경로는 바꿀 수 있다.**

주입 지점: SDK는 `cli_path=_get_cli_path()`로 CLI를 띄운다(`src/backends/claude/client.py`,
`slash_commands.py`). 따라서 **`CLAUDE_CLI_PATH`를 래퍼로 바꿔** 그 안에서 실제 `claude`를 선택한
격리 아래 re-exec 하면, SDK를 패치하지 않고 **모든 Claude 백엔드 세션**에 격리를 건다.

> ⚠️ **이 래퍼는 Claude 백엔드에만 적용된다.** `_get_cli_path()`를 타지 않는 spawn 경로는 격리 밖이다:
> opt-in app-server 어댑터(`src/backends/appserver/transport.py`)는 `codex app-server`를
> `os.environ.copy()` 그대로, 래퍼 없이 자기 프로세스로 띄우고 `child_env_mask()`도 적용하지 않아
> `ADMIN_API_KEY`/`API_KEY`까지 자식에 샌다(frozen `codex`/`opencode` 백엔드도 동일). 따라서
> `BACKENDS=codex`(+`CODEX_BACKEND=appserver`) 등을 켜면 그 세션은 **F1/F2가 다시 열린다.** 이 문서가
> #173("two-user filesystem isolation" 게이트)와 같은 범위라고 적지만, 래퍼만으로는 #173의 codex 경로를
> 커버하지 못한다 — **#173 cutover는 app-server spawn 경로가 같은 래퍼(또는 동등한 격리)를 타기 전에는
> 완료로 쳐선 안 된다.** prod의 `BACKENDS` 값은 사용자 확인 필요.

> ⚠️ `_get_cli_path()`는 지금 **fail-open**이다: `CLAUDE_CLI_PATH`가 없거나 실행 불가면 경고만 찍고
> 번들 CLI로 떨어진다(매 세션 빌드마다 검사). 이미지 재빌드로 래퍼가 사라지면(+x 누락·경로 오류·볼륨
> 미마운트) **모든 새 세션이 격리 없이** 돌고 기동은 멀쩡하다. 격리 경계로 쓰려면 래퍼 부재를 기동
> `ConfigIssue(error)`나 세션 거부로 **fail-closed** 처리해야 한다.
>
> ⚠️ **래퍼는 "어느 사용자인가"를 스스로 알지 못한다.** §6의 첫 단계는 "사용자 → uid 매핑"인데,
> 게이트웨이는 그 신원을 자식에게 신뢰 가능한 형태로 넘기지 않는다(현재는 system prompt 텍스트와
> caller가 준 metadata뿐, cwd가 유일한 힌트). 게다가 세션이 아닌 `cli_path` spawn이 여럿 있다:
> SDK가 매 connect마다 거는 `claude -v` 버전 체크(env·cwd·user 모두 없음), 슬래시 명령 카탈로그/preflight
> (`slash_commands.py`, `child_env_mask()`도 미적용), `/v1/commands`, 기동 로그. 래퍼를 신원 필수로
> **fail-closed** 설계하면 이들이 깨진다 — 슬래시 턴은 500, `HIDDEN_SKILLS` 경로(`client.py`의
> `get_available_commands`, try/except 없음)는 **새 세션마다 실패**한다. 반대로 세션 아닌 spawn도
> 워크스페이스 project 설정(훅 포함)을 로드하므로, **신원이 없다고 격리 없이 통과시켜선 안 된다.**
> → 래퍼에 **신원 전달 채널**(예: 세션 전용 env/fd로
> uid를 주입)과 "세션 아닌 spawn"의 처리 규칙을 먼저 정의해야 한다.
>
> 대안: SDK엔 이미 CLI를 다른 uid로 띄우는 `ClaudeAgentOptions.user`(`types.py`)가 있다. 단 이것은 래퍼의
> **대체가 아니다**: (1) 자식 내부에서 뭔가를 실행할 훅이 없어(anyio `open_process`에 `preexec_fn` 없음)
> **Landlock 룰셋·env allowlist는 이 경로로 걸 수 없고**, 부모에서 Landlock을 걸면 게이트웨이 자신이
> 갇힌다. (2) `group=`을 안 줘서 **gid는 1000으로 남는다** — 세션들이 gid 1000을 공유하므로 서로의
> group-readable 파일을 읽을 수 있고 새로 만드는 파일도 gid 1000이 돼 **DAC 파일 격리가 약해진다**.
> (3) 문자열 uid는 `getpwnam` 실패 → 정수만 먹는데 타입힌트는 `str|None`이다.
> `no_new_privs`·cap drop은 게이트웨이 자신에 미리 걸면 이 경로로도 되지만, Landlock·allowlist·gid 때문에
> **결국 `cli_path` 래퍼가 필요**하다.

## 3. 메커니즘 후보

| | ① uid-per-user + Landlock | ② bubblewrap / nsjail |
|---|---|---|
| F2 (`/proc/1/environ`) | uid 분리 → 세션 uid ≠ PID1 uid라 EACCES | 새 PID 네임스페이스 → PID1이 세션 자신 |
| F1 (임의 파일 읽기) | Landlock이 open() 계층에서 강제(경로 생성 방식 무관) | 새 mount 네임스페이스 → 워크스페이스만 bind |
| 사용자 간 파일 | uid 소유권(DAC) | mount 네임스페이스에 애초에 없음 |
| 필요 권한 | **특권 spawner**(아래)가 `CAP_SETUID`/`SETGID`**만** 보유(종료·정리는 세션 uid로 전환한 단명 자식이 소유자로서 수행). **게이트웨이 본체는 cap 0.** | userns + mount — 기본 Docker seccomp가 `mount`를 `CAP_SYS_ADMIN` 뒤로 막고, AppArmor `docker-default`는 `deny mount`라 **seccomp를 풀어도** `apparmor=unconfined`/커스텀 프로파일이 추가로 필요 |
| 컨테이너 친화성 | 높음(mount-ns·seccomp 안 건드림) | 낮음(바깥 경계를 넓힘) |
| 커널 요구 | Landlock ABI 2(cross-dir rename) ≥ 5.19 | unprivileged userns |

**옵션 (a)에서는 ①을 권장한다.** ②는 컨테이너 바깥 경계(userns+mount+AppArmor)를 넓히는 반면, ①은
그 범위를 **작은 특권 spawner 하나**에 가두고 Landlock이 "코드로 파일 읽기" 우회까지 LSM 레벨에서 닫는다.
②는 ①이 불가능할 때의 폴백이다.

> **⚠️ 특권은 게이트웨이가 아니라 별도 spawner가 쥔다 (가장 중요).** ①에 필요한 cap을 게이트웨이 본체에
> 남기는 방식(`KEEPCAPS`+ambient로 하락 너머 유지)은 **채택하지 않는다.** ambient cap은 `execve`를 넘어
> 살아남으므로, 게이트웨이가 ambient `CAP_SETUID`를 쥐면 **게이트웨이가 exec하는 모든 것**(플러그인 설치,
> `plugin_admin`의 `claude`/`git`, MCP 연결 테스트가 띄우는 admin 지정 명령, app-server, fail-open 시
> 번들 CLI)이 그 cap을 물려받아 임의 uid(0 포함)로 전환할 수 있다. 대신 특권을 별도 프로세스로 격리한다:
> - **root가 fork한 특권 spawn 브로커 — 사실상 필수.** entrypoint가 `drop_privileges` **전에** 작은
>   브로커를 fork해 상주시키고, 게이트웨이는 cap 0으로 떨어진다. 게이트웨이는 소켓으로 "이 uid로 이
>   워크스페이스에서 세션을 띄워라"를 요청하고, 브로커가 **요청을 검증**한 뒤 uid 전환·exec을 대행한다.
>   브로커는 **`CAP_SETUID`/`SETGID`만** 쥔다(최소 권한). 자식 종료와 턴 이후 정리는 브로커가 **단명 자식을
>   fork해 세션 uid U로 전환**(`setgroups`→`setgid`→`setuid`, 신뢰 못 할 코드는 exec하지 않음)시켜, 그 자식이 U의
>   프로세스에 시그널을 보내고 U의 파일을 **소유자로서** 지우거나 chmod 하게 한다. Landlock 시그널
>   scoping은 샌드박스 도메인 **안에서 밖으로** 가는 시그널을 막을 뿐 샌드박스 밖 U-자식이 안으로 보내는
>   시그널은 막지 않으므로 ABI ≥ 6에서도 동작한다. 종료·정리가 세션 수명 내내 필요하므로 **상주하는 특권
>   프로세스**가 있어야 한다. 브로커는 entrypoint의 root 단계에 얹히므로 그 단계의 하드닝(#233,
>   §1)을 전제로 한다.
>   - 게이트웨이가 U 소유 상태를 **읽어야** 하는 경로(`/files` 라우트, transcript rehydrate, rmtree)는
>     브로커가 U로서 대행하거나, 의도적으로 설계한 group 권한 체계로 연다. `CAP_CHOWN`은 소유권을 게이트웨이
>     uid로 되돌려야 할 때만 필요하다 — 대행 방식은 권한이 작은 대신 경로마다 브로커 호출이 늘고, group 방식은
>     단순하지만 group 경계를 정확히 지켜야 한다.
>   - 대안(비권장): 브로커가 `CAP_KILL`/`DAC_OVERRIDE`/`FOWNER`/`CHOWN`을 직접 쥐고 종료·정리하는 방식.
>     구현은 단순하지만, 상주 프로세스의 `DAC_OVERRIDE`는 파일 접근에 관해 root에 가까운 권한이라 브로커가
>     뚫리면 피해 범위가 크다.
> - **래퍼 바이너리에만 file capability — spawn 단계만 커버.** `cli_path` 래퍼에 `cap_setuid,cap_setgid+ep`를
>   파일 cap으로 주면 게이트웨이는 cap 0으로 두고 래퍼만 uid 전환 + Landlock + exec을 할 수 있다. 하지만
>   래퍼는 1회성 setuid→exec이라 **exec 뒤에 남는 특권이 없다** — 자식 종료·소유권 복구는 여전히 브로커가
>   필요하므로(그래서 `cap_kill`은 래퍼에 줘도 소용없다) 브로커를 대체하지 못하고, 브로커의 spawn 경로를
>   대신하는 정도다. 또 file cap은 `no_new_privs`가 서면 무시되므로 NNP 전에 전환을 끝내야 한다(아래).
>
> **uid 전환·cap drop 순서(래퍼/브로커 공통).** `no_new_privs` 설정 → Landlock 룰셋 적용(`restrict_self`는
> NNP를 요구) → `setgroups`(보조 그룹 초기화 — 안 하면 브로커의 root 그룹 목록이 남는다) → `setgid` →
> `setuid`로 사용자 uid 전환 → **전환 후 모든 cap 집합을 0으로** 비우고 exec.
> 검증 결과 NNP가 선 뒤 전환 후 cap을 비우면 `setuid(0)`·setuid-root·file-cap 복사본 모두 euid/CapEff를
> 얻지 못한다. 즉 **bounding set을 미리 깎을 필요가 없다**(그건 `CAP_SETPCAP`를 추가로 요구하고, 전환 전
> permitted에서 SETUID/SETGID를 빼면 전환 자체가 EPERM으로 실패한다).
>
> **자식 종료(#147).** uid 1000(CAP_KILL 없음)은 uid U 자식에 시그널을 못 보낸다. SDK `close()`는
> `ProcessLookupError`만 무시하므로(`subprocess_cli.py`) teardown이 `PermissionError`로 깨지고, 멈춘 도구·
> 긴 Bash가 안 죽어 프로세스가 샌다(#147의 SIGTERM→SIGKILL 에스컬레이션도 무력화). → 종료를 **상주
> 브로커에 위임**하고, 브로커는 U로 전환한 단명 자식으로 시그널을 보낸다(위; 1회성 file-cap 래퍼로는 안 된다).
>
> **userland은 passwd 항목을 요구한다.** 커널은 숫자 uid로의 `setuid`에 `/etc/passwd` 레코드를 요구하지
> 않지만, `whoami`·python `getpass.getuser()`·node `os.userInfo()`는 레코드가 없으면 실패한다(CLI 본체는
> try/catch로 버티지만 모델이 쓴 코드·쉘 도구가 깨진다). uid 대역에 대한 passwd/group 항목을 이미지 빌드
> 때 또는 브로커가 만들거나 `nss_wrapper`를 쓰고, allowlist에 `USER`/`LOGNAME`을 포함한다(§6.4).

> **중간 선택지 — Landlock 단독(uid 전환 없음).** cap이 아예 없어도(①의 특권 spawner·종료 문제가 통째로
> 사라진다) Landlock은 open() 계층에서 파일 읽기를 가두고, **Landlock이 있는 모든 커널(5.13+)**에서
> 같은-uid 프로세스의 `/proc/<pid>/environ` 읽기(F2)까지 막는다(Landlock의 ptrace 제약은 5.13부터 존재.
> 문서의 커널 하한 5.19는 이미 이를 포함한다). 단 **남는 구멍은 교차 세션 시그널**이다: Landlock-only에선
> 모든 세션이 uid 1000이라 서로에게·게이트웨이 프로세스에 시그널을 보낼 수 있다(시그널 scoping은 6.12+에서
> `LANDLOCK_SCOPE_SIGNAL` 플래그를 세울 때만 닫힌다; abstract unix socket scope도 6.12+). 사용자 간 파일
> 격리(DAC 소유권)도 주지 못해 완전한 ①의 대체는 아니지만, cap 변경이 막힌 prod에선 F1/F2를 당장 닫는
> 현실적 1차 방어가 된다.

> **무특권 즉시 완화 — 게이트웨이 non-dumpable (#232).** 위 메커니즘 전부와 독립적으로, FastAPI
> lifespan의 **첫 단계**에서 `prctl(PR_SET_DUMPABLE, 0)`를 건다(기본 on; `GATEWAY_NON_DUMPABLE=false|0|no|off`로
> 끔, 빈 값은 on 유지). 세션 CLI 자식의 env에서는 `child_env_mask()`가 `ADMIN_API_KEY`·`API_KEY`와
> `SYSINFO_CHILD_ENV_MASK` 이름을 덮고(`SYSINFO_REDACTION`이 켜졌을 때만), `_sdk_env`/`_isolation_vars`가
> `OPENAI_API_KEY`(api-key 인증이면 `CLAUDE_CODE_OAUTH_TOKEN`도)를 뺀다 — 그 값들은 게이트웨이 environ에만
> 남는다. 이 설정은 그 environ을 **`/proc/<게이트웨이>/environ`으로 읽어 정리를 우회하는 경로를 닫고**,
> 게이트웨이의 메모리·fd를 같은-uid 세션으로부터 보호한다 — cap·컨테이너 변경 없이. 단 모든 spawn 경로가
> 아직 이 정리를 거치지는 않는다(#230 후속). 보호 대상은 lifespan을 실행하는 프로세스이고, 그것이 PID 1인
> 것은 기본 단일 프로세스 CMD일 때뿐이다(`--workers`/`--reload` 감독 프로세스는 아님). 한계: 상속된 비밀은
> **모든 CLI 자식의 environ에서 여전히 읽히고**(exec된 자식은 다시 dumpable), 호출 전에 열린 fd는 회수하지
> 못하며, 디버거 부착은 `CAP_SYS_PTRACE`가 필요해진다. 그래서 ①/Landlock 전의 **스톱갭**이다.

## 4. 메커니즘 선택: 역량 프로브

`scripts/isolation_probe.py`를 **prod 컨테이너 안에서, 세션과 같은 app 유저로** 돌린다. 표준
라이브러리만 쓰고 읽기 전용이며(네임스페이스/`mount`/`setuid` 테스트는 전부 fork된 자식에서만 수행
후 종료), 커널 버전·게이트웨이 프로세스(기본 PID 1, init shim이면 그 자식)의 uid/gid·cap·seccomp·AppArmor
라벨·`/proc` hidepid·게이트웨이 dumpable 여부·그 `environ` 가독성(F2 재현)·Landlock ABI·`bwrap`/`socat`
존재·unshare/mount 가능성·공유 자산과 게이트웨이 코드(`/app`)에 다른 uid가 닿는지/쓸 수 있는지를 찍고
판정을 낸다. 판정은 **게이트웨이의 bounding cap**과 공유 자산
도달성을 근거로 삼고, 프로브 자신(`docker exec`)의 cap은 참고용으로만 찍는다.

```bash
docker compose cp scripts/isolation_probe.py gateway:/tmp/isolation_probe.py
docker compose exec -u app gateway python3 -I -S /tmp/isolation_probe.py
```

> `docker compose exec`는 USER 지시자가 없어 기본 root로 붙는다. 세션 맥락을 반영하려면 **`-u app`**가
> 필수다 — root로 돌리면 프로브는 `NONE` 판정으로 멈춘다. 또한 prod가 `APP_GID`를 1000이 아닌 값으로
> 띄웠다면 `-u <uid>:<gid>`로 **gid까지 세션과 맞춰야** 한다(`/proc/<pid>/environ` 접근은 gid도 보므로,
> gid가 어긋나면 F2가 "이미 격리됨"으로 잘못 보고된다 — 프로브가 게이트웨이와 자신의 uid:gid가 다르면
> `!! creds` 줄로 올바른 명령을 찍고, hidepid로 게이트웨이가 안 보이면 결과가 세션 시야가 아니라고
> 경고하며, 게이트웨이 후보 프로세스가 여럿이면 F2 판정을 보류한다). 개발 머신(WSL2)에서 돌린 값은
> 스모크 테스트일 뿐 prod가 아니다.

판정 규칙(프로브가 찍는 라벨):

- **NONE** — 프로브가 root로 돌았다(판정 불가).
- **BEST PATH: ① uid-per-user + Landlock** — Landlock ABI ≥ 2 이고 **게이트웨이의 bounding set에
  SETUID+SETGID** 존재(= `grantable`). 프로브는 브로커를 fork할 root로 시작하는 entrypoint를 가정한다
  (검사하지 않음). 특권 위치·cap은 §3. 공유 자산 중 다른 uid가 못 닿는 항목이 있으면 `!!` 줄로 경고(§5).
- **LANDLOCK-ONLY** — Landlock은 쓸 수 있으나 bounding set에 SETUID/SETGID가 없어 uid 전환이 막힘(§3 중간
  선택지). Docker 기본 bounding set에 SETUID/SETGID가 있고 아무도 빼지 않으므로, 운영자가 `--cap-drop`으로
  뺀 경우가 아니면 `grantable`은 참이라 드물다.
- **BWRAP** — 위가 안 되고 userns+pidns+fresh procfs가 가능 → **② bwrap/nsjail**(seccomp·AppArmor를 열어야
  할 수 있음; `bwrap` 미설치면 함께 안내). userns 안의 새 procfs mount 가능 여부는 커널·런타임에 따라
  다르므로 프로브의 실제 시험 결과를 따른다.
- **NO PATH** — 무엇을 열어야 하는지 우선순위로 나열(Landlock은 errno별로: EPERM=seccomp에 `landlock_*`
  허용, ENOSYS=커널 미지원(<5.13) **또는** seccomp 프로파일이 syscall보다 오래됨(Docker ≤20.10.17) — LSM
  목록이 읽히면 프로브가 둘을 구분하지만 컨테이너 안에선 보통 못 읽으므로 `docker version`으로 확인,
  EOPNOTSUPP=`lsm=`에 landlock 추가, ABI 1=≥5.19 필요, `KILLED (SIGSYS)`=seccomp가 syscall을 죽임 → 역시
  `landlock_*` 허용; Landlock이 seccomp 때문에만 실패했다면 BWRAP 판정에도 "허용하면 ①이 가능"이 붙는다).
  fork가 pids/nproc 한도로 실패한 테스트는 DENIED가 아니라 `UNKNOWN`으로 찍히고 `missing:`에 `?`로 표시된다.
- NONE이 아닌 판정 뒤에는 **F2 문단**(uid 분리/새 PID ns/Landlock 5.13+, 그리고 `PR_SET_DUMPABLE` 무특권
  스톱갭과 그 한계)을 덧붙이고, Landlock을 쓸 수 있으면 **INTERIM**(Landlock-only 안내 + 그 한계: 같은-uid
  시그널·DAC 미격리)을, 바이너리가 없거나 userns가 거부될 때만 **CLI OS 샌드박스 한 줄**을 더한다(NONE
  판정에는 덧붙이는 줄이 없다) — 바이너리가 없으면 Bash는 샌드박스 없이 돌고, 있는데 userns가 거부되면
  **모든 Bash 호출이 실패**한다(게이트웨이 기본값 `allowUnsandboxedCommands=false`라 비샌드박스 폴백이 없다).

## 5. 반드시 지킬 제약: plugin/skill 공유 유지

**plugin 등으로 설치된 스킬은 모든 사용자에게 계속 공유되어야 한다.** 이 격리가 그걸 깨면 안 된다.

- `~/.claude/{plugins,skills,agents,commands,output-styles}` + user-scope `CLAUDE.md`은 기동 때
  `install_plugins.py`가 **한 곳에 한 번만** 설치한다(지금처럼). 이들은 **읽기 전용 공유 트리**로
  두고, **모든 세션 uid/감옥에 ro 접근을 허용**한다.
- ⚠️ **공유 목록에서 빠지기 쉬운 경로들**(빠지면 플러그인 스킬·훅이 모든 세션에서 사라진다 — 검증됨):
  - **플러그인 clone 루트** `$CLAUDE_PLUGIN_CLONE_ROOT`(미설정 시 `~/.claude/plugin-marketplaces/<repo>-<hash>`).
    CLI는 플러그인 스킬의 base dir과 훅의 `CLAUDE_PLUGIN_ROOT`를 **`plugins/cache`가 아니라 이 clone**에서
    해석한다. `plugins/`만 공유하고 clone을 빼면 `Skill` 호출이 "Unknown skill"로 떨어지고 훅이 안 돈다.
  - `plugins/known_marketplaces.json`(`installLocation`·`source.path`), `plugins/installed_plugins.json`
    (`installPath`), `settings.json`의 `extraKnownMarketplaces.*.source.path`에 적힌 **절대 경로 전부**와,
    공유 자산이 **symlink면 그 대상 경로**까지 ro로 닿아야 한다.
- **쓰기 상태만 분리**한다: `projects/`, `plans/`, SDK가 쓰는 cache/history, 워크스페이스, 그리고 아래의
  `plugins/data`.
- ⚠️ **`plugins/` 전체를 읽기 전용으로 두면 모든 플러그인 훅이 깨진다.** CLI 2.1.283은 훅마다
  `<plugins 루트>/data/<plugin-id>`를 `mkdir`하고(EEXIST 외 모든 에러를 rethrow) `CLAUDE_PLUGIN_DATA`로
  넘긴다. 루트는 `$CLAUDE_CODE_PLUGIN_CACHE_DIR`(미설정 시 `<cfg>/plugins`). 읽기 전용이면 훅이
  `EACCES … mkdir … plugins/data`로 실패하는데 **턴은 success로 보고**돼 조용히 깨진다(§5 e2e의 "스킬 인식"
  체크로는 안 잡힘). → `plugins/data`를 **쓰기 가능한 세션 상태**로 분리하거나, `CLAUDE_CODE_PLUGIN_CACHE_DIR`
  (쓰기용)과 `CLAUDE_CODE_PLUGIN_SEED_DIR`(읽기 전용 seed, 경로 구분자 목록)로 공유 seed와 세션별 쓰기를
  나눈다. 세션은 `plugins/cache/.../<ver>/.in_use/<pid>` 마커도 쓴다.
- ⚠️ **`~/.claude/settings.json`은 세션별로 나누지 말 것 — 공유 *정책*이다.** admin이 관리하는 env 블록이
  여기 쓰이고(`claude_settings_env.py`, `user` setting source로 모든 세션에 적용), `claude plugin install
  --scope user`가 **플러그인 활성화(`enabledPlugins`)를 이 파일에 기록**한다. 세션마다 새 `settings.json`을
  주면 플러그인은 설치돼 있어도 **활성화가 안 돼 스킬이 사라지고**(위 필수 제약 위반) admin env 블록도
  조용히 안 먹는다. 이 파일은 공유 ro(또는 공유 + 세션 오버레이)로 두고, 리팩터 전에 prod
  `~/.claude/settings.json`의 `enabledPlugins`를 먼저 확인한다.
- ⚠️ **게이트웨이 자신이 이 공유 파일을 0600으로 되쓴다.** `claude_settings_env.py`의 `_write_settings`는
  기동 때와 **모든 admin 저장/clear/reproject**마다 `tempfile.mkstemp`(0600) + `os.replace`로 파일을
  새로 만든다 — chmod 0644를 되돌리고, 공유본을 가리키던 symlink를 **사설 일반 파일로 교체**해 공유본을
  stale로 만든다. 세션 uid가 못 읽으면 `claude plugin list`는 플러그인을 `disabled`로 보이고 **에러 없이
  exit 0** 한다. 따라서 세션별 HOME 리팩터에서는 이 writer가 **공유 경로에 올바른 모드/소유권으로** 쓰도록
  (또는 env 블록을 settings.json과 분리하도록) 함께 고쳐야 한다. admin env 블록은 CLI가 exec 뒤 다시
  적용하므로 §6.4 allowlist로는 걸러지지 않는다 — 공유 정책 채널로 다뤄야 한다.
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
- ⚠️ **config dir를 옮기면 게이트웨이의 여러 소비자가 같이 따라가야 한다**(검증됨 — 안 고치면 조용히 깨짐):
  - **transcript 경로가 import 시점에 고정**돼 있다: `session_manager.py`의
    `_PROJECTS_ROOT = Path.home()/".claude"/"projects"`. CLI가 세션 config dir에 transcript를 쓰면
    `_session_jsonl_exists()`가 항상 False가 되고, client 재생성 경로가 `--resume` 대신 `--session-id`를
    넘겨 CLI가 "Session ID … is already in use"로 exit 1 → 그 세션의 **이후 모든 턴 실패**. 재기동 후
    `_try_rehydrate_from_jsonl`도 못 찾아 `previous_response_id` 연속이 404. `_cleanup_sdk_transcript`는
    엉뚱한 경로를 지워(에러 억제) stateless transcript가 디스크에 남는다.
  - **guard/워크스페이스 샌드박스의 루트도 `$HOME/.claude`에서만** 뽑는다(`workspace_sandbox.py`의
    `_claude_home`, 두 훅 모두 게이트웨이 프로세스 안에서 실행). 자식에만 준 HOME/`CLAUDE_CONFIG_DIR`은
    이들에게 닿지 않으므로, 샌드박스 OFF면 다른 세션 transcript가 다시 열리고, ON이면 세션 자신의 spill된
    도구 결과·옮겨진 공유 스킬 읽기가 거부된다(`plugin_resource_roots()` → `[]`, `--add-dir` 소스가 빔).
    → 리팩터에서 **훅 루트와 `--add-dir`를 자식의 config dir에서 재유도**해야 한다(§7의 guard 격하보다 선행).
- **검증 항목**: CLI가 세션별 config dir에서 공유 스킬/플러그인을 **여전히 인식**하는지 e2e로 확인.
  프로브의 "SHARED ASSET LAYOUT" 섹션이 자산마다 소유권·권한과 함께 **다른 uid가 닿는지**(막는 상위
  디렉터리)와 **안에서 못 쓰는 항목 수**(o+r 없는 파일, o+x 없는 실행 파일·디렉터리)를 찍으니 리팩터
  범위 산정에 쓴다.

## 6. spawn 설계 (메커니즘 공통 골격)

`CLAUDE_CLI_PATH` 래퍼가 하는 일:

1. 사용자 → **숫자 uid** 결정적 매핑. 키는 **`WorkspaceManager`가 쓰는 resolved key와 동일**해야
   한다(한 uid ↔ 한 워크스페이스; `@` 처리·legacy localpart 플래그가 어긋나면 서로 다른 uid가 같은
   워크스페이스를 쓰거나 그 반대가 된다). 충돌 금지(레지스트리로 추적하고 충돌 시 거부). `user`가 없는
   트래픽(`/v1/agents/messages`의 stateless 런, `/v1/responses`의 `user=None`)은 **각자 단기 uid**를
   받아야 한다. ⚠️ 커널은 숫자 uid `setuid`에 `/etc/passwd` 레코드를 요구하지 않지만 **userland은
   요구한다**(§3 — passwd/group 항목 또는 `nss_wrapper`, allowlist에 `USER`/`LOGNAME`).
2. **세션별 HOME/`CLAUDE_CONFIG_DIR`** 준비: 쓰기 가능한 per-session `.claude`(projects/plans/
   cache/history/`plugins/data`) + 공유 자산(§5의 clone 루트·절대 경로 포함, `settings.json`은 공유 정책)을
   ro로 연결. §5의 config-dir 소비자(transcript 경로·guard 루트·`--add-dir`)도 함께 이전.
3. 격리 적용:
   - **①**: 순서·cap drop·특권 위치는 §3. 룰셋 ro: 시스템 경로·CLI 설치·공유 자산·**게이트웨이 코드 `/app`**(§1 — 세션이
     코드를 덮어쓰지 못하게) / rw: 워크스페이스·per-session `.claude`. 세션 uid는 **`app`(1000)과 달라야**
     `/app`·공유 자산이 DAC로도 쓰기 불가가 된다. Landlock-only(uid 전환 없이 모두 1000)면 DAC로는 못 막으니
     `/app`을 반드시 ro 규칙에 넣는다.
     ⚠️ **rw에 세션 전용 TMPDIR과 `/dev/null`을 반드시 포함**한다: CLI는 기동 때
     `${CLAUDE_CODE_TMPDIR||TMPDIR||/tmp}/claude-<uid>`를 `mkdir`하고(없으면 `EACCES`로 exit 1), Bash 도구는
     모든 명령을 `< /dev/null`로 감싼다(`/dev/null` 쓰기 권한 없으면 매 호출 "permission denied"). 공유 `/tmp`를
     rw로 열면 Landlock-only(모두 uid 1000)에선 세션 간 통로가 되므로, **세션별 `TMPDIR`/`CLAUDE_CODE_TMPDIR`**
     (세션 `.claude` 하위)와 `/dev/null` 파일 규칙으로 준다.
   - **②**: `bwrap --unshare-pid --die-with-parent --ro-bind <공유 자산> … --bind <워크스페이스> …`로 깨끗한
     env와 함께 실제 `claude` exec. ⚠️ **`--die-with-parent` 필수**: 없으면 teardown 시그널이 바깥 bwrap에만
     닿고 안쪽 CLI·자손은 계속 돌아, `_teardown`이 **살아있는 에이전트의 cwd/transcript를 지우고**(규칙 위반)
     고아가 PID 1(기본 구성은 uvicorn, compose에 `init:` 없음)로 재부모화돼 안 거둬진다(#147 재발). (그래도 SIGTERM은
     bwrap에서 멈추고 CLI엔 SIGKILL이 가므로, SDK의 graceful 세션 flush는 잃는다.) ⚠️ **`--unshare-net`은
     쓰지 말 것**: 빈 net ns는 loopback만 남아 CLI가 `ANTHROPIC_BASE_URL`·게이트웨이 loopback MCP relay에
     닿지 못해 **첫 턴부터 실패**한다(그런데도 relay `reachable()` self-probe는 통과). egress 통제는 별도
     레이어로(§8). ②는 userns+mount 외에 **AppArmor `docker-default`(deny mount)** 때문에
     `apparmor=unconfined`/커스텀 프로파일이 추가로 필요할 수 있다(프로브가 라벨을 찍는다).
4. **env는 allowlist로 구성**하되, 고정 목록을 손으로 들기보다 **게이트웨이가 각 변수를 세팅하는 지점에서
   생성**한다(SDK가 `os.environ`과 `options.env`를 한 env로 합치므로, 손 목록은 의도한 값과 샌 값을 구분
   못 한다). CLI가 필요로 하는 것(`ANTHROPIC_*`, `PATH`/`HOME`/로케일, `USER`/`LOGNAME`)만이 아니라
   **게이트웨이가 자식에 심는 제어 변수까지 명시로 포함**한다. `CLAUDE_CODE_HARBOR_KITE`는 **빈 값·미설정이
   곧 ON**이라(`src/constants.py`가 `=0`을 박아 두어야 꺼진다) allowlist에서 빠지면 교차 세션 메시징이 다시
   열린다 — "자연히 사라진다"가 아니라 **그 반대다.** 반드시 함께 넘길 것: `CLAUDE_CODE_HARBOR_KITE=0`,
   `MCP_TOOL_TIMEOUT`, `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS`, SDK가 주는 `CLAUDE_CODE_ENTRYPOINT`/
   `CLAUDE_AGENT_SDK_VERSION`, loopback `NO_PROXY` 항목, 그리고 빠지기 쉬운 것들: `BASH_MAX_TIMEOUT_MS`
   (게이트웨이가 자기 copy로 `TOOL_STALL_TIMEOUT`·기동 순서 검사를 계산하므로 빠지면 자식이 600000ms로
   되돌아가 검사가 실제 자식을 설명 못 함), admin이 덮는 `CLAUDE_CODE_AUTO_COMPACT_WINDOW`,
   `METADATA_ENV_ALLOWLIST` 키(운영자 지정 — `THREAD_ID`·`ORCHESTRATOR_URL` 등, 고정 목록으로 못 담음),
   cli auth 모드의 `CLAUDE_CODE_OAUTH_TOKEN`, prod가 런타임 프록시를 쓰면 `HTTP(S)_PROXY`.
   ⚠️ **`ANTHROPIC_AUTH_TOKEN`은 상류 공유 자격증명인데 `ANTHROPIC_*`에 묶여 그대로 자식에 간다**(§1).
   설계 제약: allowlist는 게이트웨이 전용 비밀을 넘기지 않아야 하지만, 이 토큰은 CLI 인증에 필요해 그 규칙만으로는
   빠지지 않는다 — 자식은 이 토큰으로 게이트웨이 회계 밖에서 상류를 직접 호출할 수 있다. 완화책을 명시해야
   한다(사용자별 토큰, 토큰을 주입하는 프록시 뒤에 두기, 또는 CLI의
   `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB`=on — 단 이건 `bubblewrap`을 요구하고 켜면 권한 모드를 `default`로
   강제한다). 단순 allowlist로는 못 지운다.
5. **argv 노출을 설계 단계에서 닫는다.** `/proc/<pid>/cmdline`은 누구나 읽어 ①에서도 교차 테넌트로 샌다
   (검증: Landlock 도메인의 다른 uid가 피해자 cmdline을 읽음). 거기엔 `--mcp-config` JSON(세션의
   `X-MCP-Context` = `{{user}}`와, `MCP_FORWARD_CONTEXT` 설정 시 `{{header:…}}` 자격증명, relay_id)과
   `--system-prompt`의 caller 지시문이 실린다. `hidepid=2` 리마운트는 `CAP_SYS_ADMIN`을 요구해(기본 cap에서
   거부됨) 권장 경로가 아니다. 대신 SDK가 이미 받는 **파일 경로로 MCP 설정**(`mcp_servers`에 파일 path →
   `subprocess_cli.py`)과 **`--system-prompt-file`**(SDK `SystemPromptFile`)을 써서 argv에서 빼면 무특권으로
   해결된다. `hidepid`는 가능하면 추가 방어로만.

## 7. 단계별 계획

1. **(이 PR)** 역량 프로브 + 본 설계 문서. prod에서 프로브 실행 → 메커니즘 확정.
2. **세션별 HOME 리팩터** (공유 ro 자산 보존). 가장 공유-스킬 민감한 부분 — 먼저, 독립적으로. §5의
   config-dir 소비자(transcript 경로·guard 루트·`--add-dir`)와 `settings.json`/`plugins/data` writer를 함께
   이전·수정.
3. **특권 spawner + `CLAUDE_CLI_PATH` 래퍼** (① uid+Landlock 또는 ② bwrap). 특권 위치는 §3(SETUID/SETGID만
   쥔 상주 브로커).
   신원 전달 채널과 "세션 아닌 spawn" 처리(§2)도 여기서.
4. **워크스페이스 uid 소유권** + `/app` 쓰기 차단 + `/proc` 하드닝. ⚠️ 세션이 만든 파일이 uid U 소유가 되면
   **uid 1000인 게이트웨이가 못 지운다**: `workspace_manager.cleanup_temp_workspace`(rmtree)와
   `agent_messages._cleanup_sdk_transcript`가 조용히 실패해 stateless 정리 보장이 깨지고, 업로드·`/files`
   라우트는 U 소유 디렉터리에서 EACCES가 난다. 그래서 정리와 U 소유 상태 읽기는 §3대로 브로커가 U로 전환한
   단명 자식으로 대행하거나 의도한 group 권한 체계로 연다(`CAP_CHOWN`은 소유권 반환이 필요할 때만).
   무특권 스톱갭 `PR_SET_DUMPABLE`(#232)과 entrypoint 하드닝(#233)은 이 단계와
   **독립적으로 먼저** 넣을 수 있다.
5. **검증**: 2-사용자 파일 격리 테스트, 세션 uid가 게이트웨이의 environ **그리고 다른 세션 CLI 자식의
   environ**을 못 읽는지 확인(#232만으로는 앞의 것만 통과한다), **스킬 공유 유지** e2e, 플러그인
   훅 실행(= `plugins/data` 쓰기) 확인, 기존 게이트웨이 스위트.
6. 중복이 된 임시 방어를 안전한 선에서 정리(`make_claude_home_guard_hook`은 방어심층으로 격하 — 단 §5의
   훅 루트 재유도 이후).

## 8. 열린 질문 (prod 사실에 의존 — 대부분 프로브가 답한다)

- 커널 버전 / Landlock ABI, **게이트웨이의 부여 가능한 cap**, seccomp 프로파일, AppArmor 라벨, `bwrap`/`socat`
  설치 여부, `/home/app` 모드 → **프로브가 답한다.**
- `docker version`(ENOSYS가 커널인지 낡은 seccomp인지 — §4), `APP_GID`(1000이 아니면 프로브를 `-u
  <uid>:<gid>`로), 비공개 `GATEWAY_BUILD_INSTALL_SCRIPT`가 `bwrap`/`socat`을 넣는지.
- 호스트 전역 `fs.protected_hardlinks`/`protected_symlinks` 값 → 프로브의 `/proc HARDENING` 행이 찍는다
  (entrypoint 하드닝(#233) 후에는 그 단계가 이 값에 의존하지 않지만 다른 root 경로 점검에 참고).
  게이트웨이 코드(`/app`)가 세션 uid로 쓰기 가능한지는 SHARED ASSET LAYOUT의 `gateway code` 행(바로 아래
  site-packages 행 포함)이 찍는다.
- `/proc` `hidepid` 리마운트가 이 컨테이너에서 가능한가(CAP_SYS_ADMIN 필요 — 권장 경로는 아님, §6.5).
- uid 할당 대역과 수명(생성·회수), 동시 사용자 상한. 키는 `WorkspaceManager` resolved key와 일치(§6.1).
- 격리 단위: **사용자(테넌트)별**이 기본으로 충분(같은 사용자의 다른 세션은 기밀성 위협이 아님).
  단 `user`가 없는 stateless/익명 런은 각자 단기 uid(§6.1).
- **resume와의 상호작용**: `/v1/responses`의 세션 재개가 턴을 넘어 **같은 uid/HOME로** 매핑되어야
  한다(세션 매니저 ↔ uid 매핑의 안정성; transcript 경로 이전과 함께 — §5).
- `BACKENDS` 값: codex/opencode가 켜져 있으면 그 spawn 경로는 래퍼 밖이다(§2).
- prod가 init shim(compose `init: true`/`--init`)을 쓰는가 — 쓰면 PID 1은 docker-init이고 F2 대상과 브로커
  배치 전제가 게이트웨이 자식 기준으로 바뀐다(§1·§4).
- prod가 compose `user:`로 비root 시작하는가 — 그러면 브로커를 fork할 root 단계가 없다(프로브는 이를 검사하지
  않고 가정만 한다, §4).
- 네트워크 egress 통제(①은 egress를 다루지 않음 — 별도 레이어 / #173의 zero-egress 게이트).
