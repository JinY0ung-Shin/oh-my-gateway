# 사용자별 프로세스 격리 (설계)

> **상태: 설계/계획, 일부 구현.** Landlock 단독과 ①은 `CLAUDE_ISOLATION_MODE=landlock|uid`(#241)로 들어왔다(§3·§5·§6의
> 구현 노트). uid 모드는 워크스페이스 소유권과 컨테이너 프로비저닝(§7 step 4) 전에는 쓸 수 없다. 메커니즘 선택은 prod
> 호스트의 커널·권한에 달려 있고, 그 사실은 `scripts/isolation_probe.py`를 prod 컨테이너에서 돌려 확정한다.
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
  - 다만 빌드 훅 `GATEWAY_BUILD_INSTALL_SCRIPT`(비공개 스크립트를 넣는 자리)가 `bubblewrap`/`socat`을 설치하면
    달라진다. CLI의 가용성 검사는 `bwrap`·`socat`이 PATH에 있는지만 본다(ripgrep은 번들된 것을 쓴다). 그래서
    상태는 셋이다: (1) 둘이 없으면 Bash는 제한 없이 돈다(위). (2) 둘이 있고 기본 seccomp처럼 userns가 막혀 있으면
    "Sandbox disabled" 없이 **모든 sandboxed Bash 호출이 `bwrap: No permissions to create new namespace`로
    실패한다**(게이트웨이 기본값 `allowUnsandboxedCommands=false`, 로컬 CLI 2.1.283으로 확인). (3) 둘이 있고
    프로파일이 userns·mount를 허용하면 CLI 샌드박스가 실제로 돈다(그래도 #218의 `Read` deny 규칙이 없어 읽기는
    막지 않는다; ②는 그것을 중첩하고, ①·Landlock 단독의 도메인 안에선 bwrap이 뜨지 못하므로 꺼야 한다 — §3).
    `command -v bwrap socat`(과 프로브의 PATH 줄)은 **존재만** 보므로 (2)와 (3)을 가르지 못한다 — 배포 컨테이너에서
    프로브의 **`=> CLI OS sandbox can run`** 줄로 판정한다(게이트웨이 트리거·user/managed 설정·바이너리·네임스페이스 시험을 합친 값, §4).
- **읽기 경계(#234, opt-in `CLAUDE_BLOCK_READS_OUTSIDE_WORKSPACE`)도 F1의 '코드를 써서 실행' 경로는 못 막는다.**
  CLI 네이티브 `permissions.blockReadsOutsideWorkingDirectories`를 `--settings`로 넘겨 파일 도구의 워크스페이스
  (+`add_dirs`) 밖 읽기를 거부하고, CLI가 판별하지 못하는 Bash 읽기(계산된 경로·`python -c` 등)는 ask가 돼 게이트웨이
  콜백이 거부한다(그래서 실행 모드를 `bypassPermissions`로 돌린다). 하지만 에이전트가 워크스페이스에 쓴 스크립트를
  실행하면 CLI는 그 안을 보지 못해 프로세스가 읽을 수 있는 것은 다 읽는다 — `client.py`의 읽기 경계 주석과
  `tests/test_cli_read_boundary.py`가 이 한계를 고정한다. 그래서 F1은 여전히 OS 경계(Landlock·bwrap; uid 분리는 교차 사용자 읽기만)가 닫는다.
- **PID 1 = 게이트웨이(기본 구성).** entrypoint(`docker/entrypoint.py`)가 `os.execvp`로 CMD를 exec 하므로
  PID 1은 uvicorn 게이트웨이 프로세스다. `/proc/1/environ`은 게이트웨이의 **마스킹되지 않은 전체 env**다. 단
  compose `init: true`/`--init`(#147 고아 회수의 그럴듯한 해법)을 쓰면 PID 1은 docker-init이고 게이트웨이는 그
  자식이다 — 그때 F2의 대상은 PID 1이 아니라 **게이트웨이 자식 프로세스**다(이하 "PID 1"은 이 경우 게이트웨이
  프로세스로 읽는다. 프로브는 `$PORT`의 LISTEN 소켓 주인으로 게이트웨이를 찾고, 거기서 확정되지 않으면 init shim을
  감지해 PID 1의 자식을 따라간다). ⚠️ **docker-init이 root인지는 시작 방식에 달렸다**: 기본(root로 시작)이면
  docker-init은 root지만, compose `user:`로 **비root 시작**하면
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
자격증명 오버레이까지 읽기 허용이 된다 — §6.3), uid 분리면 세션 uid를 `app`과 다르게 두고 DAC로 `/app`
쓰기를 막는다; 이미지가 애초에 쓰기 상태만 app 소유로 넘기도록 바꾸는 선택지도 있다). 같은 맥락에서 root 단계(entrypoint의 소유권 복구)는 #233에서
하드닝됐다: symlink를 따라가지 않고, 대상 uid 소유가 아닌 다중 링크 비디렉터리는 건너뛰며(그래서
호스트 sysctl `fs.protected_hardlinks`에 의존하지 않는다), root 단계 인터프리터를 격리 모드(`python -I`)로
돌려 app 사용자의 site 디렉터리를 읽지 않고, 읽기 전용·예기치 않은 경로가 있어도 기동을 실패시키지 않는다.
`CLAUDE_ISOLATION_MODE=uid`면 이 단계는 세션 uid 소유 항목을 chown하지 않고, 그 안은 세션이 통제하므로 걷지도 않는다 —
세션의 `projects/<name>`(transcript·메모리)이 0600/0700 그대로 app 소유가 되면 그 세션을 다시 잇지 못했다. 세션 uid는
대역(게이트웨이와 같은 파싱)과 uid 레지스트리(`CLAUDE_ISOLATION_UID_REGISTRY`, 기본 `/app/data/isolation_uids.json`)에
기록된 모든 uid에서 root·app uid·브로커 uid를 뺀 것이다 — 대역을 좁히면 named 사용자의 uid가 대역 밖에 남고, 게이트웨이는
이 단계가 끝난 뒤에야 그 사용자 때문에 기동을 거부한다. 레지스트리는 src를 import하지 않고 root로 읽으며, symlink·일반
파일 아님·16 MiB 초과·읽기 불가·형식 오류면 경고만 남기고 대역만 본다. 다른 모드는 전처럼 모두 app uid로 되돌린다(모드를
바꾼 뒤 남은 uid 모드의 것까지). 단 uid를 요청하고 `CLAUDE_ISOLATION_FALLBACK=landlock`으로 내려가면 entrypoint는 그 폴백을 알 수
없어, 세션 uid 소유 `projects/<name>`이 재기동 뒤에도 그대로 남고 landlock 모드(app uid) 세션은 거기 쓰지 못한다.

> 운영 주의: 이미지는 `HOME=/home/app`을 전역으로 설정하므로 root로 `docker exec` 하면 그 HOME을 물려받아
> root 도구가 app 소유 dotfile과 user site를 읽을 수 있다. 점검·디버깅은 `-u app`으로 붙는다(프로브 안내와 동일).

이 문서가 다루는 것은 "값을 가린다"가 아니라 **"다른 사용자/게이트웨이의 것에 OS가 닿지 못하게
한다"**는 경계다. 이는 CLAUDE.md의 #218(Bash OS 격리) 및 #173("two-user filesystem isolation")과
같은 범위의 작업이다.

## 2. 배포 형태 전제

이 설계는 **옵션 (a): 단일 고정 컨테이너**를 전제한다. 테넌트별 샌드박스를 띄우는 오케스트레이터가 없으므로,
격리는 **컨테이너 안에서, 세션을 띄우는 순간** 걸어야 한다. 바깥(호스트/런타임 플래그)은 바꾸기
어렵지만, **이 레포의 이미지와 자식 spawn 경로는 바꿀 수 있다.**

주입 지점: SDK는 `cli_path`로 CLI를 띄운다(게이트웨이는 `_get_cli_path()`로 그 값을 정한다 —
`src/backends/claude/client.py`, `slash_commands.py`). 따라서 **`cli_path`를 격리 런처로 고정**하고 그 런처
뒤에서 실제 `claude`를 선택한 격리 아래 띄우면(①은 런처가 SDK의 파이프를 상주 브로커에 넘기고 브로커가 띄운다 —
§3 B; 특권이 필요 없는 ②·Landlock 단독은 런처가 그 자리에서 re-exec), SDK를 패치하지 않고 **모든 Claude 백엔드
세션**에 격리를 건다(이 런처를 운영자 노브 `CLAUDE_CLI_PATH`에 **얹으면 안 되는** 이유는 아래 두 번째 ⚠️).

> ⚠️ **이 런처는 Claude 백엔드에만 적용된다.** `_get_cli_path()`를 타지 않는 spawn 경로는 격리 밖이다:
> opt-in app-server 어댑터(`src/backends/appserver/`)는 `codex app-server`를 **자기 프로세스로** 띄운다
> (`transport.py`의 `asyncio.create_subprocess_exec`, `cli_path` 런처를 타지 않음). 이 어댑터는 env를 그대로
> 넘기지는 **않는다**: `build_isolated_env`(`isolation.py`)는 override만 만들어(auth env + **사용자별
> `CODEX_HOME`**(`resolve_codex_home`) + allowlist된 metadata) 아무것도 지우지 못하고, 제거 집합은 어댑터
> `client.py`가 따로 만든다(`env_remove = ISOLATION_ENV_REMOVE | get_isolation_vars()` —
> `ANTHROPIC_AUTH_TOKEN`/`ANTHROPIC_API_KEY` 등). transport는 `os.environ.copy()`에 그 override를 merge하고
> `env_remove` 키를 pop해 띄운다. 다만
> **`child_env_mask()`는 적용하지 않아** `ADMIN_API_KEY`/`API_KEY`는 **그대로 자식에 샌다**(frozen
> `codex`/`opencode` 백엔드도 동일). Claude 백엔드가 쓰는 Landlock/uid/워크스페이스 격리도 전혀 없으므로,
> `BACKENDS=codex`(+`CODEX_BACKEND=appserver`) 등을 켜면 그 세션은 **F1/F2가 다시 열린다.** 게다가 app-server
> **종료**는 게이트웨이 프로세스에서 `os.killpg`로 자식 프로세스 그룹에 시그널을 보내는데(`transport.py`의
> `_signal_group`; 그룹은 `start_new_session=True`로 만든다), uid 분리 아래선 이 시그널이 `EPERM`으로 **debug
> 로그만 남기고 삼켜지고**(유일한 escalation인 뒤이은 `SIGKILL` killpg도 같은 uid 벽에 막힌다) 그룹이 조용히
> 샌다(codex 판 #147). 이 문서가 #173("two-user filesystem isolation" 게이트)와 같은 범위라고 적지만, Claude용
> 런처만으로는 #173의 codex 경로를 커버하지 못한다 — **#173 cutover는 app-server의 spawn *과 종료* 경로가
> 모두 같은(또는 동등한) uid/파일 격리 + 특권 teardown을 타기 전에는 완료로 쳐선 안 된다.** prod의 `BACKENDS`
> 값은 사용자 확인 필요.

> ⚠️ `_get_cli_path()`는 지금 **fail-open**이고, 게다가 `CLAUDE_CLI_PATH`는 **이미 운영자 노브**다
> (`src/backends/claude/constants.py`·`.env.example`에 "핀된 SDK를 안 올리고 **더 새 CLI 빌드를 시험**하는 용도"로
> 문서화돼 있다). `_get_cli_path()`의 검사는 `path.is_file() and os.access(path, X_OK)`뿐이라 **런처인지 평이한
> CLI인지 구분하지 못한다**: `CLAUDE_CLI_PATH`를 실제 `claude`로 가리키면(또는 prod가 이미 CLI 시험용으로 그렇게
> 쓰고 있으면) 격리가 **깨끗한 기동과 함께 조용히 꺼진다**. 없거나 실행 불가면 경고만 찍고 번들 CLI로 떨어지므로
> (매 세션 빌드마다 검사) 재빌드로 런처가 사라져도(+x 누락·경로 오류·볼륨 미마운트) **모든 새 세션이 격리 없이**
> 돌고 기동은 멀쩡하다. → 격리를 `CLAUDE_CLI_PATH`에 **얹지 말고**, 항상 `cli_path`로 넘기는 **고정·root 소유
> 런처**를 두고 실제 CLI(운영자의 `CLAUDE_CLI_PATH` 또는 번들)는 격리 쪽이 고른다(①이면 브로커가 기동 때 고정한다,
> §3 B). 그리고 기동 때 **설정된 `cli_path`가 바로 그 런처인지**를 경로·소유자·서명 등으로 **양성(positive) 검증**해, 아니면
> `ConfigIssue(error)`/세션 거부로 **fail-closed** 한다 — 부재뿐 아니라 '엉뚱한 바이너리로 대체됨'까지 막아야
> 경계가 된다.
>
> ⚠️ **런처는 "어느 사용자인가"를 스스로 알지 못한다.** §6의 첫 단계는 "사용자 → uid 매핑"인데,
> 게이트웨이는 그 신원을 자식에게 신뢰 가능한 형태로 넘기지 않는다(현재는 system prompt 텍스트와
> caller가 준 metadata뿐, cwd가 유일한 힌트). 게다가 세션이 아닌 `cli_path` spawn이 여럿 있다:
> SDK가 매 connect마다 거는 `claude -v` 버전 체크(`options.env`·cwd·user 없이 프로세스 env로 띄움), 슬래시 명령
> 카탈로그/preflight(`slash_commands.py`, `child_env_mask()`도 미적용), `/v1/slash-commands`, 기동 때의 슬래시 목록
> 로그와 `verify()` 질의(admin 헬스체크도 `verify()`를 부른다). 런처를 신원 필수로 **fail-closed** 설계하면 이들이
> 제각각 깨진다: 슬래시 턴은 500(`responses.py`는 preflight의 `SlashCommandError`만 400으로 바꾼다), 스킬 해석
> 경로(`client.py`가 `slash_commands.get_available_commands`(**`slash_commands.py`**, `client.py`가 아니다)를 호출)는
> granular 분기면 try/except로 원요청을 유지하고, 평이한 분기는 `HIDDEN_SKILLS`가 설정됐을 때만 spawn하며 그때
> **새 세션마다 실패**한다. 기동 로그·`verify()`는 에러 로그와 `healthy: false`만 남긴다. **조용히** 깨지는 것도 있다:
> `_fetch_command_details`는 세션마다가 아니라 `GET /v1/slash-commands`에서만 spawn하고 그 라우트가 모든 예외를 잡아
> **200 + 빈 목록**을 낸다(에러만 보는 테스트는 놓친다). SDK의 `-v` 체크는 모든 실패를 삼켜 최소 버전 경고만 잃고,
> `CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK`로 그 spawn 자체를 없앨 수 있다(게이트웨이는 지금 세팅하지 않는다). 반대로 세션
> 아닌 spawn도 워크스페이스 project 설정(훅 포함)을 로드하므로, **신원이 없다고 격리 없이 통과시켜선 안 된다.**
> → ①의 **신원 전달 채널**은 §3 B의 **게이트웨이가 점유한 브로커 제어 채널 + spawn별 1회용 티켓**이다. SDK가 spawn마다
> 넘겨 주는 건 argv·env·cwd뿐이고 fd는 넘기지 못한다(SDK는 `pass_fds` 없이 띄워 fd 0/1/2만 살아남는다 — §3 N).
> 세션 아닌 spawn도 단기/무사용자 uid의 티켓으로 격리하고, 티켓 없는 호출(= `-v`)은 거부하되 게이트웨이가
> `CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK`를 세팅해 그 spawn을 없앤다.
>
> 대안: SDK엔 이미 CLI를 다른 uid로 띄우는 `ClaudeAgentOptions.user`(`types.py`, `user: str|None`)가 있다
> (`subprocess_cli.py`가 그대로 `anyio.open_process(..., user=self._options.user)`로 넘기고 → `Popen(user=)`에
> 닿는다). 단 이것은 런처·브로커의 **대체가 아니다**: (0) **가장 결정적** — `Popen(user=)`는 **게이트웨이가 fork한
> 자기 자식 안에서**(exec 직전) `setreuid`를 하므로 **게이트웨이 프로세스 자신이 `CAP_SETUID`를 쥐어야**
> 한다. 이는 §3의 "게이트웨이 본체는 cap 0" 원칙과 정면 충돌하고, 그걸 성립시키려 ambient `CAP_SETUID`를 두면
> 그 cap이 **게이트웨이가 exec하는 모든 자식**에 샌다(§3). (1) 자식 내부에서 뭔가를 실행할 훅이 없어(anyio
> `open_process`에 `preexec_fn` 없음) **Landlock 룰셋·env allowlist는 이 경로로 걸 수 없고**, 부모에서 Landlock을
> 걸면 게이트웨이 자신이 갇힌다. (2) SDK에 **`group` 필드가 아예 없어** `Popen`에 `group=`이 안 가므로 **gid는
> 1000으로 남는다**(그래서 쟁점은 `CAP_SETUID`뿐, `SETGID`는 이 경로에 없다) — 세션들이 gid 1000을 공유해 서로의
> group-readable 파일을 읽고 새 파일도 gid 1000이 돼 **DAC 파일 격리가 약해진다**. (3) 숫자 uid를 문자열로 주면
> `getpwnam` 실패 → 정수로 넣어야 하는데 타입힌트는 `str|None`이다. (4) **cwd 벽**: CPython의 자식은
> `chdir(cwd)`를 uid 전환보다 **먼저** 하므로(로컬 CPython 3.12/3.13: 탐색 불가 cwd에 다른 uid를 주면 `setreuid`의
> `EPERM`이 아니라 `chdir`의 `EACCES`가 난다) U 소유 0700 워크스페이스면 이 경로도 §3의 cwd 벽에 그대로 걸린다.
> (덧붙여, 게이트웨이는 **지금 `options.user`를 세팅하지도 않는다** — `user`는 system prompt 텍스트로만 흘러간다.)
> `no_new_privs`·cap drop은 게이트웨이 자신에 미리 걸면 이 경로로도 되지만, (0)의 cap 충돌과 Landlock·allowlist·
> gid·cwd 때문에 **결국 `cli_path` 런처(①이면 + 브로커)가 필요**하다.

## 3. 메커니즘 후보

| | ① uid-per-user + Landlock | ② bubblewrap / nsjail |
|---|---|---|
| F2 (`/proc/1/environ`) | uid 분리 → 세션 uid ≠ PID1 uid라 EACCES | 새 PID 네임스페이스 → PID1이 세션 자신 |
| F1 (임의 파일 읽기) | Landlock이 open() 계층에서 강제(경로 생성 방식 무관) | 새 mount 네임스페이스 → 워크스페이스만 bind |
| 사용자 간 파일 | uid 소유권(DAC) | mount 네임스페이스에 애초에 없음 |
| 필요 권한 | **특권 표면은 가둔 상주 브로커 하나**(`CAP_SETUID`/`SETGID`)이고 spawn·종료·정리·reaping을 모두 맡는다(B·A). `cli_path`는 file cap 없는 무특권 런처 스텁으로, SDK의 파이프를 브로커에 넘길 뿐이다. **게이트웨이 본체는 cap 0 + NNP.** | userns + mount — 기본 Docker seccomp가 `mount`를 `CAP_SYS_ADMIN` 뒤로 막고, AppArmor `docker-default`는 `deny mount`라 **seccomp를 풀어도** `apparmor=unconfined`/커스텀 프로파일이 추가로 필요. 새 procfs mount가 Docker의 masked `/proc` 경로 때문에 거부되는 런타임이면 `systempaths=unconfined`까지(프로브가 시험하고 안내한다) |
| 컨테이너 친화성 | 높음(mount-ns·seccomp 안 건드림) | 낮음(바깥 경계를 넓힘) |
| 커널 요구 | Landlock ABI 2(cross-dir rename) ≥ 5.19 | unprivileged userns |

**옵션 (a)에서는 ①을 권장한다.** ②는 컨테이너 바깥 경계(userns+mount+AppArmor)를 넓히는 반면, ①은
그 범위를 **가둔 상주 브로커 하나**(B·A)에 모으고 Landlock이 "코드로 파일 읽기" 우회까지 LSM 레벨에서 닫는다.
②는 ①이 불가능할 때의 폴백이다.

> ⚠️ **①과 Landlock 단독(아래)에선 CLI 자신의 OS 샌드박스를 꺼야 한다.** Landlock은 FS 접근을 다루는 도메인의
> `mount`/`pivot_root`를 거부하므로 그 도메인 안에선 bwrap이 뜨지 못한다(로컬 6.18 커널: CLI와 같은 플래그의
> bwrap이 도메인 밖에선 돌고, 모든 FS 권한을 `/`에 준 도메인 안에선
> `bwrap: Failed to make / slave: Operation not permitted`). 게이트웨이는 `CLAUDE_SANDBOX_ENABLED`가 미설정이면
> 워크스페이스 샌드박스나 `USER_WORKSPACE_QUOTA_MB>0`이 켜질 때 이 샌드박스를 강제로 켜므로(`_configure_sandbox`)
> **`CLAUDE_SANDBOX_ENABLED=false`를 명시**한다(`false`는 워크스페이스 샌드박스가 켜져 있어도 존중된다). 그러면 SDK가
> `--settings`(flag 층)로 `sandbox.enabled=false`를 넘기고, CLI의 설정 병합에서 flag는 user·project·local보다 앞서므로
> (낮은 것부터 user·project·local·flag·policy) 다시 켤 수 있는 건 managed(policy) 설정뿐이다 — managed 설정이
> `sandbox.enabled`를 켜지 않게 하고, user 설정에서 빼는 건 방어심층으로만 둔다. 안 그러면 bwrap+socat이 있을 때
> **모든 sandboxed Bash 호출이 실패**하고(`allowUnsandboxedCommands=false`라 폴백이 없다), 둘 중 하나가 없을 때
> `failIfUnavailable`이면 CLI가 exit 1 한다(CLI의 가용성 검사는 존재만 본다). bwrap을 요구하는
> `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB`도 같은 이유로 이 조합에선 못 쓴다(§6.4). 프로브의 `=> CLI OS sandbox can run`은
> 도메인 **밖**에서 잰 값이다.

> **⚠️ 특권은 게이트웨이가 아니라 별도 spawner가 쥔다 (가장 중요).** ①에 필요한 cap을 게이트웨이 본체에
> 남기는 방식(`KEEPCAPS`+ambient로 하락 너머 유지)은 **채택하지 않는다.** ambient cap은 `execve`를 넘어
> 살아남으므로, 게이트웨이가 ambient `CAP_SETUID`를 쥐면 **게이트웨이가 exec하는 모든 것**(플러그인 설치,
> `plugin_admin`의 `claude`/`git`, MCP 연결 테스트가 띄우는 admin 지정 명령, app-server, fail-open 시
> 번들 CLI)이 그 cap을 물려받아 임의 uid(0 포함)로 전환할 수 있다. 대신 특권을 별도 프로세스로 격리한다:
> - **(B) spawn은 상주 브로커가 한다 — `cli_path` 스텁은 SDK의 파이프를 넘길 뿐이다.** SDK는 자신이 `cli_path`로
>   띄운 프로세스와 **그 파이프 fd로만** NDJSON 턴/`control_request`/`initialize`를 주고받고, `wait`/`terminate`/`kill`도
>   **그 pid에만** 건다(`subprocess_cli.py`의 `anyio.open_process(cmd, stdin=PIPE, stdout=PIPE, …)`와 `close()`). 파이프
>   끝은 `SCM_RIGHTS`로 다른 프로세스에 넘길 수 있고, 넘겨받은 fd의 읽기·쓰기엔 추가 권한 검사가 없다. 그래서 역할을
>   나눈다:
>   - **`cli_path` = 고정·root 소유·무특권 런처 스텁**(§2의 고정 런처; file cap 없이 게이트웨이 uid로 돈다). 스텁은
>     자신을 non-dumpable로 만들고, 게이트웨이 uid만 닿는 디렉터리의 **고정 경로** spawn 소켓에 접속해 `options.env`로
>     받은 **1회용 티켓**을 내고, fd 0/1/2를 `SCM_RIGHTS`로 넘긴 뒤 자기 0/1 사본을 닫는다(파이프 EOF가 CLI만 따르게).
>     그 뒤에도 SDK의 자식으로 남아 SIGTERM/SIGINT를 브로커에 중계하고 CLI의 종료 상태·시그널을 **자기 것으로
>     미러링**한다 — SDK의 `wait()`/`returncode`가 그대로 맞는다. 스텁이 죽으면(SIGKILL 포함) 브로커가 소켓 EOF를 보고
>     그 CLI 트리를 (U 감시 프로세스를 통해 — C) 죽인다.
>   - **브로커**는 spawn마다 자식을 fork하고, 그 자식이 D 순서로 uid U로 전환한 뒤 감시 프로세스로 남고, 감시
>     프로세스가 fork한 자식이 Landlock을 걸고 넘겨받은 fd를 stdio로 실제 CLI를 `exec`한다(C). exec 대상은 브로커가
>     기동 때 고정한 실제 CLI 하나다(운영자의 `CLAUDE_CLI_PATH` 또는 번들 — 요청에서 경로를 받지 않는다). argv는 스텁이 SDK가
>     만든 그대로 넘기고, 브로커는 실행 파일만 고정한다. CLI 트리가 브로커 아래에 있으므로 종료·reaping·U 소유 상태
>     정리도 브로커 쪽이 한다(C·O).
>   - **호출자 인증 = 게이트웨이가 점유한 제어 채널 + 1회용 티켓.** 게이트웨이의 자식은 모두 같은 uid라
>     `SO_PEERCRED`만으론 호출자를 가르지 못한다. 그래서 게이트웨이가 기동 때 브로커 제어 채널을 **처음이자 한
>     번만** 점유하고(`SO_PEERCRED` uid = 게이트웨이 uid; 점유 실패는 fail-closed 기동 에러), spawn마다 {uid,
>     워크스페이스, env allowlist, 룰셋 파라미터}를 담은 무작위 1회용 티켓을 등록한다. 유효한 티켓이 없는 spawn은
>     거부한다. 이것이 §2의 신원 전달 채널이다. SDK의 `-v` 버전 체크는 `options.env`가 아니라 프로세스 env로 띄우므로
>     티켓이 없다 — 게이트웨이가 `CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK`를 세팅해 그 spawn을 없애고, 스텁은 티켓 없는
>     호출을 거부한다. 스텁 environ의 티켓은 non-dumpable 스텁 + 1회용으로 노출을 줄인다(잔여 위험은 §8). env는 스텁이
>     자기 environ(= SDK가 만든 env)도 함께 넘기고 브로커가 티켓의 allowlist(이름)로 거른다 — 값까지 티켓에 실으면
>     SDK가 spawn 때 넣는 변수(`CLAUDE_CODE_ENTRYPOINT`·`CLAUDE_AGENT_SDK_VERSION`·
>     `CLAUDE_CODE_SDK_READS_SESSION_STATE`·checkpointing 플래그·`TRACEPARENT`/`TRACESTATE`)가 빠진다. 브로커는 `PWD`를
>     워크스페이스로 맞춘다(SDK는 중립 cwd를 넣는다; 위생일 뿐 — CLI 2.1.283은 `PWD`가 cwd로 이어지는 symlink일 때만
>     쓴다).
>   - **게이트웨이는 cap 0이고 file-cap 바이너리를 exec할 일이 없으므로 `PR_SET_NO_NEW_PRIVS`를 건다**(entrypoint가
>     CMD를 exec하기 전에 걸면 CMD 셸 단계의 `install_plugins.py`·`GATEWAY_STARTUP_SCRIPT`까지 덮는다).
>   - **로컬 확인**: 수정하지 않은 SDK 0.2.160·번들 CLI 2.1.283·`tests/fixtures/fake_anthropic_api.py`로, 스텁이
>     넘긴 SDK 파이프 위에서 **브로커의 자식인** CLI가 initialize·Bash 도구 호출·result까지 돌았고, 종료코드
>     미러링(0/7), `terminate()` → -15, `kill()` → -9(브로커가 소켓 EOF를 보고 CLI를 죽이고 거둠)도 맞았다. uid
>     전환은 이 확인 밖이다(D의 평범한 fork+`setresuid`). 전환 뒤엔 넘겨받은 fd를 그대로 써야 한다 — 파이프
>     inode는 만든 uid(게이트웨이) 소유 0600이라(로컬 확인) U가 `/dev/stdin`·`/proc/self/fd/N`으로 다시 열면 DAC에
>     걸린다.
> - **브로커 기동.** root로 시작하면 entrypoint가 `drop_privileges` **전에** 브로커를 fork해 상주시킨다(그 단계
>   하드닝 #233, §1 전제). 비root 시작(compose `user:`; root 단계 없음)이면 브로커 **바이너리**에 file cap
>   (`cap_setuid,cap_setgid+ep`; file cap은 `#!` 스크립트엔 효과가 없으니 브로커는 네이티브 바이너리여야 한다)을 주고,
>   **어디에도 NNP가 걸리기 전에** entrypoint가 그것을 띄운 뒤 자신에게 NNP를 걸고 CMD를 exec한다 — NNP는 fork·exec로 상속되므로 이후의 게이트웨이 uid
>   프로세스는 그 cap을 얻지 못한다. 그러려면 컨테이너가 NNP=0으로 시작해야 한다(`no-new-privileges` security_opt
>   없음). 프로브는 PID 1이 init shim일 때만 그 uid로 둘을 가리고, 아니면 root 시작을 가정한다 — 비root shim에
>   컨테이너 시작부터 NNP=1이면 브로커에 SETUID/SETGID를 줄 길이 없어 uid 분리를 권하지 않는다(§4).
> - **(A) 그 상주 브로커의 권한을 가둬야 한다 — "`CAP_SETUID`만 = 최소 권한"은 거짓.** `CAP_SETUID` 하나로도
>   `setresuid(0,0,0)`로 **root가 되고**(root-equivalent), uid 0 프로세스는 아무 `execve`에서나 Docker 기본 cap
>   전부를 되찾고 root 소유 파일(site-packages의 `.pth`, `docker-entrypoint.py` 등)을 써 **다음 `restart`에 완전
>   root**가 된다 — 이 문서가 `DAC_OVERRIDE`에 대해 거부한(아래) 바로 그 near-root 피해범위다. 그러므로 이
>   **상주 브로커를 "최소 권한"이라 불러선 안 된다.** 반드시 잠가야 한다 — **SETUID/SETGID만 남긴 전용 비root
>   uid**를 우선하고, 어느 쪽이든 NNP + `SECBIT_NOROOT(_LOCKED)` + **`set*id` 대상을 세션 uid 대역으로 묶는 seccomp
>   인자 필터**, bounding set 최소화, **고정된 실제 CLI 외 execve 금지·root 소유 경로 쓰기 금지**. 그래도 남는
>   잔여 위험은 열린 질문으로 둔다(§8). 아래 두 세부는 이 브로커의 능력 범위를 정한다:
>   - 게이트웨이가 U 소유 상태를 **읽어야** 하는 경로(`/files` 라우트, transcript rehydrate, rmtree)는
>     브로커가 U로 전환한 단명 자식으로 대행하거나, 의도적으로 설계한 group 권한 체계로 연다. `CAP_CHOWN`은
>     소유권을 게이트웨이 uid로 되돌려야 할 때만 필요하다 — 대행 방식은 권한이 작은 대신 경로마다 브로커 호출이
>     늘고, group 방식은 단순하지만 group 경계를 정확히 지켜야 한다.
>   - 대안(비권장): 브로커가 `CAP_KILL`/`DAC_OVERRIDE`/`FOWNER`/`CHOWN`을 직접 쥐고 종료·정리하는 방식.
>     구현은 단순하지만, 상주 프로세스의 `DAC_OVERRIDE`는 파일 접근에 관해 root에 가까운 권한이라 브로커가
>     뚫리면 피해 범위가 크다.
> - ⚠️ **cwd 벽(어느 토폴로지든).** SDK는 `options.cwd`를 `open_process(cwd=…)`로 넘기고, fork된 자식은 **exec
>   전에 게이트웨이 uid로** 그 디렉터리에 `chdir`한다. 게이트웨이는 세션 워크스페이스를 cwd로 넘기므로(`client.py`의
>   `effective_cwd`, `slash_commands.py`, `agent_messages.py`) 워크스페이스가 U 소유 0700이면 **모든 spawn이
>   `cli_path`가 돌기도 전에** `CLIConnectionError(… Permission denied)`로 실패한다(로컬 SDK 0.2.160 transport로
>   확인). B에선 SDK에 **중립 cwd**를 주고 실제 워크스페이스는 티켓에 실어 브로커 자식이 전환 **뒤** `chdir`한다.
>   다른 해법은 워크스페이스와 상위 경로를 게이트웨이 uid가 탐색할 수 있게 두는 것이다(예: 0710 + 게이트웨이
>   group). spawn 직전의 `materialize_workspace_resources(cwd)`도 워크스페이스를 만진다(§7 step 4).
> - **대안(비권장) — in-place file-cap 래퍼(이전 초안의 B).** `cli_path` 래퍼에 `cap_setuid,cap_setgid+ep` 파일
>   cap을 주고, SDK가 띄운 그 자리에서(상속한 파이프를 둔 채) uid 전환+Landlock+`exec`을 하는 방식. spawn용 특권
>   바이너리가 하나 더 생기고(종료·정리용 상주 헬퍼는 여전히 필요하다) 다음 비용이 붙는다:
>   - **(N) confused deputy — 요청 검증 ≠ 호출자 인증.** file cap은 `no_new_privs`가 서면 무시되므로 게이트웨이는
>     래퍼를 **NNP 없이** exec해야 하고, 그 상태에선 NNP 없는 **다른 모든 uid-1000 프로세스**(`plugin_admin`의
>     `claude`/`git`, MCP 연결 테스트 명령, app-server, 그리고 게이트웨이 자식이 아닌 CMD 셸의 `install_plugins.py`·
>     `GATEWAY_STARTUP_SCRIPT`)도 래퍼의 setuid primitive를 쓸 수 있다. 래퍼가 요청(대상 uid·경로)을 검증해도
>     **호출자가 게이트웨이인지**는 인증되지 않는다. 상속 fd로 인증하는 길은 **없다**: SDK는 `pass_fds` 없이 띄우고
>     (anyio → asyncio → `Popen`의 기본 `close_fds=True`) 래퍼에는 fd 0/1/2만 살아남는다(로컬 확인: 부모의
>     inheritable socketpair fd가 자식에 없다). env는 아무것도 인증하지 못한다(누구나 아무 env로 래퍼를 exec할 수
>     있다). 남는 방어는 래퍼 외 **모든 spawn에 개별 NNP**뿐이고, spawn 지점이 늘 때마다 빠뜨리기 쉽다.
>   - **종료 `EPERM`.** uid U인 CLI가 SDK의 직속 자식이라 SDK `close()`의 `terminate()`/`kill()`이 `EPERM`이고, SDK는
>     `ProcessLookupError`만 suppress하므로 `PermissionError`가 올라와 teardown 래더(#147의 SIGTERM→SIGKILL,
>     `CLIENT_DISCONNECT_TIMEOUT`이 지키는)가 깨진다. `/v1/agents/messages`의 직접 kill
>     (`agent_messages._kill_client_process` — CLI가 wedged면 `_DISCONNECT_WEDGED_TIMEOUT_S`가 SDK `close()`를
>     terminate 전에 끊으므로 이것이 유일한 시그널이다)도 `ProcessLookupError`만 삼켜 `PermissionError`가
>     나고, `_teardown`은 그것을 로그만 남긴 채 `finally`에서 정리를 시작한다 — uid 1000 그대로면 U 소유 파일을 못
>     지워 조용히 남기고(§7 step 4), 정리를 U 권한으로 옮기면 **살아 있는 CLI의 cwd를 지운다**("never delete a live
>     agent's cwd" 위반). 풀려면 게이트웨이 `CAP_KILL`(본체 cap 0 목표와 상충), 종료를 헬퍼로 라우팅하고 SDK·`agent_messages`
>     양쪽의 EPERM을 처리(얇은 SDK 래핑), 또는 SDK 패치(`agent_messages` 경로는 그대로 남는다) 중 하나가 필요하다.
>   - **reaping.** CLI가 게이트웨이(PID 1)의 자식이라 CLI의 고아 자손도 PID 1로 가 거둬지지 않는다(O).
>     entrypoint가 fork한 헬퍼는 CLI의 조상이 아니어서 거둘 수 없으므로 compose `init: true`(docker-init이 거둠)가
>     필요하고, 그러면 F2 대상이 바뀐다(§1).
>   - **secure exec의 환경 삭제.** 비root 프로세스가 file cap으로 cap을 얻는 exec은 secure exec(`AT_SECURE`)이라
>     glibc 로더가 래퍼의 `main` 전에 `TMPDIR`·`LD_PRELOAD`·`LD_LIBRARY_PATH`·`LOCPATH`·`TZDIR`·`RES_OPTIONS` 등을
>     지운다(ld.so(8)의 secure-execution mode; 로컬 glibc 2.39에서 setgid 복사본의 AT_SECURE exec으로 `TMPDIR`·
>     `TZDIR`·`LOCPATH`는 사라지고 `CLAUDE_CODE_TMPDIR`·`TMP`는 남는 것을 확인). 그래서 §6.3의 세션 `TMPDIR`과 V의
>     `nss_wrapper` `LD_PRELOAD`는 래퍼가 다시 세팅해야 한다. file cap은 `#!` 스크립트엔 적용되지 않으므로 래퍼는
>     네이티브 바이너리여야 한다.
>   - 위 cwd 벽도 그대로다(래퍼가 돌기 전에 실패한다).
> - **대안(비선호) — 커스텀 transport.** `ClaudeSDKClient(transport=…)`는 공개 훅이라 스텁 없이 게이트웨이가
>   직접 fd를 브로커에 넘길 수 있다. 다만 그 transport엔 `options`의 CLI 플래그가 적용되지 않아 비공개
>   `SubprocessCLITransport`의 명령 조립·`connect`(와 `-v` spawn)를 다시 구현해야 하므로 SDK 내부에 결합된다 —
>   pass-through 원칙상 선호하지 않는다. `ClaudeAgentOptions.user`는 대안이 아니다(§2).
>
> **(D) uid 전환·cap drop·Landlock 순서(브로커의 spawn 자식) — 전환을 먼저, 룰셋을 나중에.** (1)
> `setgroups([])`→`setresgid`→`setresuid(U)`(브로커의 SETUID/SETGID로; `setgroups`를 빼면 브로커의 보조그룹이
> 남는다) → (2) **남은 cap 전부 0** → (3) `PR_SET_NO_NEW_PRIVS` → (3′) fork: 부모는 U 감시 프로세스로 남아 스텁처럼
> 넘겨받은 0/1 사본을 닫고(C·O), 자식은 그 감시 프로세스 기준 `PR_SET_PDEATHSIG(SIGKILL)`을 건 뒤 `getppid()`로 감시
> 프로세스가 아직 살아 있는지 확인하고 → (4) U로서 워크스페이스에 `chdir` → (5)
> `landlock_create_ruleset`+`landlock_add_rule`(U로서 규칙 경로를 연다) → (6) `landlock_restrict_self`(NNP 충족) →
> (7) 넘겨받은 fd를 stdio로 두고 실제 CLI `exec`. `restrict_self`는 **최종 uid가 아니라 NNP만** 요구한다. NNP가
> **전환 뒤·exec 전**에 서므로 `setuid(0)`·setuid-root·file-cap 복사본이 CLI 자식에서 euid/CapEff를 되찾지 못한다 —
> 그래서 **bounding set을 미리 깎을 필요는 없다**(그건 `CAP_SETPCAP`을 더 요구하고, 전환 전 permitted에서
> SETUID/SETGID를 빼면 전환 자체가 `EPERM`). 룰셋을 **U로서** 만드는 이유: `landlock_add_rule`에 넘기는 `O_PATH`
> 열기는 대상 자체엔 권한이 필요 없고 **경로상 상위 디렉터리의 탐색(x) 권한만** 요구한다(open(2); 로컬 확인: mode
> 0000 디렉터리 자체는 열리고 그 아래 경로는 `EACCES`). 그래서 `EACCES`는 빌더가 탐색하지 못하는 디렉터리 **아래**의
> 규칙 경로에서 난다. 전환 전 빌더(`DAC_OVERRIDE`/`DAC_READ_SEARCH` 없는 root나 브로커 uid)는 U 소유 0700 디렉터리
> **아래**(워크스페이스·per-session `.claude` 하위의 세션 TMPDIR 등)를 탐색하지 못하지만, 전환 뒤의 U는 자기
> 디렉터리를 탐색한다. 반대로 U는 `/home/app`(0700, uid 1000 소유)을 탐색하지 못하므로 그 아래에 남은 규칙
> 경로(`/home/app/.cache/uv`, `$HOME/.npm`, `~/.claude`의 공유 자산)는 U로서 `EACCES`다 — 그 경로들은 U가 런타임에도
> 못 쓰므로 §5·§6.3대로 세션 경로와 HOME 밖 공유 트리로 옮긴다. ⚠️ 이는 이 문서 이전 초안의 "NNP → Landlock →
> setuid" 순서를 뒤집는다.
>
> **(C) 자식 종료(#147).** B에서 SDK의 직속 자식은 게이트웨이 uid의 스텁이므로, SDK `close()`의
> `wait→terminate→wait→kill→wait` 래더(`subprocess_cli.py`)와 `/v1/agents/messages`의 직접 kill
> (`agent_messages._kill_client_process`)은 **같은 uid 프로세스**에 닿아 `EPERM`이 없다. `terminate()`는 스텁이
> 브로커에 중계해 CLI에 닿고, 스텁은 CLI가 끝난 뒤 같은 상태로 끝나므로 `wait()`가 CLI 종료를 기다린다(SDK는 자기
> 자식인 스텁만 기다리므로 `ECHILD`도 없다). 남는 틈은 **스텁이 CLI보다 먼저 죽는 경우**다: `kill()`(SIGKILL)은
> 중계되지 않아 브로커가 소켓 EOF를 보고 CLI 트리를 죽이지만 그건 비동기다. 그 사이 게이트웨이의 `wait()`는 이미
> 돌아와 `agent_messages`의 `_teardown`이 `finally`에서 정리를 시작한다 — uid 1000 그대로면 U 소유 파일을 못 지워
> 조용히 남기고(§7 step 4), 정리를 U 권한(브로커 대행·group 권한)으로 옮기면 그때 **살아 있는 CLI의 cwd를
> 지운다**("never delete a live agent's cwd" 위반). 그래서 정리는 브로커로 라우팅해 **kill → wait(reap) → 삭제**를
> 보장한다(삭제 자체는 U로 전환한 단명 자식이 — A). 브로커가 죽으면 그 CLI들도 함께 죽어야 하고, 브로커가 없으면
> spawn은 fail-closed다. ⚠️ `PR_SET_PDEATHSIG`는 죽는 부모의 자격으로 보내는 시그널이라 kill 권한 검사를 받는다 —
> 브로커가 `CAP_KILL`을 쥐지 않으면(A의 잠금처럼 SETUID/SETGID만 남기면 root여도) uid U인 CLI에 조용히 닿지 않는다
> (로컬 확인: SETUID/SETGID만 남긴 root 부모가 죽어도 uid 2000 자식은 살아남고, `CAP_KILL`을 더하면 죽는다). 그래서
> 브로커 자식은 U로 전환한 뒤 **감시 프로세스**로 남아 CLI를 fork한다: 브로커와의 파이프가 EOF가 되면 CLI 프로세스
> 그룹을 죽이고(U→U라 권한이 필요 없다), CLI의 `PR_SET_PDEATHSIG(SIGKILL)`은 그 감시 프로세스 기준으로 전환 뒤에
> 건다(euid 변경은 이 설정을 지운다). 같은 SETUID/SETGID만의 root 브로커 아래에서 브로커가 죽든 감시 프로세스가
> 죽든 uid 2000 CLI가 함께 죽는 것을 로컬에서 확인했다. 스텁 사망·`terminate()` 중계도 이 감시 프로세스를 거친다.
> (Landlock 시그널 scoping은 샌드박스 **안→밖** 시그널만 막고 밖에서 U로 전환한 자식이 안으로 보내는 시그널은
> 막지 않으므로 ABI ≥ 6에서도 이 경로는 Landlock에 막히지 않는다.)
> - **(O) reaping.** 기본 compose엔 `init:`이 없어 **PID 1 = 게이트웨이(uvicorn)**다(§1). PID 네임스페이스의
>   init이 죽으면 커널이 네임스페이스 전체를 SIGKILL하므로(`init: true`면 docker-init이 자식과 함께 끝난다)
>   게이트웨이 사망은 고아를 남기지 않는다. #147의 실제 고아는 **CLI가 끝나거나 죽은 뒤 남은 CLI의 자손**이다:
>   가장 가까운 subreaper 조상이 없으면 PID 1(게이트웨이)로 재부모화되는데, 게이트웨이는 자기가 띄운 특정 pid만
>   `wait`하므로 거두지 않아 좀비가 쌓인다. B에선 CLI의 부모인 U 감시 프로세스(C)가 `PR_SET_CHILD_SUBREAPER`를
>   걸어(fork로 상속되지 않으므로 그 프로세스 자신이; 특권은 필요 없다) CLI와 그 고아 자손을 거두고 살아남은 U
>   프로세스를 죽이며(U→U), 감시 프로세스 자신은 그 부모인 브로커가 거둔다. 브로커도 `PR_SET_CHILD_SUBREAPER`를
>   건다 — 세션은 같은 uid라 자기 감시 프로세스를 죽일 수 있고(시그널 scoping이 없으면), 그때 CLI는 PDEATHSIG로
>   죽지만 `setsid`로 떨어져 나간 자손은 브로커로 올라와야 거두고 U로 전환한 단명 자식으로 죽일 수 있다(로컬 확인:
>   SETUID/SETGID만 남긴 root 브로커가 subreaper가 아니면 그 자손은 PID 1 — 게이트웨이 자리 — 로 가고, subreaper면
>   브로커로 온다). ABI ≥ 6이면 세션 룰셋에 `LANDLOCK_SCOPE_SIGNAL`을 넣어 세션이 감시 프로세스에 시그널을 못 보내게
>   한다. reaping엔 특권이 아니라 **조상 관계**가 필요하다 — CLI의 조상이 아닌 형제 헬퍼는 거둘 수 없다.
>
> **구현 노트(PR #244 리뷰 2).** 위 C·O의 "U 감시 프로세스"는 구현에서 **spawn별 supervisor** 하나로 합쳐졌고,
> 완전한 U가 아니다: `setgroups([])` → `setresgid(G,G,G)` → `setresuid(broker, U, broker)` 뒤 cap을 전부 버린다.
> real/saved uid가 브로커라 uid U 프로세스는 supervisor에 시그널도 ptrace도 못 하고(kill은 보내는 쪽 real/effective를
> 받는 쪽 real/saved와 비교한다 — **시그널 scoping이 없는 ABI < 6 커널에서도**), effective uid가 U라 supervisor는 U
> 트리에 시그널을 보낼 수 있다. CLI 자식이 `(U,U,U)`를 스스로 마저 맞추고 확인한다(특권 불필요). 그래서 위의 "세션이
> 자기 감시 프로세스를 죽일 수 있다"는 경로는 닫혔다. 런처 연결이 제어 채널이다: 중계된 SIGTERM/SIGINT는 CLI 그룹으로,
> EOF면 SIGTERM → 첫 SIGTERM 5초 뒤 SIGKILL, CLI가 끝나면 남은 하위 프로세스를 자식이 없을 때까지 SIGKILL·reap한 뒤에야
> 상태를 보낸다(CLI는 Bash 명령마다 따로 세션을 만들므로 그룹 kill만으론 `cmd &`가 샜다; `kill(-1)`은 같은 uid를 쓰는
> 같은 사용자의 다른 세션까지 죽이므로 안 쓴다). 상주 브로커는 교차 uid로 시그널을 보내지 않는다 — CAP_KILL이 없어
> EPERM이 조용히 삼켜지던 것이 리뷰 2의 #1이다. supervisor가 비정상 종료해 남긴 프로세스만 브로커(subreaper)가 단명
> 자식으로 죽이는데, 그 자식은 그 uid를 effective uid로만 쥐고(supervisor와 같은 `(broker, U, broker)`, cap 0) 브로커는
> 그것을 최대 2초 기다린다(`WNOHANG|WUNTRACED`). 그래서 ABI < 6에서도 U의 고아가 그 자식을 멈추거나 죽이지 못하고,
> 단일 스레드인 상주 브로커가 세션이 시그널을 보낼 수 있는 프로세스를 기다리며 멈추지 않는다. 멈추거나 시간을 넘긴
> 자식은 SIGKILL하고 최대 2초 더 기다려 거두며(그래도 남으면 reaper에 맡긴다), 그 pid들은 치우지 못했다고 로그에 남긴 채
> 다시 시도하지 않는다. ABI ≥ 6이면 두 모드 모두 `LANDLOCK_SCOPE_SIGNAL | ABSTRACT_UNIX_SOCKET`을 더한다. 남은 창:
> 런처를 SIGTERM 중계 없이 바로 SIGKILL하면(`agent_messages` wedge) CLI가 최대 5초 더 살아 위의 정리 경쟁이 남는다 —
> 정리를 브로커로 라우팅하는 후속에서 닫는다.
>
> **구현 노트(PR #244 리뷰 3, 로컬 컨테이너 실측 2026-10-14).**
> - **실측 결과:**
>   - 위 배치(브로커 cap·securebits, supervisor `(broker, U, broker)`, CLI `(U,U,U)`, 종료 정리)는 d86a886 이미지에서
>     그대로 확인됐다. 런처 SIGKILL 뒤 정리는 1초 미만이었고, 5초는 SIGTERM을 무시하는 CLI에만 걸리는 상한이다.
>   - **출하 이미지에서는 uid 모드 턴이 하나도 돌지 않았다.** 워크스페이스가 app 소유 0755라 세션 uid가
>     `<ws>/.cli-tmp`를 못 만들었고, 브로커가 그 실패를 삼켜 CLI가 exit 1로 죽었다.
>   - 그런데도 기동은 통과했다. 브로커 도달성만 봤기 때문이다.
> - **바뀐 점:**
>   - `.cli-tmp`를 쓸 수 있는 디렉터리로 만들지 못하면 이제 바로 실패한다(브로커 자식 exit 118, 런처 exit 70). 그 자리의
>     디렉터리 아닌 것(symlink·파일·FIFO·소켓)은 지우고 새로 만들며, 소유자 rwx를 잃은 자기 디렉터리와 그 안의 CLI
>     `claude-<uid>`도 고친다 — `touch .cli-tmp`나 `chmod 000 .cli-tmp` 하나로 그 워크스페이스의 이후 spawn이 모두 막혔었다.
>     다른 uid 소유의 것(예: landlock 형태로 돈 preflight가 게이트웨이 uid로 남긴 `.cli-tmp`)은 `<name>.stale-<n>`으로 옮기고
>     새로 만든다(워크스페이스에 쓸 수 없으면 fail closed). 수리 방식은 §6 step 3의 구현 노트.
>   - 게이트웨이는 브로커가 응답하면 기동 때 한 번, 세션과 같은 경로로 ephemeral uid의 `--version`을 띄워 본다.
>     새 익명 워크스페이스, 같은 티켓과 경로 목록, 런처, 브로커를 그대로 쓴다.
>   - 이어서 그 uid가 설정 디렉터리에 닿는지, `projects/<name>`을 만들 수 있는지를 mode bit로 확인한다.
>   - 이 중 하나라도 실패하면 uid는 "사용 불가"다. 기동 오류가 나거나, `CLAUDE_ISOLATION_FALLBACK=landlock`이면
>     landlock으로 내려간다. 출하 이미지에서는 이 프로브가 실패한다.
> - **uid 모드를 쓰려면 남은 일:** 워크스페이스 소유권(§7 step 4)과 컨테이너 프로비저닝이 먼저다. 프로비저닝에는
>   세션 uid가 통과할 수 있는 공유 트리, 브로커 uid와 대역의 passwd 항목, 사용자별 config dir(적어도 세션 uid가 쓸 수 있는
>   `session-env`·`tasks` — 그 아래 `<session id>`는 브로커 자식이 세션 uid로 만든다)가 들어간다. 브로커가 세션 uid로
>   transcript를 읽고 지우는 대행도 남았다: 게이트웨이는 세션 uid 소유 0600 transcript를 읽지도 지우지도 못해, 메모리에
>   없는(만료·재기동) uid 모드 세션의 재개는 재시도로 풀리지 않는 HTTP 500 `transcript_unreadable`
>   (`session_manager.TranscriptUnreadable`, 경로마다 경고 한 번)이고 stateless transcript 정리는 경고만 남긴다. 세션
>   uid가 쓴 파일이 든 `session-env/<id>`·`tasks/<id>`도 같은 이유로 게이트웨이가 지우지 못해 남는다.
>
> **(V) userland은 passwd 항목을 요구한다.** 커널은 숫자 uid `setuid`에 `/etc/passwd`를 요구하지 않는다.
> python `getpass.getuser()`는 **`LOGNAME`·`USER`·`LNAME`·`USERNAME`을 차례로 보고 하나라도 있으면** passwd를 보지
> 않는다(CPython 3.12 `getpass.py`와 로컬 이미지의 Python 3.12.13으로 확인). 다만 출하된 env(이미지 ENV·entrypoint·
> compose)는 `USER`도 `LOGNAME`도 세팅하지 않으므로 **브로커가 uid별 값을 세팅**해야 이것이 성립한다 — 그러면
> passwd 레코드가 실제로 필요한 건 `whoami`와 node `os.userInfo()`다(CLI 본체는 try/catch로 버티지만 모델이 쓴
> 코드·쉘 도구가 깨진다). uid 대역의 passwd/group 항목을 이미지 빌드 때 또는 브로커가 만들거나 `nss_wrapper`를
> 쓰고, allowlist에 `USER`/`LOGNAME`을 반드시 포함한다(§6.4).

> **구현 노트(PR #244 리뷰 4, 117a0d0 재리뷰).**
> - **최소 ABI는 3(커널 6.2)이다.** 이 문서의 "ABI ≥ 2"는 그 이전 판단이다. ABI 1/2는 `LANDLOCK_ACCESS_FS_TRUNCATE`를
>   다루지 않아서, DAC가 허용하면 룰셋 밖 파일도 `truncate(2)`/`O_TRUNC`로 비울 수 있다. 그래서 두 모드 모두 ABI 2
>   호스트에서 기동을 거부한다(`MIN_LANDLOCK_ABI`, `docker/_landlock_rules.MIN_ABI`). 프로브도 ABI ≥ 3을 "usable"로 본다.
> - **익명 uid는 lease다.** LRU 재활용은 살아 있는 런의 uid를 새 런에 줄 수 있었다. 이제 런의 워크스페이스를 정리할 때
>   (CLI를 멈춘 뒤) 반납하고, 지우지 못한 워크스페이스는 uid를 계속 쥔다. 다 차면 워크스페이스가 사라진 lease부터
>   회수하고, 그래도 없으면 거절한다. 브로커도 다른 워크스페이스에서 살아 있는 uid와, 아직 정리하지 못했거나 정리를
>   포기한 고아가 쥔 uid를 거부한다.
> - **§6.5 argv 노출:** 격리 모드에서 `--mcp-config`와 시스템 프롬프트는 세션 워크스페이스의 1회용 파일로 넘기고
>   `connect()` 뒤 지운다(CLI는 argv 파싱 때 읽는다 — `tests/test_cli_argv_files.py`). `--json-schema`는 파일 형태가
>   없어 남는다. 요청자의 `text.format` 스키마라 게이트웨이 비밀은 아니다.
> - **런처 소유권은 기동 오류다.** 런처·`_landlock_rules.py`·(있으면) `claude-session-bash`·모든 상위 디렉터리가 root
>   소유·비쓰기여야 한다. dev 체크아웃만 `CLAUDE_ISOLATION_ALLOW_UNSAFE_LAUNCHER=true`로 경고로 낮춘다.
> - **프로브 실행 실패는 실패다.** `can_enforce_landlock`이 프로브를 못 돌리면 기동을 거부한다.

> **중간 선택지 — Landlock 단독(uid 전환 없음).** cap이 아예 없어도(①의 특권 브로커와 그 프로토콜이 통째로
> 사라진다 — `cli_path` 런처가 그 자리에서 룰셋을 걸고 exec한다) Landlock은 open() 계층에서 파일 읽기(F1)를 가두고,
> 같은-uid 프로세스의 `/proc/<pid>/environ` 읽기(F2)도 막는다(Landlock의 ptrace 제약은 5.13부터 — 이 F2 속성만
> 보면 "모든 Landlock 커널 5.13+"이 맞다).
> **단 쓸 만한 파일 가둠에는 ABI ≥ 2(커널 ≥ 5.19)가 필요하다**: ABI 1(5.13–5.18)에는 `LANDLOCK_ACCESS_FS_REFER`가
> 없어 **어떤 룰셋이든 cross-dir rename/link를 전부 `EXDEV`로 강제**한다 — git object finalize, uv/npm 설치,
> `mv a/x b/`가 세션 안에서 깨진다. 그래서 프로브는 **ABI 1을 사용 불가로 판정**하고(BEST PATH·LANDLOCK-ONLY
> 모두 ABI ≥ 2 요구) NO PATH에서 "≥5.19 필요"로 안내한다. ①에 비해 남는 구멍은 둘: (1) **교차 세션 시그널** —
> Landlock-only에선 모든 세션이 uid 1000이라 서로에게·게이트웨이에 시그널을 보낼 수 있다(시그널 scoping은 6.12+
> `LANDLOCK_SCOPE_SIGNAL`, abstract unix socket scope도 6.12+에서만 닫힌다), (2) **사용자 간 파일 격리(DAC
> 소유권)** 미제공. CLI 샌드박스는 ①처럼 꺼야 한다(위 ⚠️). 완전한 ①의 대체는 아니지만, cap 변경이 막힌(단 ABI ≥ 2인)
> 배포라면 F1/F2를 당장 닫는 현실적 1차 방어가 된다.
> 그리고 **세션 간 loopback TCP는 이 선택지·①·② 모두에서 열려 있다**: 세션이 loopback 리스너(미리보기·dev 서버,
> HTTP MCP 서버 등)를 띄우면 다른 테넌트의 세션이 거기 접속할 수 있다 — uid 분리는 TCP에 DAC를 걸지 않고, 위 FS
> 룰셋은 네트워크를 다루지 않으며, ②는 net ns를 공유한다(§6.3). 설계 안의 수단은 Landlock ABI 4(커널 6.7+)의 TCP
> 포트 규칙(`LANDLOCK_ACCESS_NET_BIND_TCP`/`LANDLOCK_ACCESS_NET_CONNECT_TCP`)이다 — 호스트가 아니라 **포트** 단위라
> CLI의 상류 포트와 게이트웨이(MCP relay) 포트는 허용으로 남겨야 한다. 세션이 리스너를 띄우지 않는다면 이 구멍은
> 쓰이지 않는다(§7 step 5, §8). pathname unix socket도 같다: Landlock FS 규칙은 `connect()`를 다루지 않아(로컬 6.18·
> ABI 7: 룰셋 밖 파일의 `open()`은 `EACCES`인데 그 옆 소켓으로의 `connect()`는 성공) Landlock 단독에선 다른 세션의
> 소켓 경로를 알면 붙고, 피어 uid만 보는 서버는 같은 uid라 받아 준다. ①은 소켓 inode의 쓰기 권한(DAC)이, ②는 mount
> ns가 막는다.

> **무특권 즉시 완화 — 게이트웨이 non-dumpable (#232, `src/process_hardening.py`).** 위 메커니즘 전부와 독립적으로, FastAPI
> lifespan의 **첫 단계**에서 `prctl(PR_SET_DUMPABLE, 0)`를 건다(기본 on; `GATEWAY_NON_DUMPABLE=false|0|no|off`로
> 끔, 빈 값은 on 유지). 세션 CLI 자식의 env에서는 `child_env_mask()`가 `ADMIN_API_KEY`·`API_KEY`와
> `SYSINFO_CHILD_ENV_MASK` 이름을 덮고(`SYSINFO_REDACTION`이 켜졌을 때만), `_configure_sdk_env`/`_isolation_vars`가
> `OPENAI_API_KEY`(api-key 인증이면 `CLAUDE_CODE_OAUTH_TOKEN`도)를 `options.env`에서 **늘 빈 값으로 덮는다**
> (`os.environ`을 `await client.connect()` 너머로 pop/restore하면, SDK가 `-v` 체크를 await한 **뒤에야** env를
> 스냅숏하는 사이 겹친 세션 생성의 자식이 값을 물려받는다 — `tests/test_sdk_env_race.py`). 그 값들은 게이트웨이
> environ에만 남고, 이 설정은 그
> environ을 **`/proc/<게이트웨이>/environ`으로 읽어 정리를 우회하는 경로를 닫고**,
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
(`$PORT`의 LISTEN 소켓 주인 — 그 포트의 LISTEN 소켓마다 읽을 수 있는 주인이 있을 때만 믿고, 아니면 PID 1로, PID 1이
init shim·root이거나 hidepid로 아예 안 보이면 그 자식으로 물러난다)의 uid/gid·cap·seccomp·AppArmor 라벨·`/proc` hidepid·
게이트웨이 dumpable 여부·그 `environ` 가독성(F2 재현)·Landlock ABI·CLI가 합치는 설정 층(`CLI SETTINGS` 섹션)·
`bwrap`/`socat` 존재·unshare/mount 가능성·공유 자산과 게이트웨이 코드(`/app`)에 다른 uid가 닿는지/쓸 수 있는지를
찍고 판정을 낸다. 판정은 **게이트웨이를 확정한 뒤 그 bounding cap**과 공유 자산 도달성을 근거로 삼고(게이트웨이의
신원을 못 얻으면 INCONCLUSIVE), 프로브 자신(`docker exec`)의 cap은 참고용으로만 찍는다.

```bash
docker compose exec -T -u app gateway python3 -I -S - < scripts/isolation_probe.py
```

> 스크립트는 **stdin으로** 넣는다(`-T`는 TTY 없이 파이프하기 위해서다). 컨테이너 `/tmp` 같은 고정 이름에 복사해
> 돌리면 같은 uid의 세션이 그 이름을 `__main__.py`를 담은 디렉터리로 미리 만들어 둘 수 있다 — 그러면
> `docker compose cp`는 그 디렉터리 안에 복사하고 `python3`는 디렉터리의 `__main__.py`를 돌린다(`-I -S`로도 못
> 막는다). 프로브는 `__file__`·argv·stdin을 읽지 않으며, 자신이 찍는 재실행 명령도 같은 stdin 형태다.
> `docker compose exec`는 USER 지시자가 없어 기본 root로 붙으므로 **`-u app`**이 필수다 — root로 돌리면 프로브는 `NONE` 판정으로 멈춘다.
> `APP_GID`가 1000이 아니면 `-u <uid>:<gid>`로 **gid까지 세션과 맞춰야** 한다(`/proc/<pid>/environ` 접근은 gid도
> 보므로, gid가 어긋나면 F2가 "이미 격리됨"으로 잘못 보고된다). 프로브는 자기 uid:gid가 게이트웨이와 다르면 신원
> 섹션의 `!! WARNING`과 VERDICT의 "`!! …; the F2/uid results above are not the sessions'. Re-run:`" 줄로 맞는 명령을
> 찍는다. hidepid가 게이트웨이를 가릴 때는 프로브가 세션과 같은 uid:gid인지가 갈린다: 같으면 그것이 곧 세션의
> 시야이고(hidepid는 non-dumpable 게이트웨이를 같은 uid에게도 가린다 — 게이트웨이 자신의 F2는 닫힌 것으로 찍고 판정을
> 이어 간다), 다르면 세션 시야가 아니라고 경고한다. 게이트웨이 후보가 여럿이거나 LISTEN 소켓 주인을 확신할 수 없으면
> (주인을 읽을 수 없는 LISTEN 소켓이 있는데, 대신 고른 프로세스는 fd가 읽히는데도 그 소켓을 쥐지 않았다면 — 쥐었다면
> 찾았을 것이므로 — 게이트웨이가 그 주인일 수 있어 확정하지 않는다; 고른 프로세스가 그 포트의 다른 LISTEN 소켓을
> 쥐고 있으면, 못 읽은 소켓이 이 uid 소유(`/proc/net/tcp`의 uid 열)일 때만 확정하지 않는다; fd가 안 읽히는
> non-dumpable 게이트웨이는 확정한다)
> 신원 섹션의 `!! not concluded`와 VERDICT의 "`!! gateway not concluded: …`"로 F2 판정을 보류한다.
> 개발 머신(WSL2)에서 돌린 값은 스모크 테스트일 뿐 prod가 아니다.

판정 규칙(프로브가 찍는 라벨, 검사 순서대로):

- **NONE** — 프로브가 root로 돌았다(root는 언제나 setuid할 수 있어 판정 불가). 재실행 명령만 찍고 멈춘다.
- **INCONCLUSIVE**(신원) — 게이트웨이 자신의 신원이 없다(`the gateway was not identified` /
  `the gateway runs as root` / `the gateway's uid:gid could not be read` — 미식별이면 확정 못 한 사정이 뒤에 붙는다; 이
  경우가 아래 "not concluded"보다 앞선다). "Is it running, with $PORT its port?"와 재실행 명령을 찍고 **멈춘다** —
  아래 F2·INTERIM·룰셋·CLI 샌드박스 문단을 내지 않는다.
- 판정 줄 앞의 `!!` 줄: 위 신원 caveat(`!! gateway not concluded: …` 또는
  `!! …; the F2/uid results above are not the sessions'. Re-run:`), 공유 자산 walk가 끝까지 못 간 경우의
  `!! shared-asset check INCOMPLETE`(walk마다 원인 — 시간
  상한·항목 상한·이 uid가 못 여는 디렉터리 — 를 적고, 깨끗한 행도 all-clear가 아니라고 경고한다), `/app` walk가
  쓰기 가능한 것을 못 찾은 채 중간에 멈춘 경우의 `!! gateway-code check INCOMPLETE`, 그리고 확인하지 않은 플러그인
  경로 값의 `!! <n> plugin path value(s) from the registries or settings were NOT checked … unverified`. 세션이 쓴
  값이라 걷지 않는 것들이다: 공유 루트 밖에 실재하는 레지스트리 경로(SHARED ASSET LAYOUT의
  `registry-declared (not walked)` 행이 경로마다 — 10개까지, 나머지는 `!! and more` — 그 경로까지의 도달성, 즉
  상위 디렉터리의 o+x와 그 경로 자신의 o+rx/o+r만 찍는다; 실재하지 않는 경로는 stale로 `registry paths` 행에 개수만
  적고 세지 않는다), 4096자·256성분·500개 상한에 걸려 버린 레지스트리 값, 쓸 수 없는 설정 경로 값(cache/seed/config
  dir), 그리고 앞 16개를 넘는 seed 항목이 있으면 1(나머지는 훑지 않는다).
- **BEST PATH: ① uid-per-user + Landlock** — Landlock usable(ABI ≥ 2 + enforce 체인 OK)이고 uid 전환이 **실제로
  가능**할 때(= reachable): **게이트웨이의 bounding set에 SETUID+SETGID**(= `grantable`)가 있고, PID 1이 비root uid로
  도는 init shim(증명된 비root 시작)이면서 컨테이너 시작부터 NNP=1인 상태 — root entrypoint도 file cap도 브로커에
  SETUID/SETGID를 줄 수 없다 — 가 아니다(그 상태면 UID SWITCHING의 `=> uid-per-user buildable`도
  `no, as started (non-root, NoNewPrivs from start)`). 불릿이 §3 B·A를 요약한다: `cli_path` = 고정·root 소유·file cap 없는
  런처 스텁이 1회용 티켓을 내고 SDK 파이프(fd 0/1/2)를 `SCM_RIGHTS`로 상주 브로커에 넘기며 브로커가 세션 uid로 CLI를
  fork, 게이트웨이는 cap 0이고 NoNewPrivs를 걸어도 된다; 브로커의 SETUID/SETGID는 최소 권한이 아니니 잠글 것;
  게이트웨이엔 KEEPCAPS/ambient 금지. 시작 방식은 PID 1이 init shim일 때만 가린다: shim이 root면 root 시작이라
  entrypoint가 drop 전에 브로커를 fork할 수 있고, 다른 uid면 비root 시작이라 브로커 바이너리 file cap과 컨테이너 시작
  시 NNP=0이 필요하다고 찍는다. shim이 없으면(PID 1 = 게이트웨이) **root 시작을 가정할 뿐 검사하지 않는다**.
  게이트웨이에 NoNewPrivs가 서 있으면 한 줄을 더한다: 프로브 자신도 NNP=1이면 컨테이너 시작부터인
  것이라("from container start") root 시작 브로커는 되지만 **비root 시작(브로커 바이너리 file cap)과 in-place file-cap
  래퍼 대안은 막힌다**; 게이트웨이만 NNP=1이면("on the gateway only") file-cap 래퍼 대안만 막힌다(§3의 브로커 기동·N).
  공유 자산 중 다른 uid가 못 닿는 항목이 있으면 "`!! shared assets fail for other uids -- fix before uid split`"(§5) —
  uid 분리를 권하는 다른 판정(uid 전환이 reachable인 NO PATH, Landlock이 seccomp 때문에만 실패하고 reachable인
  BWRAP)에도 붙는다.
- **LANDLOCK-ONLY** — Landlock은 쓸 수 있으나 uid 전환이 막힘(§3 중간 선택지): bounding set에 SETUID/SETGID가
  없거나(Docker 기본 bounding set엔 둘이 있고 아무도 빼지 않으므로, 운영자가 `--cap-drop`으로 뺀 경우가 아니면
  드물다), 둘이 있어도 reachable이 아닐 때다(위의 비root 시작 + 시작부터 NNP — 이때는 그 사정과 "root 시작이나 시작
  시 NNP=0인 file-cap 브로커가 필요하다"를 찍는다).
- **BWRAP** — 위가 안 되고 userns+pidns+mountns+새 procfs가 모두 OK → **② bwrap/nsjail**(테스트가 통과했으니
  현재 seccomp·AppArmor 프로파일로 충분하다; `bwrap` 미설치면 함께 안내). CLI 샌드박스가 `inert`/`fail`이면 "bwrap+socat을 설치하면 CLI 자신의
  샌드박스도 실제로 돈다(켠 소스 명시)"가, Landlock이 미검증이면 "Landlock untested … re-run before choosing this"가,
  Landlock이 seccomp 때문에만 실패했으면 "`landlock_*`을 허용하면 reachable일 때 ①, 아니면 LANDLOCK-ONLY가
  가능"이 붙는다(`grantable`이지만 reachable이 아니면 그 사정까지). userns 안의 새 procfs mount 가능 여부는 커널·런타임에 따라 다르므로 프로브의 실제 시험 결과를 따른다.
- **INCONCLUSIVE**(테스트) — 위 어느 것도 아니고 **판정을 가를 테스트가 답을 못 냈다**: Landlock 자식이 fork 실패·
  SIGSYS 아닌 시그널로 죽음·예외·무보고로 미검증이거나, 네임스페이스 테스트가 미검증인데 실제로 거부된 것은 없다.
  재실행을 권하고(pids/nproc 한도, OOM?) **멈추지 않는다** — F2·CLI 샌드박스 문단은 붙는다. (fork 실패·SIGSYS 아닌
  kill·예외는 어디서든 DENIED가 아니라 `UNKNOWN`이고, seccomp·거부로 읽히는 건 SIGSYS kill뿐이다.)
- **NO PATH** — 그 외(실제 Landlock 실패 **그리고** 실제 네임스페이스 거부). 무엇을 열어야 하는지 우선순위로
  나열한다. Landlock은 원인별로: EPERM(또는 enforce 체인의 `create`/`add_rule`/`restrict`가 EPERM·ENOSYS) =
  seccomp에 `landlock_*` 허용, `KILLED (SIGSYS)` = seccomp가 syscall을 죽임 → 역시 허용, ENOSYS = 커널 미지원
  (< 5.13) **또는** syscall보다 오래된 seccomp 프로파일(Docker ≤ 20.10.17) — LSM 목록에 landlock이 보이면 프로파일
  쪽, 커널이 5.13 미만이면 커널 쪽으로 가르고 그 밖엔 `docker version`으로 확인, EOPNOTSUPP = `lsm=`에 landlock
  추가, ABI 1 = ≥ 5.19 필요. uid 전환이 reachable이면 "uid 분리만으로도 F2·교차 사용자 읽기는 닫힌다"를,
  `grantable`이지만 reachable이 아니면 그 사정과 필요한 것(root 시작, 또는 시작 시 NNP=0인 file-cap 브로커)을
  덧붙인다. bwrap 쪽은
  "OR bwrap (missing: …)"으로 빠진 네임스페이스를 나열하고(미검증은 `<name>?`), mount/새 procfs가 막혔으면
  `apparmor=unconfined`(AppArmor 라벨이 있을 때)와 `systempaths=unconfined`까지 안내한다. 새 procfs만 실패했을 때의
  weaker-nested 안내(`enableWeakerNestedSandbox`/`CLAUDE_SANDBOX_WEAKER_NESTED`)에는 "F2가 열린 채다(게이트웨이가
  non-dumpable이 아니면 sandboxed 명령이 그 environ을 읽는다)"가 붙고, 그 테스트가 돌지 않았으면 이 안내는 없다.
- 판정 블록 뒤의 `!! the gateway holds SETUID/SETGID in its AMBIENT set …` — 게이트웨이의 ambient set에
  SETUID/SETGID가 있으면 NONE·INCONCLUSIVE(신원) 말고는 어느 판정 뒤에든 붙는다(게이트웨이가 exec하는 모든 것이 그
  cap을 물려받는다 — §3).
- 그다음 **INTERIM + Landlock 룰셋 문단**은 Landlock을 쓸 수 있을 때(BEST PATH·LANDLOCK-ONLY)만 붙는다. INTERIM은 Landlock
  단독의 한계(같은-uid 시그널 — ABI ≥ 6이면 signal scope로 닫을 수 있다 — 와 DAC 미격리)를, 룰셋 문단은 §6.3 ①의
  요건을 요약한다: 게이트웨이 코드 디렉터리(`/app`)는 룰셋에서 **뺀다**(규칙 부재 = 읽기·쓰기 거부; ro로 넣으면
  `/app/data` 자격증명이 열린다), 이 uid가 쓰거나 만들 수 있는 site-packages엔 **쓰기를 주지 않는다**, `/proc`(ro)·
  `/dev/urandom`·`/dev/null` 파일 규칙·세션 TMPDIR·패키지 캐시와 ro 규칙의 EXECUTE를 넣는다, 세션 간 loopback TCP는
  TCP 포트 규칙(ABI ≥ 4 — 이 커널에서 되는지 함께 찍는다)이 없으면 열려 있다, 그리고 CLI 샌드박스 상태가 `off`가
  아니면 Landlock 도메인 안에선 bwrap이 못 뜨니 `CLAUDE_SANDBOX_ENABLED=false`와 user/managed 설정의
  `sandbox.enabled`·`failIfUnavailable` 제거가 필요하다(§3). 이 불릿은 CLI 샌드박스 상태별로 끝맺는다: 켜져 있고 둘 다
  있으면 모든 sandboxed Bash 호출이 실패한다, `inert`면 지금은 꺼진 셈이지만 설치하는 순간 실패한다, `fail`이면 이미
  exit 1이다, `unset`이면 지금 켜는 쪽은 없지만 워크스페이스의 project/local 설정이 켤 수 있다(둘이 있으면 그
  워크스페이스의 sandboxed Bash가 실패하고, 하나라도 없으면 그곳에선 inert였다가 설치하는 순간 실패한다).
- 이어서 **F2 문단**은 NONE과 INCONCLUSIVE(신원)를 뺀 모든 판정 뒤에 붙는다(uid 분리 / 새 PID ns + 새 procfs / Landlock
  5.13+, 그리고 `PR_SET_DUMPABLE` 무특권 스톱갭과 그 한계 — "here: non-dumpable = …"; hidepid가 세션 uid:gid에게서
  게이트웨이를 가리면 그렇게 적는다). 게이트웨이가 PID 1이 아니고 PID 1의 environ이 읽히면
  "`!! /proc/1/environ is READABLE here too …`"를 더한다 — 게이트웨이의 `PR_SET_DUMPABLE(0)`이 덮지 못하는 전체
  컨테이너 env다(root 시작으로도 닫힌다는 절은 PID 1이 init shim일 때만 — §1).
- **CLI OS 샌드박스 줄**(`=> CLI OS sandbox can run`)은 NAMESPACES 섹션에서 **매 실행** 찍힌다(NONE 포함). 프로브는
  CLI가 병합하는 설정을 흉내 낸다 — 뒤가 이긴다: user 설정(게이트웨이 `--setting-sources`에 `user`가 있을 때만;
  게이트웨이 기본값은 `project,local`, `docker-compose.yml`은 `user,project,local`) < project/local(워크스페이스마다
  세션이 쓰므로 읽지 않는다) < 게이트웨이의 `--settings`(`CLAUDE_SANDBOX_ENABLED=false`면 `enabled:false`, `true`·
  `WORKSPACE_SANDBOX_ENABLED`·`USER_WORKSPACE_QUOTA_MB>0`이면 `enabled:true`) < managed
  (`/etc/claude-code/managed-settings.json`, 이어서 `managed-settings.d/*.json` 이름 순) — CLI 2.1.283이 스스로 밝히는 순서(낮은 것에서
  높은 것으로 user·project·local·flag·policy)와 같다. 읽은 층은 별도 `CLI SETTINGS` 섹션이 찍는다. 전역 설정 파일은 CLI처럼
  고르고(`.config.json`(legacy)이 있으면 그것, 아니면 `.claude.json` — `CLAUDE_CODE_CUSTOM_OAUTH_URL`이면
  `.claude-custom-oauth.json`) env 블록만 보며, 어느 층의 env 블록이 `CLAUDE_CONFIG_DIR`를 옮기면 user 설정과 이후의
  config-dir 경로도 옮겨진 곳에서 읽는다(쓸 수 없는 값이면 읽지 않고 unverified로 센다). 경로 값은 CLI처럼
  다룬다: `CLAUDE_CONFIG_DIR`는 설정돼 있으면 빈 값까지 손대지 않고 그대로 써서(CLI의 `??`) 빈 값·앞 공백·`~`는
  세션 cwd 기준 상대 경로(=쓸 수 없음)이고 미설정일 때만 `$HOME/.claude`로 가며(전역 설정 파일만은 CLI처럼 빈 값도
  HOME으로 — `||`), `CLAUDE_CODE_PLUGIN_CACHE_DIR`/`SEED_DIR`는 trim 뒤 `~`·`~/…`만 펼친다(`SEED_DIR`는 `:`로 나눠 빈 항목을
  버린다). 프로브는 managed-settings.d를 `*.json` 64개(디렉터리 항목 640개)까지만 읽고, 넘치면 `!!` 줄과 함께 그
  층을 unverified로 남긴다. 상태: `off`(그 소스가 끔 —
  managed `sandbox.enabledPlatforms`의 병합 목록에 이 호스트의 플랫폼 이름 — CLI가 WSL이면 `wsl`, 아니면 `linux`로
  부르는 것 — 이 없을 때 포함) / `unset`(게이트웨이·user·managed 어느 쪽도 안 켬 → 워크스페이스의
  project/local이 결정, 기본 off) / `fail`(켜졌는데 `bwrap`/`socat`이 없고 `sandbox.failIfUnavailable` → CLI가 exit 1,
  모든 세션 실패) / `inert`(켜졌는데 바이너리가 없어 Bash가 샌드박스 없이 돈다) / `broken`(바이너리는 있는데
  userns·pidns·mountns·(weaker nested가 아니면) 새 procfs 중 하나가 거부 → `allowUnsandboxedCommands=false`라 모든
  sandboxed Bash 호출 실패) / `unknown`(그 네임스페이스 테스트가 돌지 않음 — broken으로 단정하지 않는다) / `ok`.
  켠 소스는 "(enabled by …)"로, 못 읽은 설정 파일은 "(unverified: …)"로 덧붙인다. 이 값은 Landlock 도메인
  **밖**에서 잰 것이다(위 룰셋 문단). 판정 뒤의 "CLI OS sandbox: … here" 문단은 `inert`·`fail`·`broken`·`unknown`에만
  붙고, NONE·INCONCLUSIVE(신원)는 그 문단을 내지 않는다.

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
  `plugin_autorefresh`; uninstall/remove는 `claude plugin` CLI가 트리를 다시 쓴다). 그래서 교체되는 clone
  자체에 ro-bind·규칙을 걸면, **살아있는 세션**에 대해 ②의 ro-bind는 **mid-session에 빈 디렉터리**가 되고 ①의
  Landlock 규칙(열린 fd의 inode에 묶인다)은 **respawn 전까지 그 플러그인 파일에 `EACCES`**가 난다. 경로 문자열
  규칙은 없고 살아 있는 도메인을 제자리에서 다시 걸 수도 없다(`landlock_restrict_self`는 호출 스레드에 더 좁은 층만
  더한다 — 다시 거는 길은 respawn뿐). 해법은 규칙·bind를 **교체되지 않는 부모**에 거는 것이다: `prepare_repo`는
  `<clone 루트>/<repo>-<hash>` 자식만 바꾸고 루트는 `mkdir(exist_ok=True)`만 하므로, clone 루트(`clone_root()`)와
  `plugins/`에 건 path-beneath ro 규칙(②는 그 부모의 ro-bind)은 교체된 clone을 계속 보여 준다.
- ⚠️ **공유 목록에서 빠지기 쉬운 경로들**(빠지면 플러그인 스킬·훅이 모든 세션에서 사라진다 — 로컬 검증):
  - **플러그인 clone 루트** `$CLAUDE_PLUGIN_CLONE_ROOT`(미설정 시 `$HOME/.claude/plugin-marketplaces`; 그 아래
    저장소마다 `<repo>-<hash>` 자식이 생기고 `prepare_repo`가 그 자식을 통째로 교체한다 — 규칙은 루트에 건다).
    CLI는 플러그인 스킬의 base dir과 훅의 `CLAUDE_PLUGIN_ROOT`를 **`plugins/cache`가 아니라 이 clone**에서
    해석한다. `plugins/`만 공유하고 clone을 빼면 `Skill` 호출이 "Unknown skill"로 떨어지고 훅이 안 돈다.
  - `plugins/known_marketplaces.json`(`installLocation`·`source.path`), `plugins/installed_plugins.json`
    (`installPath`), `settings.json`의 `extraKnownMarketplaces.*.source.path`에 적힌 **절대 경로 전부**와,
    공유 자산이 **symlink면 그 대상 경로**까지 ro로 닿아야 한다.
  - **구현(PR #244):** Landlock 목록의 공유 항목(가드의 공유 자산 이름, 파일 도구에는 열지 않고 CLI가 직접 읽고 실행하는
    `rules`·`hooks`·`workflows`, `plugin_resource_roots()`, CLI 설정 파일, `CLAUDE_ADD_DIRS`)은 모두 `landlock._GrantFilter`를
    거쳐 **풀린(resolved) 경로**로 들어간다 — 검사한 경로가 곧 허용되는 경로여야 한다(워크스페이스는 따로 검사한다 — §6
    step 3의 구현 노트).
    - 풀린 경로가 `/`이거나 config dir·`$HOME`·워크스페이스 base·`/app`·임시 디렉터리·시스템 루트(`/run`·`/var/run`·`/dev`·
      `/sys`·`/root`·`/var/tmp`)이거나 그것을 품으면, 워크스페이스 base·`/app`·임시 디렉터리·시스템 루트 안이면, config dir
      안의 공유 하위 트리·설정 파일 밖이면, config dir 밖의 `$HOME` 안이면 경고 한 번과 함께 빠진다. `$HOME` 안의 예외는
      전역 설정 파일과 운영자가 정한 `CLAUDE_PLUGIN_CLONE_ROOT`(그 경로 자체가 symlink가 아닐 때) 안뿐이고, 두 예외 모두
      워크스페이스 base·`/app`·시스템 루트 안에서는 듣지 않는다.
    - 겹치는 루트: 같은 디렉터리인 루트들은 모두 적용돼 가장 엄한 쪽이 이긴다(`USER_WORKSPACES_DIR=$HOME`이면 `$HOME` 안이
      모두 빠진다). 그 밖엔 가장 안쪽 루트의 규칙을 따르므로, 워크스페이스 base·`/app`·시스템 루트 안에 둔 config dir나
      `$HOME`은 자기 규칙을 지키고, 임시 디렉터리 안(같은 디렉터리는 아닌)에 둔 `$HOME`은 예외까지 지킨다. `HOME=/`이면
      config dir 밖 항목이 모두 `$HOME` 안으로 잡혀 위 예외 말고는 빠진다(fail closed — 실제 홈 디렉터리가 필요하다).
    - 기본 clone 루트 `<cfg>/plugin-marketplaces`는 config dir 안의 허용 이름일 뿐 예외의 기준이 아니다. symlink면 가리키는
      곳에서 판단하므로 심어 둔 `plugin-marketplaces -> ~/.ssh`는 아무것도 열지 않는다. 기본 clone 루트는 `CLAUDE_CONFIG_DIR`가
      아니라 `$HOME`을 따르므로, `CLAUDE_CONFIG_DIR`를 다른 곳에 두면 `$HOME/.claude/plugin-marketplaces`의 clone은
      `CLAUDE_PLUGIN_CLONE_ROOT`를 설정하지 않는 한 빠진다(§6 step 2의 세션별 config dir에서 함께 챙긴다).
    - 파일 규칙은 inode를 허용하므로 파일 대상(`CLAUDE.md`·`settings*.json`·`.config.json`·`.claude.json`)은 링크가 하나뿐인
      일반 파일이어야 한다 — 다른 사용자의 transcript에 하드 링크한 `CLAUDE.md`가 그 transcript를 모든 세션에 열었다.
    - 런처와 브로커 자식은 게이트웨이가 준 항목(uid 모드는 티켓의 목록과 워크스페이스)을 `/`부터 성분마다 `O_NOFOLLOW`로
      열어, 필터를 지난 뒤 바꿔치기한 링크도 허용하지 않는다: 어느 성분이든 symlink이거나 마지막 앞 성분이 디렉터리가
      아니면 그 항목을 빼고 stderr에 `landlock: not granting '<path>': …`를 남기며, 없는 rw 디렉터리도 같은 식으로 성분마다
      만들고, 링크가 둘 이상인 파일 대상도 뺀다. 자기 경로(`/usr`·`/etc` 같은 시스템 ro 경로, `/dev` 파일, `/dev/shm`, 실제
      CLI 디렉터리, 셸 래퍼)만 링크를 따라 연다(merged-usr의 `/bin`·`/lib` 링크).
    - 실제 CLI 디렉터리는 런처·브로커가 모든 세션에 ro+exec로 연다. 그래서 `check_config`는 그 디렉터리가 `/`·`$HOME`·
      config dir·워크스페이스 base·`/app`·임시 디렉터리·시스템 루트이거나 그것을 품거나 워크스페이스 base 안이면 기동을
      거부하고(`$HOME` 안은 괜찮다 — 네이티브 설치기의 `~/.local/share/claude/versions`), 런처(exit 70)와 브로커 자식
      (exit 114)은 `/`이거나 워크스페이스를 품은 CLI 디렉터리를 다시 거부한다.
    - 그래서 `agents -> .` 같은 링크나 `installLocation: /`, 워크스페이스 base를 품은 `CLAUDE_PLUGIN_CLONE_ROOT`는 모든 세션의
      허용을 넓히지 못하고 그 항목이 빠지며(그 플러그인 스킬·훅이 confined 세션에서 사라진다), `skills -> /opt/shared-skills`
      처럼 안전한 대상으로의 링크는 그대로 열린다. 게이트웨이 자신의 상태를 막는 deny-list일 뿐, 운영자가 다른 곳에 둔
      비밀까지 아는 것은 아니다. 남은 틈: 허용된 디렉터리 **안**에 심은 하드 링크(예: `skills/x.jsonl` → 다른 사용자의
      transcript)는 잡지 못한다 — 같은 파일시스템에 쓰는 비격리 writer가 있어야 심을 수 있다.
- **쓰기 상태만 분리**한다: `projects/`, `plans/`, SDK가 쓰는 cache/history, 워크스페이스, 그리고 아래의
  `plugins/data`.
- ⚠️ **`plugins/` 전체를 읽기 전용으로 두면 모든 플러그인 훅이 깨진다.** CLI 2.1.283은 훅마다
  `<plugins 루트>/data/<plugin-id>`를 `mkdir`하고(EEXIST 외 모든 에러를 rethrow) `CLAUDE_PLUGIN_DATA`로
  넘긴다. 루트는 `$CLAUDE_CODE_PLUGIN_CACHE_DIR`(미설정 시 `<cfg>/plugins`). 읽기 전용이면 훅이
  `EACCES … mkdir … plugins/data`로 실패하는데 **턴은 success로 보고**돼 조용히 깨진다(§5 e2e의 "스킬 인식"
  체크로는 안 잡힘). → `plugins/data`를 **쓰기 가능한 세션 상태**로 분리하거나, `CLAUDE_CODE_PLUGIN_CACHE_DIR`
  (쓰기용)과 `CLAUDE_CODE_PLUGIN_SEED_DIR`(읽기 전용 seed, 경로 구분자 목록)로 공유 seed와 세션별 쓰기를
  나눈다. 세션은 `plugins/cache/.../<ver>/.in_use/<pid>` 마커도 쓴다.
  **구현(PR #244 리뷰 2):** 공유 `plugins/data`를 세션에 rw로 주면(landlock 모드는 모든 세션이 한 uid) 다른 세션의
  훅이 실행하는 플러그인 상태를 심을 수 있어 막았다. CLI의 유일한 노브 `CLAUDE_CODE_PLUGIN_CACHE_DIR`은 data만이
  아니라 plugins 루트 전체(cache·registry·marketplaces)를 옮기므로 세션별 data 디렉터리는 사용자별 config dir 없이는
  불가능하다. 그래서 `plugins/data`는 **읽기 전용**으로 두고, 게이트웨이가 confined spawn마다
  `installed_plugins.json`의 각 플러그인에 대해 `plugins/data/<id>`를 미리 만든다(`landlock._ensure_plugin_data_dirs`;
  CLI의 mkdir가 EEXIST로 통과해 훅이 돈다 — 그 안에 쓰는 훅만 EACCES로 실패). `plugins`나 `plugins/data`가 symlink면
  만들지 않는다(심어 둔 링크가 가리키는 곳에 게이트웨이가 디렉터리를 만들게 된다; 경고 한 번, 미리 있지 않은 플러그인의
  훅은 confined 세션에서 실패한다). 같은 이유로 `~/.npm`·uv 캐시도 공유 rw에서 빼고 세션별
  `<cwd>/.cli-tmp/{npm-cache,uv-cache}`(`npm_config_cache`·`UV_CACHE_DIR`)로 옮겼고, 공유 `<cfg>/shell-snapshots`도
  쓰기 불가로 둔다(모든 세션이 명령마다 자기 스냅숏을 source 한다 — §6 step 3의 구현 노트).
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
- ⚠️ **config dir를 옮기면 게이트웨이의 여러 소비자가 같이 따라가야 한다**(로컬 검증 — 안 고치면 조용히 깨짐):
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
  - ⚠️ **읽기 모델도 반만 따라온다.** `CLAUDE_CONFIG_DIR`를 보는 건 `plugin_service._plugins_root()`뿐이다
    (`/admin/api/plugins`는 `plugin_service.list_plugins`가, `/v1/agent-resources`의 **플러그인** 항목은
    `agent_catalog`가 그것을 거쳐 낸다). 나머지는 `$HOME`만 본다: `agent_catalog`의 user-scope 스킬/에이전트
    (`Path.home()/.claude`), `plugin_admin_service`의 clone 루트와 Refresh·install이 저장소를 찾는
    `known_marketplaces.json`, `claude_settings_env`의 `settings_path()`(`GATEWAY_CLAUDE_SETTINGS_PATH` 또는
    `$HOME/.claude/settings.json` — 그 엔드포인트는 `/admin/api/claude-settings-env`), 샌드박스/guard의
    `_claude_home`. 그래서 공유 트리를 **HOME 밖**으로 옮기면(§5 ①의 `/opt/claude-shared` 권고) 읽기 모델이 그
    경로를 몰라 `/admin/api/plugins`·`/v1/agent-resources`가 **과소보고**한다(세션 자체는 샌드박스 루트만 맞으면
    영향 없음). 좁지만 실재하는 간극이고, 이 소비자들을 하나씩 고치기보다 리졸버 하나로 모으는 편이 낫다(§7 step 2).
- **검증 항목**: CLI가 세션별 config dir에서 공유 스킬/플러그인을 **여전히 인식**하는지 e2e로 확인.
  프로브의 "SHARED ASSET LAYOUT" 섹션이 자산마다 소유권·권한과 함께 **다른 uid가 닿는지**(막는 상위
  디렉터리)와 **안에서 못 쓰는 항목 수**(o+r 없는 파일, o+rx 없는 실행 파일·디렉터리)를 찍으니 리팩터
  범위 산정에 쓴다.

## 6. spawn 설계 (메커니즘 공통 골격)

격리 spawn이 하는 일(①은 `cli_path` 런처 스텁(=§2의 고정·root 소유 런처) + 상주 브로커 — §3 B; ②·Landlock
단독은 런처가 그 자리에서):

1. 사용자 → **숫자 uid** 결정적 매핑. 키는 **`WorkspaceManager`가 쓰는 resolved key와 동일**해야
   한다(한 uid ↔ 한 워크스페이스; `@` 처리·legacy localpart 플래그가 어긋나면 서로 다른 uid가 같은
   워크스페이스를 쓰거나 그 반대가 된다). 충돌 금지(레지스트리로 추적하고 충돌 시 거부). ①에선 게이트웨이가 정해
   티켓에 싣고, 브로커는 그 uid가 세션 uid 대역인지 검증한다(§3 A·B). `user`가 없는
   트래픽(`/v1/agents/messages`의 stateless 런, `/v1/responses`의 `user=None`)은 **각자 단기 uid**를
   받아야 한다(구현: 대역 맨 위의 ephemeral 구간에서 런마다 절대 워크스페이스 경로를 키로 하나를 메모리에서 임대하고
   `WorkspaceManager.cleanup_temp_workspace`가 워크스페이스가 실제로 지워졌을 때 반납한다(지우지 못한 워크스페이스는 uid를
   계속 쥔다). 모든 경로가 그 런의 CLI를 멈춘 뒤에야 워크스페이스를 지우므로 살아 있는 두 런은 uid를 공유하지 않는다 — 남은
   틈은 `/v1/agents/messages`·try-run의 정상 경로 disconnect가 시간을 넘긴 경우(그 경로엔 kill이 없다), 요청이 클라이언트를
   만들기 전에 도는 슬래시 preflight 중에 들어온 삭제, uid 모드에서 kill된 CLI의 프로세스를 브로커가 거두기까지의 약 5초다.
   그 틈에서도 브로커가 다른 워크스페이스에서 살아 있는 uid와 고아가 쥔 uid의 spawn을 거부한다(`_check_uid_free`); 상한은 §8).
   ⚠️ 커널은 숫자 uid `setuid`에 `/etc/passwd` 레코드를 요구하지 않지만 **userland은 요구한다**(§3 — passwd/group 항목
   또는 `nss_wrapper`, allowlist에 `USER`/`LOGNAME`).
2. **세션별 HOME/`CLAUDE_CONFIG_DIR`** 준비: 쓰기 가능한 per-session `.claude`(projects/plans/
   cache/history/`plugins/data`) + 공유 자산(§5의 clone 루트 — 교체되는 `<repo>-<hash>` 자식이 아니라 그 부모 —
   와 절대 경로 포함, `settings.json`은 공유 정책)을 ro로 연결. §5의 config-dir 소비자(transcript 경로·guard
   루트·`--add-dir`)도 함께 이전.
3. 격리 적용:
   - **①**: 순서·cap drop·특권 위치·종료/reaping은 §3(특히 B·D·C·O). CLI 샌드박스는 끈다
     (`CLAUDE_SANDBOX_ENABLED=false` — §3 ⚠️). 룰셋 ro: 시스템 경로·CLI 설치·공유 자산·**`/proc`** / rw: 워크스페이스·per-session `.claude`.
     ⚠️ **ro 집합에도 `LANDLOCK_ACCESS_FS_EXECUTE`를 준다**: Landlock의 읽기 권한은 실행을 포함하지 않아, 빼면 CLI
     `exec`(§3 D의 (7))이 매번 `EACCES`이고, 공유 플러그인 트리에서 빼면 플러그인 훅·런처가 조용히 깨진다(턴은
     success로 보고된다 — §5). ⚠️ **`/proc`(ro)와 `/dev/urandom`(파일 규칙)이 둘 다 있어야 번들 CLI가 뜬다**: 이
     목록에서 둘을 빼고 만든 룰셋에선 `claude -v`가 exec 직후 `panic(main thread): abort() called`(exit 134)로 죽고,
     둘 중 하나만 더해도 같다(로컬 6.18 커널·CLI 2.1.283). `/proc` ro는 F2를 다시 열지 않는다 — 도메인 밖 같은-uid
     프로세스의 `environ`·`maps`는 여전히 `EACCES`이고 `cmdline`·`status`만 읽힌다(로컬 확인; `cmdline`은 아래 5).
     ⚠️ **`/app`은 룰셋에 넣지 않는다**(ro에도): Landlock ro는 **읽기를
     허용**하는 규칙이라 `/app`을 ro로 넣으면 그 아래 `/app/data`의 자격증명 오버레이(`gateway-mcp.json`,
     `gateway-mcp-plugin-overlay.json`, `gateway-claude-settings-env.json`, 프롬프트 배포 로그, usage db —
     `./data:/app/data`로 bind; 모드도 제각각 — 앞의 세 JSON 오버레이는 mkstemp라 0600, 프롬프트 배포 로그(append)와
     usage sqlite는 0644라, 같은 uid엔 어느 쪽도 무의미하고 0644는 오히려 더 노출)까지 **모든 세션이 읽게 된다.**
     CLI는 `/app` 아래
     아무것도 필요 없으므로 **규칙 부재로 읽기·쓰기를 모두 거부**하는 게 맞다(§1 — 코드 덮어쓰기 차단은 ro가
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
     따라오게)해 rw 규칙/kept env에 넣지 않으면 `uv run`·`npx`가 `EACCES`로 깨진다. `/dev/shm`이 rw로 없으면 Python
     `multiprocessing.Lock()` 같은 POSIX 세마포어가 `PermissionError`다(로컬 확인). 공유 `/dev/shm`을 rw로 주면
     Landlock-only(모두 uid 1000)에선 `/tmp`처럼 세션 간 통로가 된다(POSIX shm/세마포어 경로는 `/dev/shm` 바로
     아래로 고정이라 Landlock만으론 세션별로 가를 수 없다).
     **구현(PR #244):**
     - **워크스페이스**는 confined spawn마다 `landlock._confined_workspace`가 검사한다 — 런처가 커널 cwd를 rw로 허용하므로,
       워크스페이스 자리에 심은 `<base>/alice/claude -> ~/.claude` 링크가 그 사용자 세션에 config dir 전체를 rw로 열었다.
       워크스페이스 base 안이면 정확히 `realpath(base)/<rel>`로 풀려야 하고(base 아래 어디에도 symlink가 없어야 한다; base
       자체의 링크는 괜찮다), 게이트웨이 소유 preflight 디렉터리는 게이트웨이 uid 소유의 실제 디렉터리여야 하며, 그 밖의 cwd는
       symlink나 디렉터리 아닌 것이면 안 된다. 어느 쪽이든 `/`이거나 §5 구현 노트의 루트(config dir·`$HOME`·워크스페이스
       base·`/app`·임시 디렉터리·시스템 루트)이거나 그것을 품을 수 없고, base 밖의 cwd는 config dir·워크스페이스 base·`/app`·
       시스템 루트 안일 수 없다. 아니면 `WorkspaceRefused`로 spawn하지 않는다: `/v1/responses`는 재시도로 풀리지 않는
       HTTP 500 `workspace_refused`(메시지엔 경로가 없고 경로와 이유는 로그에 남는다), stateless 런은 `api_error` 이벤트다. 풀린 워크스페이스가 첫 rw 항목이자 uid 티켓의 워크스페이스이고, 런처는 커널
       cwd(`os.getcwd()`)가 rw 항목 중 하나와 정확히 같지 않으면(rw 목록이 비어도) exit 70, 브로커 자식은 chdir 뒤 cwd가 티켓
       워크스페이스와 다르면 exit 113으로 거부한다.
     - **rw**는 워크스페이스, 자기 `projects/<name>`(풀린 cwd로 CLI가 짓는 이름 하나), `<cfg>/session-env/<session id>`와
       `<cfg>/tasks/<session id>`(CLI의 Task 목록), `/dev/shm`뿐이다. 워크스페이스 밖의 rw 항목은 정확히 그 경로로 풀려야
       하고(그 자리나 위에 symlink가 있으면 거부), 이미 있는 것이 디렉터리가 아니면 거부한다. 두 세션 디렉터리의 형제는
       다른 세션의 `$CLAUDE_ENV_FILE` 스크립트(CLI가 Bash에 source 한다)와 Task 목록이라 부모는 열지 않는다. landlock
       모드는 게이트웨이가 둘을 0700으로 미리 만들고, uid 모드는 브로커 자식이 세션 uid로 만든다(게이트웨이 소유 0700은
       U가 쓰지 못하므로 부모가 U에게 쓰기 가능해야 한다 — §3 리뷰 3 노트).
       confined CLI는 이것들을 치우지 못하므로 게이트웨이가 세션이 **끝났을 때만** 지운다: 명시적 삭제, 익명 세션의 모든
       teardown, stateless 정리. named 세션이 만료·축출되거나 게이트웨이가 내려갈 때는 남긴다 — 이어지는 요청이 같은 id로
       재개하고, 그때 SessionStart는 `source=resume`으로만 돌아 시작 훅이 그 env를 다시 만들지 않는다. 같은 id를 쥔 살아 있는
       세션이 있으면 지우지 않고(`SessionManager._release_session_dirs`), 재개되지 않은 named 세션의 디렉터리는 남는다.
       `tasks/<id>`는 confined 모드에서만 지운다(격리가 꺼져 있으면 CLI 자신의 것이다).
     - **정리 순서:** 세션의 클라이언트를 만드는 동안(`begin_client_start` → `create_client` → `finish_client_start`)
       teardown(삭제·만료·종료)은 워크스페이스·uid 임대·세션 디렉터리 제거를 그 클라이언트를 멈출 때까지 미루고, 축출은 그
       세션을 건너뛰며, 그 사이 끝난 세션을 만들던 요청은 409 `session_ended`를 받는다. 세션 매니저의 모든 teardown은 disconnect
       상한을 넘겨 살아남은 CLI를 SIGKILL한 뒤에야 무엇이든 지우고(`_stop_client`), `verify()`는 질의를 닫은 뒤 워크스페이스를
       지운다. 첫 턴이 거부된 새 세션(예: 슬래시 400)은 바로 지우고, 이어지는 요청의 세션은 남긴다.
     - **목록 형식:** `options.env`의 `GATEWAY_LANDLOCK_RO`/`GATEWAY_LANDLOCK_RW`는 절대 경로의 JSON 배열이다. 경로에 `:`가
       들어갈 수 있어서다(`:`로 이은 목록은 `.../x:/secret`을 `/secret` 허용으로 바꿨다). 그 밖의 형태면 런처가 exit 70으로
       거부한다. uid 모드에선 목록이 서명된 티켓 안에만 실린다.
     - **세션 TMPDIR** `<cwd>/.cli-tmp`는 규칙이 아니다: cwd 아래라 이미 허용되고, 에이전트가 바꿀 수 있는 그 경로에 규칙을
       걸면 `ln -s / .cli-tmp`가 탈출이 된다(`_add_rule`은 `O_PATH`로 링크를 따라간다). 그 자리가 쓸 수 있는 디렉터리가
       아니면 spawn 때 다시 만들거나 고치고(§3 리뷰 3 노트), 못 하면 spawn이 바로 실패한다(70/118). 수리는 워크스페이스 fd
       기준으로 하고 링크를 따라가지 않으며, 내보내는 TMPDIR은 실제로 고친 그 디렉터리다. 링크를 계속 바꿔 심는 경쟁자를 만나면
       8번 만에 fail closed한다 — landlock 모드에선 세션의 분리된 자손을 아무도 치우지 않으므로, 에이전트가 띄운 경쟁자가 자기
       워크스페이스의 이후 spawn을 계속 실패시킬 수 있다(uid 모드는 CLI가 끝나면 supervisor가 자손을 모두 치운다). 운영자의
       `CLAUDE_CODE_TMPDIR`는 landlock 모드에서 워크스페이스 바로 아래 한 항목(`tmp`·`./tmp`·`<ws>/tmp`)일 때만 따르고, 그 밖의
       값과 uid 모드에선 `.cli-tmp`다.
     - **캐시:** 두 캐시는 세션 쓰기 경로로 재지정했다 — 런처/브로커가 `npm_config_cache`·`NPM_CONFIG_CACHE`·
       `UV_CACHE_DIR`을 `<cwd>/.cli-tmp/{npm-cache,uv-cache}`로 덮어쓰고, 공유 `~/.npm`·uv 캐시는 rw 규칙에서 뺐다
       (워크스페이스마다 새로 받는다).
     - **git:** `GIT_CONFIG_GLOBAL=<cwd>/.cli-tmp/gitconfig`(`core.excludesFile`·`core.attributesFile`을 없는 tmp 경로로
       돌린다; 물려받은 값도 덮는다). Landlock은 `access(2)`를 중재하지 않아, git이 거부된 `~/.gitconfig`나
       `~/.config/git/ignore`를 읽을 수 있다고 보고 열다가 exit 128로 죽었다. 그 파일은 읽을 수 있고 비어 있지 않은 일반
       파일이며 게이트웨이의 `excludesFile` 줄을 그대로 품는 동안만 그대로 둔다 — 그래서 세션 자신의 `git config --global`
       변경이 남는다. 그 밖의 것(디렉터리, 빈 파일, 읽을 수 없는 파일)은 `gitconfig.broken-<n>`으로 옮기고 새 파일을 임시
       이름 + rename으로 원자적으로 쓴다(그 자리의 디렉터리 하나가 그 워크스페이스의 이후 모든 세션에서 git을 exit 128로
       만들었다). 운영 전역 git 설정은 `/etc/gitconfig`(ro)에 두되 위 두 키는 세션 파일이 덮는다(게이트웨이 env의 운영자
       `GIT_CONFIG_COUNT`/`GIT_CONFIG_KEY_<n>`/`GIT_CONFIG_VALUE_<n>`은 그래도 이긴다). 남은 틈: CLI 내부 git 도우미는
       `GIT_CONFIG_GLOBAL=/dev/null`로 고정해 기본 excludes 파일을 여전히 읽는다.
     - **세션 셸:** 공유 `<cfg>/shell-snapshots`는 일부러 쓰기 불가로 둔다(모든 세션이 명령마다 자기 스냅숏을 source
       한다). 스냅숏이 없으면 CLI는 모든 Bash 호출을 `bash -c -l <cmd>`로 돌려, `/etc/profile`이 PATH를 되돌리고 거부된
       `~/.profile`이 결과마다 `Permission denied`를 남겼다. 그래서 CLI가 bash를 고를 때는
       `CLAUDE_CODE_SHELL=claude-session-bash`(root 소유, 이미지의 `/usr/local/bin`; 파일 자체가 ro 목록에 있다)로 두고, 이
       래퍼가 맨 앞 `-c -l`을 `--norc -c`로 바꿔 CLI가 골랐을 bash를 exec한다(Debian bash는 env가 ssh처럼 보이면 login이
       아닌 `bash -c`에서도 거부된 `~/.bashrc`를 읽는다; 다른 argv는 그대로이고 zsh는 손대지 않는다). 남은 틈: 플러그인
       `bin/`의 PATH 추가와 CLI의 pkill 자기 보호는 스냅숏으로만 들어와 confined 세션엔 없다.
     - **`/dev/shm`**은 multiprocessing 때문에 rw로 남겼고, landlock 모드에선 같은 uid 세션끼리의 공유 면으로 문서화한다
       (uid 모드는 DAC로 갈린다).
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
     레이어로(§8) — 공유 net ns라 세션 간 loopback도 열려 있다(§3). ②는 userns+mount 외에 **AppArmor
     `docker-default`(deny mount)** 때문에 `apparmor=unconfined`/커스텀 프로파일이 추가로 필요할 수 있고(프로브가
     라벨을 찍는다), 새 procfs(`--proc /proc`)가 Docker의 masked `/proc` 경로 때문에 거부되는 런타임이면
     `systempaths=unconfined`도 필요하다(프로브가 시험하고 안내한다 — 컨테이너 전체의 `/proc`·`/sys` 마스킹을 푼다).
4. **env는 allowlist로 구성한다 — 넘기는 것은 아래 넷의 합집합이고 나머지는 전부 버린다.** (가) **게이트웨이가
   세팅하는 변수**: 손 목록이 아니라 각 세팅 지점에서 생성한다(SDK가 `os.environ`과 `options.env`를 한 env로
   합치므로, 손 목록은 의도한 값과 샌 값을 구분 못 한다). CLI가 필요로 하는 것(`ANTHROPIC_*`, `PATH`/`HOME`/로케일,
   `USER`/`LOGNAME`)과 게이트웨이가 자식에 심는 제어 변수가 여기 든다. (나) **리뷰된 운영자 노브 목록**: 운영자가
   env로 주지만 게이트웨이가 세팅도 읽지도 않는 CLI 변수는 (가)로는 **조용히 떨어지므로** 명시적으로 리뷰한 이름
   목록으로 넘긴다. 예: `BASH_DEFAULT_TIMEOUT_MS`(게이트웨이는 ceiling 계산에 `BASH_MAX_TIMEOUT_MS`만 읽고 per-call
   기본값인 이 변수는 안 본다)가 빠지면 자식 Bash 기본 타임아웃이 CLI 기본값 120000ms로 되돌아가고, `DISABLE_*`처럼
   **'미설정=켜짐'**인 노브는 빠지는 순간 그 기능이 도로 켜진다. (다) **런타임 `METADATA_ENV_ALLOWLIST` 키**(운영자
   지정 — `THREAD_ID`·`ORCHESTRATOR_URL` 등; 이름이 설정에서 오므로 그 설정을 읽어 넣는다). (라) **반드시 넘길
   목록**: `CLAUDE_CODE_HARBOR_KITE=0`(**빈 값·미설정이 곧 ON**이라 — `src/constants.py`가 `=0`을 박아 두어야 꺼진다 —
   빠지면 교차 세션 메시징이 다시 열린다: "자연히 사라진다"가 아니라 **그 반대다**), `MCP_TOOL_TIMEOUT`,
   `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS`, SDK가 spawn 때 넣는 `CLAUDE_CODE_ENTRYPOINT`/`CLAUDE_AGENT_SDK_VERSION`/
   `CLAUDE_CODE_SDK_READS_SESSION_STATE`(쓸 때는 checkpointing 플래그와 `TRACEPARENT`/`TRACESTATE`도), loopback
   `NO_PROXY` 항목, 그리고 빠지기 쉬운 것들: `BASH_MAX_TIMEOUT_MS`(게이트웨이가 자기 copy로 `TOOL_STALL_TIMEOUT`·기동
   순서 검사를 계산하므로 빠지면 자식이 600000ms로 되돌아가 검사가 실제 자식을 설명 못 함), admin이 덮는
   `CLAUDE_CODE_AUTO_COMPACT_WINDOW`, cli auth 모드의 `CLAUDE_CODE_OAUTH_TOKEN`, 배포가 런타임 프록시를 쓰면
   `HTTP(S)_PROXY`. ①에선 이 allowlist(이름)가 티켓에 실리고, 값은 스텁이 넘긴 SDK env에서 브로커가 거른다(§3 B) —
   값까지 티켓에 실으면 SDK가 spawn 때 넣는 변수가 빠진다.
   ⚠️ **`ANTHROPIC_AUTH_TOKEN`은 상류 공유 자격증명인데 `ANTHROPIC_*`에 묶여 그대로 자식에 간다**(§1).
   설계 제약: allowlist는 게이트웨이 전용 비밀을 넘기지 않아야 하지만, 이 토큰은 CLI 인증에 필요해 그 규칙만으로는
   빠지지 않는다 — 자식은 이 토큰으로 게이트웨이 회계 밖에서 상류를 직접 호출할 수 있다. 완화책을 명시해야
   한다: 사용자별 토큰, 또는 토큰을 주입하는 프록시 뒤에 두기. CLI의 `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB`=on은
   **①·Landlock 단독에선 쓸 수 없다**: 이 모드는 bwrap을 요구하고(없으면 CLI가 "bubblewrap is required for
   subprocess env scrubbing"으로 거부) 모든 Bash 호출을 bwrap 샌드박스로 강제하며 샌드박스 없는 폴백이 없는데,
   Landlock 도메인 안에선 bwrap이 뜨지 못하고(§3 ⚠️) 기본 seccomp에선 userns부터 막힌다(§1). 켜면 scrub이 아니라
   Bash 실패가 된다(또 권한 모드를 `default`로 강제한다). 단순 allowlist로는 못 지운다.
   보완(선택): 게이트웨이 전용 비밀을 기동 때 한 번 읽어 쥐고 `os.environ`에서 빼는 **단일 지점**을 두면, 런처를
   타지 않는 spawn 경로(`plugin_admin`의 `git`/`claude`, app-server, frozen 백엔드)까지 한 번에 줄어든다(슬래시
   preflight는 이제 세션과 같은 비밀 공란 `client.child_secret_env()`를 `options.env`로 받는다). 단 이것은 denylist라
   비밀을 빠짐없이 나열해야 하고(`USER_API_KEYS`, `USAGE_LOG_DB_URL`, 호출마다 다시 읽는 `CLAUDE_PLUGIN_GIT_TOKEN` 등), 뺀 값도 `/proc/<게이트웨이>/environ`(exec 시점의 블록)엔 남는다(로컬 확인 —
   그래서 #232가 여전히 필요하다). 세션 allowlist를 대신하지 못한다.
5. **argv 노출을 설계 단계에서 닫는다.** `/proc/<pid>/cmdline`은 누구나 읽어 ①에서도 교차 테넌트로 샌다
   (로컬 검증: Landlock 도메인의 다른 uid가 피해자 cmdline을 읽음). 거기엔 `--mcp-config` JSON(세션의
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
   - ⚠️ **`--json-schema`엔 파일 형태가 없다.** `/v1/responses`의 `text.format` json_schema는 caller의 스키마를 그대로
     `options.output_format`으로 넘기고(`responses.py`의 `_response_output_format` → `client.py`) SDK가
     `--json-schema <json>`으로 argv에 싣는다 — create 때 정해져 세션 내내 `/proc/<pid>/cmdline`에 남는다. CLI
     2.1.283엔 파일 형태 플래그가 없고(`--json-schema <schema>`뿐), 대신 stdin `initialize` 요청의 `jsonSchema`를
     받지만 핀된 SDK는 그 필드를 보내지 않는다(SDK 패치나 커스텀 transport가 있어야 한다). 선택지: 그 경로를 만들거나,
     격리 아래선 json_schema를 거부·제한하거나, 이 누출을 수용한다(§8).
   `hidepid`는 가능하면 추가 방어로만.

## 7. 단계별 계획

1. **(#231)** 역량 프로브 + 본 설계 문서. prod에서 프로브 실행 → 메커니즘 확정.
2. **세션별 HOME 리팩터** (공유 ro 자산 보존). 가장 공유-스킬 민감한 부분 — 먼저, 독립적으로. 지금 config/plugin
   루트는 아홉 곳이 서로 다른 우선순위로 따로 푼다(`session_manager`의 `_PROJECTS_ROOT`, `workspace_sandbox`의
   `_claude_home`, `plugin_service`의 `_plugins_root`와 clone anchor, `plugin_admin_service`의 `clone_root`와
   `known_marketplaces.json` 조회, `docker/install_plugins.py`의 `clone_root`, `claude_settings_env`의 `settings_path`,
   `agent_catalog`의 user scope). 목록을 하나씩 고치지 말고, CLI의 우선순위(`CLAUDE_CONFIG_DIR` → `$HOME/.claude`;
   플러그인 루트는 `CLAUDE_CODE_PLUGIN_CACHE_DIR`/`SEED_DIR`; clone 루트)를 따르는 **리졸버 하나**를 두고 모든 소비자가
   그것을 부르게 한다. §5의 config-dir 소비자(transcript 경로·guard 루트·`--add-dir`)와 `settings.json`/`plugins/data`
   writer도 그 리졸버 위에서 함께 이전·수정.
3. **`cli_path` 고정 런처 스텁 + 가둔 상주 브로커** (① uid+Landlock; ②·Landlock 단독은 특권 없이 런처가 그 자리에서).
   특권 배치는 §3 B·A: SDK의 파이프를 넘기는 무특권 스텁, spawn·종료·정리·reaping을 모두 맡는 브로커, 게이트웨이의
   제어 채널 점유 + spawn별 1회용 티켓(§2의 신원 전달 채널과 "세션 아닌 spawn" 처리를 겸한다), 게이트웨이 NNP,
   `CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK`, SDK엔 중립 cwd(§3 cwd 벽), ①·Landlock 단독의 `CLAUDE_SANDBOX_ENABLED=false`.
   ⚠️ **이 단계는 step 4(워크스페이스 uid 소유권·정리)와 한 묶음으로 가야 ①이 성립한다**: uid만 전환하고(여기)
   소유권·정리(step 4)를 미루면 uid-U CLI가 1000 소유 워크스페이스에 못 쓰고 게이트웨이가 그 산출물을 못 지운다.
   또 종료·정리 순서(§3 C — 스텁이 먼저 죽어도 브로커가 kill → reap → 삭제)를 여기서 함께 넣어야 `agent_messages`의
   teardown이 살아 있는 CLI의 cwd를 지우지 않으므로, step 3을 '스폰만' 넣고 끝내선 안 된다.
4. **(step 3과 한 묶음) 워크스페이스 uid 소유권** + `/app` 쓰기 차단(§6.3 — ro가 아니라 **룰셋 부재**/DAC) +
   `/proc` 하드닝. ⚠️ 세션이 만든 파일이 uid U 소유가 되면 **uid 1000 게이트웨이가 못 지우고 못 들어간다**:
   `workspace_manager.cleanup_temp_workspace`(rmtree, `ignore_errors=True`라 조용히 실패)와
   `agent_messages._cleanup_sdk_transcript`(경고는 남긴다)가 stateless 정리 보장을 깨고, 메모리에 없는 세션의
   transcript rehydrate는 0600 transcript를 못 읽으며(지금은 `TranscriptUnreadable` → HTTP 500
   `transcript_unreadable`), 업로드·`/files` 라우트는 U 소유 디렉터리에서 EACCES가 나며, **spawn 자체**도 걸린다: SDK가
   게이트웨이 uid로 `options.cwd`에
   `chdir`하고(§3 cwd 벽), 그 직전 게이트웨이의 `materialize_workspace_resources(cwd)`는 워크스페이스를 탐색조차 못
   하면 `Path.is_dir()`의 `PermissionError`를 그대로 올려 세션 생성을 실패시키고(로컬 CPython 3.12·3.13 확인 — `is_dir`은
   `EACCES`를 다시 올리고 `ENOENT`·`ENOTDIR`·`EBADF`·`ELOOP`만 삼킨다), 탐색은 되고 쓰기만 막히면 `OSError`를 경고로
   삼킨 채 mirror 갱신을 건너뛴다. 그래서 **U 소유 파일 상태의 읽기(transcript 포함)·쓰기(업로드·mirror 갱신)·정리**는
   §3대로 브로커가 U로 전환한 단명 자식으로 대행하거나 의도한 group 권한으로 연다(`CAP_CHOWN`은 소유권 반환이 필요할
   때만) — 삭제는 브로커가 그 세션의 CLI를 죽이고 거둔 **뒤에만** 한다(§3 C). 무특권 스톱갭 `PR_SET_DUMPABLE`(#232)과
   entrypoint 하드닝(#233)은 이 단계와 독립적으로 이미 들어가 있다.
5. **검증**: 2-사용자 파일 격리 테스트, 세션 uid가 게이트웨이의 environ **그리고 다른 세션 CLI 자식의
   environ**을 못 읽는지 확인(#232만으로는 앞의 것만 통과한다), **두 세션 간 loopback**(세션 A가 띄운 loopback
   리스너에 세션 B가 닿는지 — §3), ①·Landlock 단독에서 CLI 샌드박스가 꺼진 채 Bash가 도는지(§3 ⚠️), `-v` spawn이
   없고 티켓 없는 spawn이 거부되는지, 스텁을 SIGKILL했을 때 브로커가 CLI를 죽이고 거둔 **뒤에** 정리하는지, 브로커를
   SIGKILL했을 때 실행 중 CLI 트리가 함께 죽는지(§3 C), (Landlock 단독이면) 두 세션 간 pathname unix socket(§3),
   **스킬 공유 유지** e2e, 플러그인 훅 실행(= `plugins/data` 쓰기) 확인, 기존 게이트웨이 스위트.
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
  site-packages 행 포함 — user site는 아직 없어도 이 uid가 만들 수 있으면 `CREATABLE`)이 찍는다.
- `/proc` `hidepid` 리마운트가 이 컨테이너에서 가능한가(CAP_SYS_ADMIN 필요 — 권장 경로는 아님, §6.5).
- **(설계 결정 — 프로브가 답하지 않음) 브로커 토폴로지의 잔여 위험(§3 A·B·C).** (1) 상주 특권 프로세스 자체:
  SETUID/SETGID만 쥔 전용 비root uid로 둘지 root + `SECBIT_NOROOT_LOCKED` + `set*id` 인자 필터 seccomp로 가둘지,
  그래도 남는 위험을 수용할지(A). (2) 프로토콜 복잡도(fd 전달·티켓·시그널 중계·종료 미러링)와 가용성: 브로커가
  죽으면 spawn은 fail-closed이고 실행 중 CLI도 함께 죽여야 한다 — 감시·재기동 정책. (3) 티켓 노출: 스텁 environ의
  티켓은 같은 게이트웨이 uid 프로세스(app-server·`plugin_admin` 자식 등)에 보일 수 있다 — non-dumpable 스텁 +
  1회용으로 줄일 뿐이다(세션은 다른 uid라 못 읽는다; 브로커가 접속한 스텁의 `SO_PEERCRED` pid가 게이트웨이의
  자식인지 더 볼 수 있다). (4) 사용자별 uid에 같은 사용자의 세션이 동시에 여럿이면 U 단위가 아니라 **세션 단위**
  트리 kill이 필요하다(브로커가 세션별 CLI 트리를 따로 추적). (in-place file-cap 래퍼 대안을 택하면 §3의 N·종료
  `EPERM`·reaping(`init: true`)·secure exec 비용을 대신 떠안는다.)
- uid 할당 대역과 수명(생성·회수), 동시 사용자 상한. 키는 `WorkspaceManager` resolved key와 일치(§6.1). 지금은
  named 사용자의 영속 uid를 회수하지 않아(후속) 안정 대역이 차면 새 named 사용자가 HTTP 500 `uid_band_exhausted`를
  받고(재시도로 풀리지 않는다; 90%를 넘으면 경고), 슬래시/스킬 preflight는 그 사용자에게 uid가 생기기 전엔 게이트웨이
  uid로 landlock 형태로 돌아 400으로 끝나는 요청이 uid를 쓰지 않는다. 익명 런의 임대(§6.1)는 그 런의 워크스페이스를
  정리할 때까지라 `CLAUDE_ISOLATION_EPHEMERAL_UIDS`가 동시 익명 런(stateless 호출, TTL 동안의 익명 세션, try-run,
  기동 프로브·`verify()`)의 상한이다(첫 턴이 거부된 새 세션은 바로 지워 TTL 동안 임대를 쥐지 않는다) — 다 차면 워크스페이스가 사라진 임대부터 회수하고, 그래도 없으면 재시도 가능한
  503(`/v1/agents/messages`는 `api_error` 이벤트)이다.
- 격리 단위: **사용자(테넌트)별**이 기본으로 충분(같은 사용자의 다른 세션은 기밀성 위협이 아님).
  단 `user`가 없는 stateless/익명 런은 각자 단기 uid(§6.1).
- **resume와의 상호작용**: `/v1/responses`의 세션 재개가 턴을 넘어 **같은 uid/HOME로** 매핑되어야
  한다(세션 매니저 ↔ uid 매핑의 안정성; transcript 경로 이전과 함께 — §5). 지금은 메모리에 없는 uid 모드 세션을 잇지
  못한다: 게이트웨이가 세션 uid 소유 0600 transcript를 읽지 못해 재시도로 풀리지 않는 HTTP 500 `transcript_unreadable`이고,
  브로커의 대행 읽기가 후속이다(§7 step 4).
- `BACKENDS` 값: codex/opencode가 켜져 있으면 그 spawn 경로는 런처 밖이다(§2).
- prod가 init shim(compose `init: true`/`--init`)을 쓰는가 — 쓰면 PID 1은 docker-init이고 F2 대상과 브로커
  배치 전제가 게이트웨이 자식 기준으로 바뀐다(§1·§4).
- prod가 compose `user:`로 비root 시작하는가 — 그러면 브로커를 fork할 root 단계가 없어 브로커 바이너리에 file cap을
  주고 컨테이너를 NNP=0으로 시작해야 한다(§3 B; 프로브는 PID 1이 init shim일 때만 이를 가리고, 그 shim이 비root인데 시작부터 NNP=1이면 uid 분리를 권하지
  않는다, §4).
- **(설계 결정) `--json-schema` argv 누출**(§6.5): stdin `initialize`의 `jsonSchema` 경로를 만들지(SDK 패치·커스텀
  transport), 격리 아래선 json_schema를 거부·제한할지, 수용할지.
- 세션이 loopback 리스너를 띄우는가 — 띄운다면 세션 간 loopback을 Landlock ABI 4(커널 6.7+) 포트 규칙으로 막을지(§3).
- 네트워크 egress 통제(①은 egress를 다루지 않음 — 별도 레이어 / #173의 zero-egress 게이트).
