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
- **PID 1 = 게이트웨이(기본 구성).** entrypoint(`docker/entrypoint.py`)가 `os.execvp`로 CMD를 exec 하므로
  PID 1은 uvicorn 게이트웨이 프로세스다. `/proc/1/environ`은 게이트웨이의 **마스킹되지 않은 전체 env**다. 단
  compose `init: true`/`--init`(#147 고아 회수의 그럴듯한 해법)을 쓰면 PID 1은 docker-init이고 게이트웨이는 그
  자식이다 — 그때 F2의 대상은 PID 1이 아니라 **게이트웨이 자식 프로세스**다(이하 "PID 1"은 이 경우 게이트웨이
  프로세스로 읽는다. 프로브는 init shim을 감지해 PID 1의 자식을 따라간다). ⚠️ **docker-init이 root인지는 시작
  방식에 달렸다**: 기본(root로 시작)이면 docker-init은 root지만, compose `user:`로 **비root 시작**하면
  docker-init도 세션 uid로 돌며 **그 uid의 전체 env를 쥔다** — 바로 이 때문에 init shim 아래에서도 F2가
  (게이트웨이 자식이 아니라 그 비root docker-init에서) 재현될 수 있다(§8의 비root 시작 질문).
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
읽는 것뿐 아니라 **코드 쓰기 경로**도 닫아야 한다(§3·§5·§6: 같은-uid(Landlock 단독) 옵션이면 `/app`을
**룰셋에서 통째로 빼** 규칙 부재로 읽기·쓰기를 모두 거부하고(ro 규칙에 넣으면 그 아래 `/app/data`의
자격증명 오버레이까지 읽기 허용이 된다 — §6.3·E), uid 분리면 세션 uid를 `app`과 다르게 두고 DAC로 `/app`
쓰기를 막는다; 이미지가 애초에 쓰기 상태만 app 소유로 넘기도록 바꾸는 선택지도 있다). 같은 맥락에서 root 단계(entrypoint의 소유권 복구)는 #233(**별도 PR, 아직 미병합** — 이 브랜치의
`docker/entrypoint.py`는 아직 평이한 `os.chown`이다)에서
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

주입 지점: SDK는 `cli_path`로 CLI를 띄운다(게이트웨이는 `_get_cli_path()`로 그 값을 정한다 —
`src/backends/claude/client.py`, `slash_commands.py`). 따라서 **`cli_path`를 격리 런처로 고정**하고 그 안에서
실제 `claude`를 선택한 격리 아래 re-exec 하면, SDK를 패치하지 않고 **모든 Claude 백엔드 세션**에 격리를 건다
(이 런처를 운영자 노브 `CLAUDE_CLI_PATH`에 **얹으면 안 되는** 이유는 아래 ⚠️ — item G).

> ⚠️ **이 래퍼는 Claude 백엔드에만 적용된다.** `_get_cli_path()`를 타지 않는 spawn 경로는 격리 밖이다:
> opt-in app-server 어댑터(`src/backends/appserver/`)는 `codex app-server`를 **자기 프로세스로** 띄운다
> (`transport.py`의 `asyncio.create_subprocess_exec`, `cli_path` 래퍼를 타지 않음). 이 어댑터는 env를 그대로
> 넘기지는 **않는다**: `build_isolated_env`(`isolation.py`)가 `ANTHROPIC_AUTH_TOKEN`/`ANTHROPIC_API_KEY`를
> 빼고(`ISOLATION_ENV_REMOVE` + auth `get_isolation_vars`) **사용자별 `CODEX_HOME`**(`resolve_codex_home`)을
> 심으며, transport는 `os.environ.copy()`에 그 override를 merge하고 `env_remove` 키를 pop해 띄운다. 다만
> **`child_env_mask()`는 적용하지 않아** `ADMIN_API_KEY`/`API_KEY`는 **그대로 자식에 샌다**(frozen
> `codex`/`opencode` 백엔드도 동일). Claude 백엔드가 쓰는 Landlock/uid/워크스페이스 격리도 전혀 없으므로,
> `BACKENDS=codex`(+`CODEX_BACKEND=appserver`) 등을 켜면 그 세션은 **F1/F2가 다시 열린다.** 게다가 app-server
> **종료**는 게이트웨이 프로세스에서 `os.killpg`로 자식 프로세스 그룹에 시그널을 보내는데(`transport.py`의
> `_signal_group`; 그룹은 `start_new_session=True`로 만든다), uid 분리 아래선 이 시그널이 `EPERM`으로 **debug
> 로그만 남기고 삼켜지고**(유일한 escalation인 뒤이은 `SIGKILL` killpg도 같은 uid 벽에 막힌다) 그룹이 조용히
> 샌다(codex 판 #147). 이 문서가 #173("two-user filesystem isolation" 게이트)와 같은 범위라고 적지만, Claude용
> 래퍼만으로는 #173의 codex 경로를 커버하지 못한다 — **#173 cutover는 app-server의 spawn *과 종료* 경로가
> 모두 같은(또는 동등한) uid/파일 격리 + 특권 teardown을 타기 전에는 완료로 쳐선 안 된다.** prod의 `BACKENDS`
> 값은 사용자 확인 필요.

> ⚠️ `_get_cli_path()`는 지금 **fail-open**이고, 게다가 `CLAUDE_CLI_PATH`는 **이미 운영자 노브**다
> (`src/backends/claude/constants.py`·`.env.example`에 "핀된 SDK를 안 올리고 **더 새 CLI 빌드를 시험**하는 용도"로
> 문서화돼 있다). `_get_cli_path()`의 검사는 `path.is_file() and os.access(path, X_OK)`뿐이라 **래퍼인지 평이한
> CLI인지 구분하지 못한다**: `CLAUDE_CLI_PATH`를 실제 `claude`로 가리키면(또는 prod가 이미 CLI 시험용으로 그렇게
> 쓰고 있으면) 격리가 **깨끗한 기동과 함께 조용히 꺼진다**. 없거나 실행 불가면 경고만 찍고 번들 CLI로 떨어지므로
> (매 세션 빌드마다 검사) 재빌드로 래퍼가 사라져도(+x 누락·경로 오류·볼륨 미마운트) **모든 새 세션이 격리 없이**
> 돌고 기동은 멀쩡하다. → 격리를 `CLAUDE_CLI_PATH`에 **얹지 말고**, 항상 `cli_path`로 넘기는 **고정·root 소유
> 런처**를 두고 그 런처가 내부에서 실제 CLI(운영자의 `CLAUDE_CLI_PATH` 또는 번들)를 고르게 한다. 그리고 기동
> 때 **설정된 `cli_path`가 바로 그 런처인지**를 경로·소유자·서명 등으로 **양성(positive) 검증**해, 아니면
> `ConfigIssue(error)`/세션 거부로 **fail-closed** 한다 — 부재뿐 아니라 '엉뚱한 바이너리로 대체됨'까지 막아야
> 경계가 된다.
>
> ⚠️ **래퍼는 "어느 사용자인가"를 스스로 알지 못한다.** §6의 첫 단계는 "사용자 → uid 매핑"인데,
> 게이트웨이는 그 신원을 자식에게 신뢰 가능한 형태로 넘기지 않는다(현재는 system prompt 텍스트와
> caller가 준 metadata뿐, cwd가 유일한 힌트). 게다가 세션이 아닌 `cli_path` spawn이 여럿 있다:
> SDK가 매 connect마다 거는 `claude -v` 버전 체크(env·cwd·user 모두 없음), 슬래시 명령 카탈로그/preflight
> (`slash_commands.py`, `child_env_mask()`도 미적용), `/v1/slash-commands`, 기동 로그. 래퍼를 신원 필수로
> **fail-closed** 설계하면 이들이 깨진다 — 슬래시 턴은 500, 스킬 해석 경로(`client.py`가
> `slash_commands.get_available_commands`(**`slash_commands.py`**, `client.py`가 아니다)를 호출)와 명령
> 상세/버전 spawn은 **새 세션마다 실패**한다(skill granular 분기는 try/except로 원요청을 유지하지만, 평이한
> 분기와 `_fetch_command_details`의 SDK spawn은 그대로 터진다). 반대로 세션 아닌 spawn도
> 워크스페이스 project 설정(훅 포함)을 로드하므로, **신원이 없다고 격리 없이 통과시켜선 안 된다.**
> → 래퍼에 **신원 전달 채널**(예: 세션 전용 env/fd로
> uid를 주입)과 "세션 아닌 spawn"의 처리 규칙을 먼저 정의해야 한다.
>
> 대안: SDK엔 이미 CLI를 다른 uid로 띄우는 `ClaudeAgentOptions.user`(`types.py`, `user: str|None`)가 있다
> (`subprocess_cli.py`가 그대로 `anyio.open_process(..., user=self._options.user)`로 넘기고 → `Popen(user=)`에
> 닿는다). 단 이것은 래퍼의 **대체가 아니다**: (0) **가장 결정적** — `Popen(user=)`는 **게이트웨이가 fork한
> 자기 자식 안에서**(exec 직전) `setresuid`를 하므로 **게이트웨이 프로세스 자신이 `CAP_SETUID`를 쥐어야**
> 한다. 이는 §3의 "게이트웨이 본체는 cap 0" 원칙과 정면 충돌하고, 그걸 성립시키려 ambient `CAP_SETUID`를 두면
> 그 cap이 **게이트웨이가 exec하는 모든 자식**에 샌다(§3). (1) 자식 내부에서 뭔가를 실행할 훅이 없어(anyio
> `open_process`에 `preexec_fn` 없음) **Landlock 룰셋·env allowlist는 이 경로로 걸 수 없고**, 부모에서 Landlock을
> 걸면 게이트웨이 자신이 갇힌다. (2) SDK에 **`group` 필드가 아예 없어** `Popen`에 `group=`이 안 가므로 **gid는
> 1000으로 남는다**(그래서 쟁점은 `CAP_SETUID`뿐, `SETGID`는 이 경로에 없다) — 세션들이 gid 1000을 공유해 서로의
> group-readable 파일을 읽고 새 파일도 gid 1000이 돼 **DAC 파일 격리가 약해진다**. (3) 숫자 uid를 문자열로 주면
> `getpwnam` 실패 → 정수로 넣어야 하는데 타입힌트는 `str|None`이다. (덧붙여, 게이트웨이는 **지금 `options.user`를
> 세팅하지도 않는다** — `user`는 system prompt 텍스트로만 흘러간다.) `no_new_privs`·cap drop은 게이트웨이 자신에
> 미리 걸면 이 경로로도 되지만, (0)의 cap 충돌과 Landlock·allowlist·gid 때문에 **결국 `cli_path` 래퍼가 필요**하다.

## 3. 메커니즘 후보

| | ① uid-per-user + Landlock | ② bubblewrap / nsjail |
|---|---|---|
| F2 (`/proc/1/environ`) | uid 분리 → 세션 uid ≠ PID1 uid라 EACCES | 새 PID 네임스페이스 → PID1이 세션 자신 |
| F1 (임의 파일 읽기) | Landlock이 open() 계층에서 강제(경로 생성 방식 무관) | 새 mount 네임스페이스 → 워크스페이스만 bind |
| 사용자 간 파일 | uid 소유권(DAC) | mount 네임스페이스에 애초에 없음 |
| 필요 권한 | **특권 표면이 둘**: spawn용 file-cap 래퍼(`CAP_SETUID`/`SETGID`) + 종료·정리용 **상주 헬퍼**. 헬퍼는 반드시 가둬야 하고(A), teardown엔 미해결 긴장(C). **게이트웨이 본체는 cap 0.** | userns + mount — 기본 Docker seccomp가 `mount`를 `CAP_SYS_ADMIN` 뒤로 막고, AppArmor `docker-default`는 `deny mount`라 **seccomp를 풀어도** `apparmor=unconfined`/커스텀 프로파일이 추가로 필요 |
| 컨테이너 친화성 | 높음(mount-ns·seccomp 안 건드림) | 낮음(바깥 경계를 넓힘) |
| 커널 요구 | Landlock ABI 2(cross-dir rename) ≥ 5.19 | unprivileged userns |

**옵션 (a)에서는 ①을 권장한다.** ②는 컨테이너 바깥 경계(userns+mount+AppArmor)를 넓히는 반면, ①은
그 범위를 **작은 특권 표면**(spawn용 file-cap 래퍼 + teardown용 상주 헬퍼)에 가두고 Landlock이 "코드로 파일 읽기" 우회까지 LSM 레벨에서 닫는다.
②는 ①이 불가능할 때의 폴백이다.

> **⚠️ 특권은 게이트웨이가 아니라 별도 spawner가 쥔다 (가장 중요).** ①에 필요한 cap을 게이트웨이 본체에
> 남기는 방식(`KEEPCAPS`+ambient로 하락 너머 유지)은 **채택하지 않는다.** ambient cap은 `execve`를 넘어
> 살아남으므로, 게이트웨이가 ambient `CAP_SETUID`를 쥐면 **게이트웨이가 exec하는 모든 것**(플러그인 설치,
> `plugin_admin`의 `claude`/`git`, MCP 연결 테스트가 띄우는 admin 지정 명령, app-server, fail-open 시
> 번들 CLI)이 그 cap을 물려받아 임의 uid(0 포함)로 전환할 수 있다. 대신 특권을 별도 프로세스로 격리한다:
> - **(B) spawn은 `cli_path` 래퍼가, 종료·정리는 상주 특권 헬퍼가 쥔다 — 역할이 갈린다.** SDK는 자신이
>   `cli_path`로 띄운 **바로 그 프로세스의 파이프**로만 NDJSON 턴/`control_request`/`initialize`를 주고받는다
>   (`subprocess_cli.py`가 `anyio.open_process(cli_path, stdin=PIPE, stdout=PIPE, …)`). 그래서 브로커가 CLI를
>   **대신 fork**하면 그 CLI는 브로커의 자식·브로커의 파이프를 갖게 돼 게이트웨이 SDK의 매 턴·`claude -v`까지
>   **아무 데도 닿지 않는다** — 즉 **브로커가 spawn을 대행할 수 없다.** spawn은 **SDK가 `cli_path`로 띄우는
>   래퍼**가 맡아 **그 자리에서**(상속한 stdin/stdout 파이프를 그대로 둔 채) uid를 전환하고 실제 CLI를 `exec`한다.
>   래퍼의 `CAP_SETUID`/`SETGID`는 **파일 cap**에서 온다(게이트웨이는 cap 0; 단 file cap이 붙으려면 게이트웨이가
>   래퍼를 **NNP 없이** exec해야 한다 — N). 1회성 래퍼는 `exec` 뒤 특권이 안 남으므로 **자식 종료·턴 후 정리·
>   reaping은 별도의 상주 특권 헬퍼/브로커**가 맡는다(entrypoint가 `drop_privileges` **전에** root 단계에서 띄워
>   상주; 그 단계 하드닝(#233, §1)을 전제). 단 teardown은 깨끗이 위임되지 않는다(C).
> - **(A) 그 상주 헬퍼의 권한을 가둬야 한다 — "`CAP_SETUID`만 = 최소 권한"은 거짓.** `CAP_SETUID` 하나로도
>   `setresuid(0,0,0)`로 **root가 되고**(root-equivalent), uid 0 프로세스는 아무 `execve`에서나 Docker 기본 cap
>   전부를 되찾고 root 소유 파일(site-packages의 `.pth`, `docker-entrypoint.py` 등)을 써 **다음 `restart`에 완전
>   root**가 된다 — 이 문서가 `DAC_OVERRIDE`에 대해 거부한(아래) 바로 그 near-root 피해범위다. 그러므로 이
>   **상주 헬퍼를 "최소 권한"이라 불러선 안 된다.** 반드시 잠가야 한다 — **가능하면 비root**, 불가피하게
>   `CAP_SETUID`를 쥔다면 **NNP + `SECBIT_NOROOT_LOCKED` + seccomp 필터로 `set*id`(및 teardown용 kill/unlink)
>   primitive만** 남기고, bounding set 최소화, **임의 바이너리 execve·root 소유 경로 쓰기 금지**. 그래도 남는
>   잔여 위험은 열린 질문으로 둔다(§8). 아래 두 세부는 이 헬퍼의 능력 범위를 정한다:
>   - 게이트웨이가 U 소유 상태를 **읽어야** 하는 경로(`/files` 라우트, transcript rehydrate, rmtree)는
>     브로커가 U로서 대행하거나, 의도적으로 설계한 group 권한 체계로 연다. `CAP_CHOWN`은 소유권을 게이트웨이
>     uid로 되돌려야 할 때만 필요하다 — 대행 방식은 권한이 작은 대신 경로마다 브로커 호출이 늘고, group 방식은
>     단순하지만 group 경계를 정확히 지켜야 한다.
>   - 대안(비권장): 브로커가 `CAP_KILL`/`DAC_OVERRIDE`/`FOWNER`/`CHOWN`을 직접 쥐고 종료·정리하는 방식.
>     구현은 단순하지만, 상주 프로세스의 `DAC_OVERRIDE`는 파일 접근에 관해 root에 가까운 권한이라 브로커가
>     뚫리면 피해 범위가 크다.
> - **래퍼 file cap 요령.** `cli_path` 래퍼에 `cap_setuid,cap_setgid+ep` 파일 cap을 주면 게이트웨이는 cap 0,
>   래퍼만 uid 전환+Landlock+exec을 한다. file cap은 `no_new_privs`가 서면 무시되므로 게이트웨이는 래퍼를 **NNP
>   없이** exec해야 한다(그래서 아래 N의 confused-deputy 문제가 생긴다). 비root 시작(compose `user:`)이라 브로커를
>   둘 root 단계가 없으면, 종료·정리용 **형제 file-cap 헬퍼**(U로 전환 후 시그널·삭제)를 따로 두어 spawn과
>   teardown을 모두 file cap으로 덮는다(단 teardown의 EPERM 문제는 C가 그대로 남는다).
> - **(N) file-cap 헬퍼 confused deputy — 요청 검증 ≠ 호출자 인증.** B 때문에 게이트웨이는 래퍼를 **NNP 없이**
>   exec해야 하고, 그 상태에선 게이트웨이가 NNP 없이 exec하는 **다른 모든 uid-1000 자식**(`install_plugins`,
>   `plugin_admin`의 `claude`/`git`, MCP 연결 테스트 명령, app-server)도 헬퍼의 setuid primitive를 호출할 수
>   있다. 헬퍼가 요청(대상 uid·경로)을 검증해도 **호출자가 게이트웨이인지**는 인증되지 않는다. 주의: "게이트웨이에
>   NNP를 건다"는 깔끔한 해법은 **B와 충돌**한다(NNP가 서면 래퍼 자신의 file cap도 무시된다). 그러므로 방어는
>   둘 중 하나다 — **(a)** 헬퍼가 **호출자=게이트웨이임을 인증**(래퍼 spawn에만 넘기는 상속 socketpair fd, 다른
>   자식은 못 물려받게 CLOEXEC, `SO_PEERCRED` 확인), 또는 **(b)** 래퍼 외 **모든 게이트웨이 자식 spawn에 개별
>   NNP**를 걸어 file cap을 못 쓰게 한다. 어느 쪽도 공짜가 아니다(§8).
>
> **(D) uid 전환·cap drop·Landlock 순서(래퍼/브로커 공통) — 전환을 먼저, 룰셋을 나중에.** 룰셋을 **전환
> 전**에 만들면 빌더가 uid 0(SETUID/SETGID만, `DAC_OVERRIDE` 없음) 또는 uid 1000이라 **DAC 우회 cap이 없어**,
> 세션 전용 0700·U 소유 **rw** 경로(per-session `.claude`, 워크스페이스, `/home/app` 0700 아래 전부)를 `O_PATH`로
> 여는 `landlock_add_rule`이 `EACCES` → **룰셋을 못 만들고 첫 턴이 실패**한다. `restrict_self`는 **최종 uid가
> 아니라 NNP만** 요구하므로 올바른 순서는: (1) 게이트웨이가 래퍼를 **NNP 없이** exec(→ file cap 적용) → (2)
> `setgroups([])`→`setgid`→`setuid(U)`(file cap/브로커 root의 SETUID로; `setgroups`를 빼면 root 보조그룹이 남는다)
> → (3) **남은 cap 전부 0** → (4) `PR_SET_NO_NEW_PRIVS` → (5) `landlock_create_ruleset`+`landlock_add_rule`(이제
> U로서 자기 rw 경로와 o+r 공유 경로를 연다) → (6) `landlock_restrict_self`(NNP 충족) → (7) 실제 CLI `exec`.
> NNP가 **전환 뒤·exec 전**에 서므로 `setuid(0)`·setuid-root·file-cap 복사본이 CLI 자식에서 euid/CapEff를 되찾지
> 못한다 — 그래서 **bounding set을 미리 깎을 필요는 없다**(그건 `CAP_SETPCAP`를 더 요구하고, 전환 전 permitted에서
> SETUID/SETGID를 빼면 전환 자체가 `EPERM`). ⚠️ 이는 이 문서 이전 초안의 "NNP → Landlock → setuid" 순서를
> 뒤집는다 — 그 순서가 바로 위 `EACCES`를 유발한다.
>
> **(C) 자식 종료(#147) — 가장 어려운 열린 긴장. "브로커에 위임"으로 깨끗이 안 풀린다.** B의 in-place 래퍼를
> 쓰면 CLI는 게이트웨이의 **직속 자식**(그래서 `wait()`는 정상)이지만 **uid U**다. SDK `close()`는 자신이 띄운
> 그 프로세스에 `wait→terminate→wait→kill→wait` 래더를 거는데(`subprocess_cli.py`), uid 1000 게이트웨이가 uid U
> PID에 보내는 그 `terminate()`/`kill()`은 `EPERM`이고 SDK는 **`ProcessLookupError`만 suppress**하므로
> `PermissionError`가 그대로 올라와 teardown 래더(#147의 SIGTERM→SIGKILL, `CLIENT_DISCONNECT_TIMEOUT`가 지키는)가
> 깨진다. 거꾸로 CLI를 **브로커가 소유**하면 게이트웨이 `wait()`가 `ECHILD`라 종료코드 오보·좀비가 난다. 즉
> 종료는 깨끗이 "브로커에 위임"되지 않는다 — 선택지(모두 비용 있음): **(a)** 게이트웨이가 `CAP_KILL`만 쥔다(본체
> cap 0 목표와 상충하나 가장 작은 cap), **(b)** 종료를 상주 헬퍼로 라우팅하되 **SDK 내부의 EPERM도 함께 처리**
> (close() 전 pre-reap, 혹은 올라오는 에러 수용 — 얇은 SDK 래핑 필요), **(c)** SDK 패치. 하나를 고르기 전엔
> teardown은 미해결이다(§8). (Landlock 시그널 scoping은 샌드박스 **안→밖** 시그널만 막고 밖의 U-헬퍼가 안으로
> 보내는 시그널은 막지 않으므로 ABI ≥ 6에서도 헬퍼 경로 자체는 Landlock에 막히지 않는다 — 막는 건 위 uid 벽이다.)
> - **(O) reaping.** ①에서 uid-U CLI는 (in-place 래퍼로) 게이트웨이의 직속 자식이라 **게이트웨이가 죽으면**
>   PID 1로 재부모화되는데 기본 compose엔 `init:`이 없어 **PID 1 = uvicorn**이고 uvicorn은 거두지 않아 좀비가
>   쌓인다(#147). 게이트웨이는 uid 벽 때문에 SIGKILL도 못 보낸다(위). → compose `init: true`(docker-init이 거둠)나
>   상주 헬퍼의 reaping이 필요하며, 이는 위 `wait()`/`returncode` 공백과 한 묶음이다.
>
> **(V) userland은 passwd 항목을 요구한다.** 커널은 숫자 uid `setuid`에 `/etc/passwd`를 요구하지 않는다.
> python `getpass.getuser()`는 **`USER`/`LOGNAME`을 먼저 읽어**(prod 3.12 interpreter에서 확인: 그 둘이 있으면
> passwd를 안 본다) allowlist에 둘이 있으면 **성공**하므로 passwd가 없어도 된다 — 즉 passwd 레코드가 실제로
> 필요한 건 `whoami`와 node `os.userInfo()`다(CLI 본체는 try/catch로 버티지만 모델이 쓴 코드·쉘 도구가 깨진다).
> uid 대역의 passwd/group 항목을 이미지 빌드 때 또는 특권 헬퍼가 만들거나 `nss_wrapper`를 쓰고, allowlist에
> `USER`/`LOGNAME`을 반드시 포함한다(§6.4 — 이게 있어야 위 `getuser()`도 성립한다).

> **중간 선택지 — Landlock 단독(uid 전환 없음).** cap이 아예 없어도(①의 특권 spawner·종료 문제가 통째로
> 사라진다) Landlock은 open() 계층에서 파일 읽기(F1)를 가두고, 같은-uid 프로세스의 `/proc/<pid>/environ`
> 읽기(F2)도 막는다(Landlock의 ptrace 제약은 5.13부터 — 이 F2 속성만 보면 "모든 Landlock 커널 5.13+"이 맞다).
> **단 쓸 만한 파일 가둠에는 ABI ≥ 2(커널 ≥ 5.19)가 필요하다**: ABI 1(5.13–5.18)에는 `LANDLOCK_ACCESS_FS_REFER`가
> 없어 **어떤 룰셋이든 cross-dir rename/link를 전부 `EXDEV`로 강제**한다 — git object finalize, uv/npm 설치,
> `mv a/x b/`가 세션 안에서 깨진다. 그래서 프로브는 **ABI 1을 사용 불가로 판정**하고(BEST PATH·LANDLOCK-ONLY
> 모두 ABI ≥ 2 요구) NO PATH에서 "≥5.19 필요"로 안내한다. 남는 구멍은 둘: (1) **교차 세션 시그널** —
> Landlock-only에선 모든 세션이 uid 1000이라 서로에게·게이트웨이에 시그널을 보낼 수 있다(시그널 scoping은 6.12+
> `LANDLOCK_SCOPE_SIGNAL`, abstract unix socket scope도 6.12+에서만 닫힌다), (2) **사용자 간 파일 격리(DAC
> 소유권)** 미제공. 완전한 ①의 대체는 아니지만, cap 변경이 막힌(단 ABI ≥ 2인) prod에선 F1/F2를 당장 닫는
> 현실적 1차 방어가 된다.

> **무특권 즉시 완화 — 게이트웨이 non-dumpable (#232 — 별도 PR, 아직 미병합: 이 브랜치 `src/`엔 `PR_SET_DUMPABLE`가
> 없다).** 위 메커니즘 전부와 독립적으로, FastAPI
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
라이브러리만 쓰고 읽기 전용이며(fork된 자식에서만 수행 후 종료하는 테스트는 네임스페이스 unshare/`mount`와
Landlock 두 자식 — ABI 조회 + `create`/`add_rule`/`restrict_self` enforce 체인 — 뿐이고, `setuid`는 *테스트*가
아니라 게이트웨이 bounding set에 `SETUID`+`SETGID`가 있는지 읽기(`grantable`)다), 커널 버전·게이트웨이 프로세스
(기본 PID 1, init shim이면 그 자식)의 uid/gid·cap·seccomp·AppArmor
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
  `landlock_*` 허용; Landlock이 seccomp 때문에만 실패했다면 BWRAP 판정에도 "허용하면 ① 또는 LANDLOCK-ONLY가
  가능"이 붙는다 — `grantable`(bounding set에 SETUID/SETGID)이면 ①(uid-per-user+Landlock), 아니면 LANDLOCK-ONLY).
  fork가 pids/nproc 한도로 실패한 테스트는 DENIED가 아니라 `UNKNOWN`으로 찍히고 `missing:`에 `?`로 표시된다 —
  그리고 **샌드박스를 그 네임스페이스 때문에 `broken`으로 단정하지 않는다**(아예 돌지 않은 테스트이므로 `cli_denied`에서
  빼 별도 `unknown` 상태로 둔다).
- NONE이 아닌 판정 뒤에는 **F2 문단**(uid 분리/새 PID ns/Landlock 5.13+, 그리고 `PR_SET_DUMPABLE` 무특권
  스톱갭과 그 한계)을 덧붙이고, Landlock을 쓸 수 있으면 **INTERIM**(Landlock-only 안내 + 그 한계: 같은-uid
  시그널·DAC 미격리)을 더한다. **CLI OS 샌드박스 줄**(`=> CLI OS sandbox can run`)은 프로브가 게이트웨이의 실제
  트리거(`CLAUDE_SANDBOX_ENABLED`, 없으면 `WORKSPACE_SANDBOX_ENABLED`/`USER_WORKSPACE_QUOTA_MB>0`)를 흉내 내 상태를
  가른다: 게이트웨이가 **안 켜면**(`=false` → `off`, 혹은 미설정 + 워크스페이스 샌드박스·쿼터 없음 → `unset`;
  프로젝트 설정이 결정, 기본 off), 켜는데 `bwrap`/`socat`이 **없으면 `inert`**(Bash가 샌드박스 없이 돈다), 켜고
  바이너리도 있는데 **userns·pidns·mountns·(중첩 모드가 아니면) 새 procfs 중 하나가 거부되면 `broken`**(게이트웨이
  기본값 `allowUnsandboxedCommands=false`라 폴백이 없어 **모든 Bash 호출 실패**), 그 네임스페이스가 **fork 실패로
  미검증이면 `unknown`**(위 — broken으로 단정 안 함), 다 통과면 `ok`. NONE 판정엔 이 줄을 안 붙인다.

## 5. 반드시 지킬 제약: plugin/skill 공유 유지

**plugin 등으로 설치된 스킬은 모든 사용자에게 계속 공유되어야 한다.** 이 격리가 그걸 깨면 안 된다.

- 공유 자산의 출처가 **둘로 갈린다**(한곳·한번이 아니다):
  - **플러그인 트리만** 기동 때 `docker/install_plugins.py`가 설치한다(git clone + `claude plugin marketplace
    add`/`install`; `~/.claude/settings.json`의 `enabledPlugins`는 그 `claude plugin` 호출의 **부수효과**로
    적힌다 — 이 스크립트가 직접 쓰는 게 아니다).
  - **`skills`/`agents`/`commands`/`output-styles` + user-scope `CLAUDE.md`**은 `install_plugins.py`가 **건드리지
    않는다** — `GATEWAY_STARTUP_SCRIPT`(Dockerfile `CMD`가 install_plugins 전에 실행)·선택적 ro bind mount·기존
    `claude_home` 볼륨에서 온다.
  이들은 모두 **읽기 전용 공유 트리**로 두고 **모든 세션 uid/감옥에 ro 접근을 허용**한다.
- ⚠️ **플러그인 트리는 런타임에 게이트웨이가 다시 쓴다.** `plugin_admin_service.prepare_repo`가 clone dest를
  `shutil.rmtree` 후 `os.replace`로 **통째로 교체**한다(admin add/install·per-marketplace Refresh·
  `plugin_autorefresh`; uninstall/remove는 `claude plugin` CLI가 트리를 다시 쓴다). 그래서 **살아있는 세션**에
  대해 ②의 ro-bind는 **mid-session에 빈 디렉터리**가 되고, ①의 inode 고정 Landlock 규칙은 **respawn 전까지 그
  플러그인 파일에 `EACCES`**가 난다. 리팩터는 이 런타임 재작성과 "공유 ro" 전제의 충돌을 어떻게 다룰지 정해야
  한다(교체 후 세션 재적용/재마운트, 또는 규칙을 inode가 아닌 디렉터리 경로 단위로).
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
    ⚠️ 단 이 재유도는 게이트웨이가 **실제로 읽는** 루트(`CLAUDE_PLUGIN_CLONE_ROOT`, `claude_home/plugins/`의
    `known_marketplaces.json`·`installed_plugins.json`)만 따라간다 — 게이트웨이는 `CLAUDE_CODE_PLUGIN_*`(위
    `CLAUDE_CODE_PLUGIN_CACHE_DIR`/`SEED_DIR` 노브 포함)을 **전혀 읽지 않는다**(`plugin_resource_roots`). 그래서
    플러그인을 `CLAUDE_CODE_PLUGIN_SEED_DIR`로 seed해 공유하면 그 seed 경로가 `plugin_resource_roots()`에 **안
    들어가** 워크스페이스 샌드박스가 그 스킬 읽기를 거부하고(레지스트리 `installPath`도 실재하지 않는 값이 된다),
    그 샌드박스가 OFF여야만 샌다. 즉 SEED_DIR 분리(위 `plugins/data` 항목)와 '재유도'는 자동으로 맞물리지 않는다 —
    seed 경로를 샌드박스 루트에도 **명시로** 넣어야 한다.
  - ⚠️ **읽기 모델도 반만 따라온다.** `plugin_service._plugins_root()`는 `CLAUDE_CONFIG_DIR`를 보지만
    (`agent_catalog`·`plugin_admin`·`claude_settings_env`가 이를 거쳐 `/admin/api/plugins`·`/v1/agent-resources`를
    낸다), 샌드박스/guard의 `_claude_home`은 `$HOME`만 본다. 그래서 공유 트리를 **HOME 밖**으로 옮기면(§5 ①의
    `/opt/claude-shared` 권고) 읽기 모델이 그 경로를 몰라 `/admin/api/plugins`·`/v1/agent-resources`가 **과소보고**
    한다(세션 자체는 샌드박스 루트만 맞으면 영향 없음). 좁지만 실재하는 간극이다.
- **검증 항목**: CLI가 세션별 config dir에서 공유 스킬/플러그인을 **여전히 인식**하는지 e2e로 확인.
  프로브의 "SHARED ASSET LAYOUT" 섹션이 자산마다 소유권·권한과 함께 **다른 uid가 닿는지**(막는 상위
  디렉터리)와 **안에서 못 쓰는 항목 수**(o+r 없는 파일, o+x 없는 실행 파일·디렉터리)를 찍으니 리팩터
  범위 산정에 쓴다.

## 6. spawn 설계 (메커니즘 공통 골격)

`cli_path` 격리 래퍼(=§2 item G의 고정·root 소유 런처)가 하는 일:

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
   - **①**: 순서·cap drop·특권 위치·종료/reaping은 §3(특히 D·C·O). 룰셋 ro: 시스템 경로·CLI 설치·공유 자산 /
     rw: 워크스페이스·per-session `.claude`. ⚠️ **`/app`은 룰셋에 넣지 않는다**(ro에도): Landlock ro는 **읽기를
     허용**하는 규칙이라 `/app`을 ro로 넣으면 그 아래 `/app/data`의 자격증명 오버레이(`gateway-mcp.json`,
     `gateway-mcp-plugin-overlay.json`, `gateway-claude-settings-env.json`, 프롬프트 배포 로그, usage db —
     `./data:/app/data`로 bind; 모드도 제각각 — 앞의 세 JSON 오버레이는 mkstemp라 0600, 프롬프트 배포 로그(append)와
     usage sqlite는 0644라, 같은 uid엔 어느 쪽도 무의미하고 0644는 오히려 더 노출)까지 **모든 세션이 읽게 된다.**
     CLI는 `/app` 아래
     아무것도 필요 없으므로 **규칙 부재로 읽기·쓰기를 모두 거부**하는 게 맞다(§1·E — 코드 덮어쓰기 차단은 ro가
     아니라 **규칙 부재**로 달성된다). 세션 uid를 **`app`(1000)과 다르게** 두면 `/app`·공유 자산은 DAC로도 쓰기
     불가가 되고, Landlock-only(모두 1000)여도 `/app`이 룰셋에 없으면 쓰기가 거부된다.
     ⚠️ **rw에 세션 전용 TMPDIR·`/dev/null`·패키지 캐시를 반드시 포함**한다: CLI는 기동 때
     `${CLAUDE_CODE_TMPDIR||TMPDIR||/tmp}/claude-<uid>`를 `mkdir`하고(없으면 `EACCES`로 exit 1), Bash 도구는
     모든 명령을 `< /dev/null`로 감싼다(`/dev/null` 쓰기 권한 없으면 매 호출 "permission denied"). 또 세션의
     `uv run`은 **`UV_CACHE_DIR`**(이미지가 `/home/app/.cache/uv`로 고정 — `Dockerfile` ENV(`d8e2060`) +
     entrypoint의 provision/chown(`28fe482`)에서 쓰기 가능하게 됨), `npx`/stdio MCP 서버는 **`$HOME/.npm`**(명시 설정은 없고 HOME이 app 소유라
     암묵적으로 쓰인다)을 쓴다. 공유 `/tmp`를 rw로 열면 Landlock-only(모두 uid 1000)에선 세션 간 통로가 되므로
     TMPDIR은 **세션별 `TMPDIR`/`CLAUDE_CODE_TMPDIR`**(세션 `.claude` 하위)로, `/dev/null`은 파일 규칙으로 준다.
     그리고 uid 분리·세션 HOME 이전 아래선 고정된 `/home/app/.cache/uv`·`/home/app/.npm`이 U에게 **쓰기 불가**가
     되므로, 두 캐시를 **세션 쓰기 경로로 재지정**(`UV_CACHE_DIR`을 세션 하위로, `$HOME/.npm`은 세션 HOME을
     따라오게)해 rw 규칙/kept env에 넣지 않으면 `uv run`·`npx`가 `EACCES`로 깨진다.
   - **②**: `bwrap --unshare-user --unshare-pid --proc /proc --dev /dev --die-with-parent --new-session
     --ro-bind <공유 자산> … --bind <워크스페이스> …`로 깨끗한 env와 함께 실제 `claude` exec. ⚠️ **빠지면 F2가 안
     닫히는 플래그들**: `--unshare-user`(비setuid·무특권 bwrap이 pid/mount ns를 만들려면 user ns가 필수),
     **`--proc /proc`**(새 PID ns 위에 **새 procfs**를 올려야 `/proc/1`이 게이트웨이가 아니라 샌드박스 자신이 돼
     F2가 닫힌다 — `--unshare-pid`만으론 옛 procfs가 비친다), `--dev /dev`(최소 `/dev`; Bash가 모든 명령을
     `< /dev/null`로 감싸므로 `/dev/null`이 있어야 한다). 번들 CLI 자신의 샌드박스도 `--proc`/`--unshare-user`/
     `--new-session`을 쓴다. ⚠️ **`--die-with-parent` 필수**: 없으면 teardown 시그널이 바깥 bwrap에만
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
   **게이트웨이가 자식에 심는 제어 변수까지 명시로 포함**한다. ⚠️ **다만 '세팅 지점에서 생성'만으론 부족하다**:
   그 방식은 게이트웨이가 **스스로 세팅하는** 변수만 본다 — 운영자가 env로 주는, 게이트웨이가 세팅도 읽지도 않는
   CLI 노브는 **조용히 떨어진다.** 예: `BASH_DEFAULT_TIMEOUT_MS`(게이트웨이는 ceiling 계산에 `BASH_MAX_TIMEOUT_MS`만
   읽고 per-call 기본값인 이 변수는 안 본다)가 빠지면 자식 Bash 기본 타임아웃이 CLI 기본값 120000ms로 되돌아가고,
   `DISABLE_*`처럼 **'미설정=켜짐'**인 노브는 빠지는 순간 그 기능이 도로 켜진다. 그래서 allowlist는 **명시적으로
   리뷰된 목록**이어야 하고 운영자 지정 CLI 변수까지 포괄해야 한다(순수 생성만으론 안 된다). `CLAUDE_CODE_HARBOR_KITE`는
   **빈 값·미설정이
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
   `X-MCP-Context` = `{{user}}`와, `MCP_FORWARD_CONTEXT` 설정 시 `{{header:…}}` 자격증명, relay_id)과 시스템
   프롬프트 argv(**프리셋 모드면 `--append-system-prompt`**, 배포된 base가 있으면 `--system-prompt`)에 담긴 caller
   지시문(+ `Current user: <user>`)이 실린다. `hidepid=2` 리마운트는 `CAP_SYS_ADMIN`을 요구해(기본 cap에서
   거부됨) 권장 경로가 아니다. 대신 **argv에서 빼는** 무특권 수단은 둘:
   - **MCP 설정은 파일 경로로.** `mcp_servers`에 dict 대신 **파일 path(str)**를 주면 SDK가 `--mcp-config <경로>`로
     넘겨 JSON이 argv에서 빠진다(`subprocess_cli.py` — dict를 주면 inline JSON이 그대로 실린다).
   - **시스템 프롬프트는 `--append-system-prompt-file`로.** ⚠️ SDK의 `SystemPromptFile`은 `--system-prompt-file`에
     매핑되는데, 이는 **claude_code 프리셋을 통째로 교체**한다(프리셋 모드의 기본 `--append-system-prompt`와 의미가
     달라, 그대로 쓰면 base 프리셋이 사라진다). CLI엔 숨은 `--append-system-prompt-file`이 있으니 **SDK `extra_args`로**
     그 플래그를 넘겨 프리셋은 유지한 채 지시문만 파일로 뺀다(SDK 0.2.160엔 append-file 타입이 없어 `extra_args`가
     유일한 경로다).
   `hidepid`는 가능하면 추가 방어로만.

## 7. 단계별 계획

1. **(이 PR)** 역량 프로브 + 본 설계 문서. prod에서 프로브 실행 → 메커니즘 확정.
2. **세션별 HOME 리팩터** (공유 ro 자산 보존). 가장 공유-스킬 민감한 부분 — 먼저, 독립적으로. §5의
   config-dir 소비자(transcript 경로·guard 루트·`--add-dir`)와 `settings.json`/`plugins/data` writer를 함께
   이전·수정.
3. **특권 spawn 래퍼 + 상주 teardown 헬퍼 + `cli_path` 고정 런처** (① uid+Landlock 또는 ② bwrap). 특권 배치는
   §3: **spawn은 file-cap re-exec 래퍼**(SDK 파이프 보존 — B), **종료·reaping은 가둔 상주 특권 헬퍼**(A). 신원
   전달 채널·"세션 아닌 spawn" 처리(§2)·confused-deputy 방어(§3 N)도 여기서. ⚠️ **이 단계는 step 4(워크스페이스
   uid 소유권·정리)와 한 묶음으로 가야 ①이 성립한다**: uid만 전환하고(여기) 소유권·정리(step 4)를 미루면 uid-U
   CLI가 1000 소유 워크스페이스에 못 쓰고 게이트웨이가 그 산출물을 못 지운다. 또 **자식 종료(§3 C)의 해법을 이
   단계에서 고르지 못하면**(CAP_KILL vs 헬퍼+SDK EPERM 처리 vs SDK 패치) teardown이 매 턴 깨지므로, step 3을
   '스폰만' 넣고 끝내선 안 된다.
4. **(step 3과 한 묶음) 워크스페이스 uid 소유권** + `/app` 쓰기 차단(§6.3 — ro가 아니라 **룰셋 부재**/DAC) +
   `/proc` 하드닝. ⚠️ 세션이 만든 파일이 uid U 소유가 되면 **uid 1000 게이트웨이가 못 지운다**:
   `workspace_manager.cleanup_temp_workspace`(rmtree, `ignore_errors=True`라 조용히 실패)와
   `agent_messages._cleanup_sdk_transcript`가 stateless 정리 보장을 깨고, 업로드·`/files` 라우트는 U 소유
   디렉터리에서 EACCES가 난다. 그래서 **U 소유 파일 상태의 읽기·정리**는 §3대로 상주 헬퍼가 U로 전환한 단명
   자식으로 대행하거나 의도한 group 권한으로 연다(`CAP_CHOWN`은 소유권 반환이 필요할 때만) — 단 이건 **파일
   op**이고, **살아있는 CLI 프로세스 종료**(§3 C)와는 별개의 더 어려운 문제다(그쪽은 step 3). 무특권 스톱갭
   `PR_SET_DUMPABLE`(#232)과 entrypoint 하드닝(#233)은 **둘 다 별도 PR**(현재 이 브랜치에 미병합)이며 이 단계와
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
- **(설계 결정 — 프로브가 답하지 않음) 자식 종료(§3 C).** uid-U CLI를 uid 1000 게이트웨이가 못 죽이고 SDK
  `close()`가 `PermissionError`를 던지는 문제를 (a) 게이트웨이 `CAP_KILL`, (b) 상주 헬퍼 경유 + SDK 내부 EPERM
  처리(pre-reap 또는 에러 수용), (c) SDK 패치 중 무엇으로 풀지 — teardown의 하드 게이트. reaping(§3 O: 게이트웨이
  사망 시 uid-U 고아를 누가 거두나)도 같은 묶음.
- **(설계 결정) 특권 헬퍼 잠금(§3 A)·confused deputy(§3 N).** 헬퍼를 비root로 둘지, 아니면 `CAP_SETUID`+NNP+
  `SECBIT_NOROOT_LOCKED`+seccomp(set*id/kill/unlink만)로 가둘지. 호출자 인증을 상속 socketpair+`SO_PEERCRED`로
  할지 비-래퍼 자식 전부에 NNP를 걸지. 남는 상주 특권 표면의 잔여 위험을 수용할지(종이 위에서 깨끗이 안 풀린다).
- uid 할당 대역과 수명(생성·회수), 동시 사용자 상한. 키는 `WorkspaceManager` resolved key와 일치(§6.1).
- 격리 단위: **사용자(테넌트)별**이 기본으로 충분(같은 사용자의 다른 세션은 기밀성 위협이 아님).
  단 `user`가 없는 stateless/익명 런은 각자 단기 uid(§6.1).
- **resume와의 상호작용**: `/v1/responses`의 세션 재개가 턴을 넘어 **같은 uid/HOME로** 매핑되어야
  한다(세션 매니저 ↔ uid 매핑의 안정성; transcript 경로 이전과 함께 — §5).
- `BACKENDS` 값: codex/opencode가 켜져 있으면 그 spawn 경로는 래퍼 밖이다(§2).
- prod가 init shim(compose `init: true`/`--init`)을 쓰는가 — 쓰면 PID 1은 docker-init이고 F2 대상과 브로커
  배치 전제가 게이트웨이 자식 기준으로 바뀐다(§1·§4).
- prod가 compose `user:`로 비root 시작하는가 — 그러면 브로커를 fork할 root 단계가 없어 spawn과 종료·정리를
  모두 형제 file-cap 헬퍼로 덮어야 한다(§3; 프로브는 이를 검사하지 않고 가정만 한다, §4).
- 네트워크 egress 통제(①은 egress를 다루지 않음 — 별도 레이어 / #173의 zero-egress 게이트).
