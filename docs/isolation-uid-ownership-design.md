# uid 모드: 워크스페이스 소유권 + 사용자별 config dir (설계)

> **상태: 설계. 구현 전.** 이 문서는 `docs/security-process-isolation.md` §7 step 2(사용자별 HOME/config dir)와
> step 4(워크스페이스 uid 소유권·정리)를 **한 묶음으로** 상세화한다. step 3의 런처·브로커는 #244로 이미 들어왔고
> 이 문서는 그 위에 얹는다. `CLAUDE_ISOLATION_MODE=uid`는 이 둘이 들어오기 전에는 출하 이미지에서 턴을 하나도
> 돌리지 못한다(기동 프로브가 거부). 상위 설계·위협 모델은 `security-process-isolation.md`를, 앱 계층 격리는
> `security-user-isolation.md`를 보라.
>
> 아래의 사실은 2026-10-15에 로컬에서 실측했다(이미지 `omg-landlock:6aa5496`, 커널 6.18, Landlock ABI 7,
> Docker 29.1.3; 번들 CLI 2.1.283 / SDK 0.2.160). 실측은 "실측"으로, 코드 정적 분석은 "코드"로 표기한다.
> prod 컨테이너의 커널·seccomp·마운트·umask는 이 dev 머신과 무관하므로, 프로비저닝 수치는 배포 컨테이너에서
> `scripts/isolation_probe.py`로 재확인한다.

---

## 0. 왜 둘을 함께 하는가 (요약)

uid 모드는 세션 CLI를 세션 uid `U`로 띄운다. 그러면 두 가지가 동시에 깨진다.

1. **워크스페이스.** 지금 워크스페이스(`<base>/<user>/claude` 또는 `_tmp_*`)는 게이트웨이(app uid)가
   `mkdir(parents=True)`로 만들어 app 소유 0755다(코드: `workspace_manager.py:299`). `U`는 여기 쓰지 못해 CLI가
   `<ws>/.cli-tmp`를 못 만들고 exit 1(브로커 자식 exit 118)이다 — 모든 턴 실패(실측, review 3).
2. **config dir.** CLI는 HOME `/home/app`(0700) 아래 `~/.claude`에서 설정·스킬·플러그인·`projects`·`session-env`·
   `tasks`·`plugins/data`를 읽고 쓴다. `/home/app`이 0700이라 `U`는 그 아래 어디에도 닿지 못하고
   (실측), 공유 `~/.claude`는 app 소유라 `U`가 `projects/<name>`·`session-env/<sid>`를 쓰지도 못한다.

둘은 **서로의 전제**다. 워크스페이스만 `U`가 쓰게 하면(소유권) 게이트웨이가 익명 워크스페이스를 지우지 못해
`CLAUDE_ISOLATION_EPHEMERAL_UIDS`번의 런 뒤 익명 요청이 전부 503이 되고(정리 대행 필요), config dir를 그대로
두면 CLI가 `projects`·`session-env`를 못 써 세션이 아예 안 뜬다. 그래서 **워크스페이스 소유권 + 정리 대행 +
사용자별 config dir**를 하나의 작업으로 설계한다.

부수 효과로 기존 결함 하나가 닫히고, 기동 정리의 주체가 바뀐다.
- **사용자명 → `projects/` 디렉터리 충돌**(별도 HIGH, `security-process-isolation.md`의 username collision):
  사용자별 config dir면 서로 다른 uid의 `projects`가 구조적으로 분리된다(코드). 한 uid 안에서 여러 식별자가
  한 이름으로 접히는 것은 같은 사용자라 기밀성 위협이 아니다.
- **공유 config dir의 기동 정리**: CLI의 기동 정리 작업(§4.5)은 공유 dir에서 **다른 사용자의 30일 지난
  transcript까지 지운다**(실측). 모두에게 같은 보존 기간이 적용된 결과라 기밀성 문제는 아니다. 사용자별 dir에선
  각 `U`가 자기 것만 지우는 대신, CLI를 다시 띄우지 않는 사용자의 상태가 만료되지 않아 보존을 게이트웨이가
  맡는다(§4.5).

---

## 1. 결정 요약 (먼저)

| 항목 | 결정 | 근거 |
|---|---|---|
| 워크스페이스 소유권 | **설계 α**: 워크스페이스 루트는 **게이트웨이(app) 소유**로 두고, spawn마다 세션 uid `U`에 **POSIX 기본 ACL**을 걸어 쓰기를 연다. 기본 ACL에 **app uid의 named 엔트리**(`d:u:<APP_UID>:rwx`)를 함께 넣어, `U`가 평범하게 만든 파일·디렉터리(기본 mode 0666/0777)를 app도 계속 읽고 쓴다. | ACL 실측(§3.1–3.2): app 소유 루트 + `u:U:rwx` + `d:u:U`·`d:u:<APP_UID>` default ACL이 overlay·volume·bind 모두에서 동작. app 엔트리가 없으면 app은 `U`가 만든 모든 것에서 막힌다(실측). 게이트웨이가 루트를 계속 소유하므로 매 spawn에 ACL을 다시 걸 수 있고, 기존 app-uid 경로(파일 API·quota·이미지·미러)가 흔한 경우 그대로 돈다. |
| `U`가 0600/0700으로 잠근 파일·transcript | **브로커 정리 대행**(새 `cleanup` op, §3.3) + **transcript 읽기 대행**(§3.6). | ACL mask는 생성 mode의 group 비트로 접혀, `U`의 0600/0700 생성물은 app이 못 연다(실측 §3.2). app은 `U` 소유 트리를 rmtree하지 못한다(실측). rehydrate는 transcript **내용**을 읽으므로(코드) 삭제 대행만으로는 resume이 살아나지 않는다. |
| config dir | **사용자별 `CLAUDE_CONFIG_DIR=P`**. `P`는 app 소유, `U`에 **traverse+read**만 주고 쓰기는 지정 하위(`projects/<name>`·`session-env`·`tasks`·`plugins/data`, `.claude.json` 대상 — §4.1)에만. 공유 자산은 `P` 안의 **symlink로 공유 트리 `S`**를 가리킨다. | CLI 쓰기 인벤토리 실측(§5): `CLAUDE_CONFIG_DIR=P`면 상태가 `P`에만 쌓이고, 쓰기 불가 `P`에서 그 셋만 열어도 세션·resume이 정상(실험은 CLI uid 소유 0500 `P`). 공유 링크 레이아웃 실측(§4): `S`에 대한 쓰기 0건. |
| 공유 트리 `S` 위치 | **HOME 밖 고정 절대경로**(예: `/opt/claude-shared`, 0755, 상위 전부 o+x). | `/home/app` 0700이라 HOME 아래로는 `U`가 못 닿는다(실측). 플러그인 레지스트리의 절대경로(`installLocation`)가 설치 때와 같아야 로드된다(실측 §4.2). |
| 선행 블로커 | **`workspace_resources.py` 미러를 symlink 안 따르게 고친다**(§6). | 지금 게이트웨이가 매 resolve에 `<ws>/.claude/{skills,agents}`에 symlink를 따라 쓴다. `U`가 워크스페이스에 쓰게 되는 순간, app 손으로 임의 파일을 0바이트로 자르는 수단이 된다(실측). **소유권보다 먼저 고쳐야 한다.** |
| 기동 프로브 | mode-bit 추정(`_dac_allows`)을 **실제 동작 시도**로 바꾼다. | 프로브가 ACL을 모른다 → ACL로 연 접근을 거짓 거부한다(코드 `landlock.py:484`). 브로커로 `U`가 실제로 `projects/<name>`을 만들고 파일을 쓰게 하면 ACL/per-uid dir를 우회해 판정한다. |
| 기존 데이터 | **1회성 이전**: 기존 워크스페이스 트리에 ACL을 소급 적용하고, named 사용자 transcript를 per-uid `P`로 옮긴다(§3.7). 롤백은 수용 시험에 넣는다. | default ACL은 생성 시점에만 상속된다(POSIX ACL 규칙). 지금 워크스페이스는 app 소유 0755/0644다(코드). |
| 보존 | **게이트웨이 주도 sweep**(브로커 `cleanup` op 재사용, §4.5). | per-uid `P`에선 CLI 기동 정리가 그 uid의 CLI가 뜰 때만 돈다(실측 §4.5). |
| 영속성 | `<config-base>`와 `S`를 **영속 볼륨**에 둔다(§8). | 지금은 `claude_home` 볼륨이 transcript·플러그인 상태를 지킨다(코드 `docker-compose.yml`). |

**대안 β**(워크스페이스를 `U`가 소유)는 §3.4에서 다룬다. 격리가 더 깔끔하지만 파일 API·quota·이미지 핸들러까지
전부 브로커 대행으로 보내야 해 표면이 크게 늘어, 지금은 α를 권한다.

---

## 2. 지금 상태 (실측 기반)

### 2.1 이미지 (코드 + `docker save` 헤더/ dpkg)
- 사용자: root와 `app`(1000:1000)뿐. **브로커 uid 19999·사용자 대역 20000–29999의 passwd/group 항목 없음.**
  userland은 숫자 uid에 passwd 레코드를 요구한다(`whoami`·node `os.userInfo()`).
- `/home/app` = **0700** 1000:1000. trixie `login.defs`의 `HOME_MODE 0700`이 `useradd -m`에 적용된 결과.
  `chown -R`는 모드를 안 바꾼다.
- `CLAUDE_CONFIG_DIR`는 Dockerfile·compose·entrypoint 어디서도 설정 안 함. 런타임 `~/.claude`는 named volume
  `claude_home`.
- `USER_WORKSPACES_DIR` compose 기본 `/data/workspaces`(host `./workspaces` bind). entrypoint는 이 경로를
  만들지도 chown하지도 않는다. 게이트웨이가 `mkdir`로 만들어 app 소유 0755.
- 패키지: **`acl`(setfacl/getfacl) 없음**(libacl1만), **`setpriv` 있음**, git 2.47.3, bubblewrap/socat/
  libcap2-bin/procps 없음. (단 prod의 비공개 build hook이 더 설치했을 수 있다.)
- 브로커는 entrypoint의 **전체 env**(compose env_file 포함)를 상속한다.

### 2.2 setfacl 없이 ACL 걸기 (실측)
이미지에 `setfacl`이 없어도 **순수 Python `os.setxattr`**로 ACL을 건다. `system.posix_acl_access`·
`system.posix_acl_default`에 쓰는 바이트가 `acl` 2.3.2의 `setfacl` 결과와 **access·default 모두 바이트
단위로 동일**했다(trixie 컨테이너에서 교차 검증). 인코딩:
- 헤더 `struct("<I", 2)`(version 2);
- 엔트리 `struct("<HHI", tag, perm, id)`, tag {USER_OBJ 0x01, USER 0x02, GROUP_OBJ 0x04, GROUP 0x08,
  MASK 0x10, OTHER 0x20}, 이름 없는 엔트리의 id는 0xffffffff;
- tag→id 순 정렬.
→ 게이트웨이(또는 entrypoint)에 이 인코더를 작은 stdlib 헬퍼로 넣는다. `acl` 패키지 의존은 불필요하되,
프로비저닝 편의로 이미지에 넣는 선택지도 있다.

---

## 3. 워크스페이스 소유권 (설계 α)

### 3.1 모델
- 워크스페이스 루트 `<base>/<user>/claude`(그리고 `_tmp_*`)는 **app 소유로 유지**. ACL을 건 뒤의 모드는
  `0770`(`+`)이다 — ACL이 있으면 mode의 group 비트가 곧 mask다(아래 ⚠️).
- **confined spawn 직전**, uid `U`가 정해진 뒤 게이트웨이(app, 루트 소유자)가 루트에 ACL을 **다시 건다**:
  - access: `u::rwx, u:<U>:rwx, g::---, m::rwx, o::---`
  - default: `d:u::rwx, d:u:<U>:rwx, d:u:<APP_UID>:rwx, d:g::---, d:m::rwx, d:o::---`
  - **`d:u:<APP_UID>:rwx`는 빠지면 안 된다.** 상속된 ACL의 `u::`는 새 객체의 소유자(= `U`)라서, app named
    엔트리가 없으면 app은 `U`가 만든 모든 것에서 `o::---`로 떨어진다(실측 2026-10-15: `U`의 0666 파일·0777
    디렉터리 모두 app EACCES, 엔트리를 넣으면 전부 열림). §3.2의 "app 접근 유지"는 이 엔트리를 전제로 한다.
    `d:u:<U>:rwx`는 반대 방향으로, app이 만든 하위(`.claude_images`·미러·argv 파일)를 `U`에 연다.
  - ⚠️ **ACL을 건 뒤 루트를 `chmod`하지 않는다.** `chmod 0750`은 mask를 `r-x`로 접어 `U`의 named 엔트리를
    조용히 막는다(실측: 그 뒤 `U`의 생성이 전부 EACCES). 모드를 바꿔야 하면 ACL을 마지막에 다시 건다.
  - 매 spawn 재적용이라 멱등이다. 루트의 모드·ACL은 소유자(app)만 바꿀 수 있으므로, 재적용이 실제로 지키는
    것은 게이트웨이 자신의 `chmod`다. `U`가 자기 하위에서 좁힌 것은 재적용으로 돌아오지 않는다(§3.2).
- `U`는 named 엔트리로 루트에 rwx를 얻어 그 안에 쓴다. `U`가 만든 하위는 default ACL을 상속한다. 상속은
  **생성 시점에만** 일어나므로 ACL 전부터 있던 내용은 따로 옮긴다(§3.7).
- uid는 named 사용자면 레지스트리에서 결정적(코드 `existing_uid`), 익명이면 ephemeral 임대. 임대는 워크스페이스
  생성이 아니라 첫 spawn에서 잡히므로(코드 `uid_isolation._EphemeralPool`), ACL도 spawn 직전, uid가 정해진
  뒤에 건다.

### 3.2 ACL이 app 접근을 **지키는 범위** (실측, 핵심)
default ACL 아래서 새 객체의 mask = `default mask ∩ 생성 호출이 넘긴 mode의 group 비트`이고 **umask는 전혀
적용되지 않는다**(실측, overlay·volume 동일). 아래 표는 §3.1의 default ACL(`d:u:<APP_UID>:rwx` 포함) 기준이다.
app의 실제 권한은 그 named 엔트리 ∩ mask다.

| `U`가 만든 방식 | 결과 mask | app 접근 |
|---|---|---|
| 일반 도구 파일(`open` → mode 0666) | `rw-` | 읽기·쓰기·삭제 **됨** |
| 일반 디렉터리(`mkdir` → 0777), `.cli-tmp` | `rwx` | 전부 **됨** |
| `tempfile.mkstemp`(0600), transcript(0600) | `---` | stat·unlink만. **읽기·쓰기 안 됨** |
| `os.mkdir(0o700)`, memory dir, `projects/<name>`(0700) | `---` | 안쪽 목록·삭제 **안 됨**(`rmdir`은 빈 경우만) |
| `os.open(..., 0o644)` | `r--` | 읽기·삭제 됨, 쓰기 안 됨 |

→ **흔한 경우(도구가 기본 mode 0666/0777로 만드는 파일·디렉터리 — ACL 아래선 0660/0770)는 app이 그대로
접근**한다. 파일 API·quota 워커·이미지 핸들러가 계속 돈다. **`U`가 일부러 0600/0700으로 만든 것**(transcript,
mkstemp, memory)만 app이 못 연다. 그건 이미 브로커 대행이 필요한 대상과 일치한다. 또 `U`는 자기 소유물의
모드·ACL을 언제든 좁힐 수 있으므로, app의 "접근 유지"는 **best-effort**다 — 보장이 필요한 삭제는 브로커로
가고(§3.3), transcript 읽기도 브로커로 간다(§3.6).

### 3.3 정리 대행 (브로커 `cleanup` op)
app은 `U` 소유 0600/0700을 못 열고, `U` 소유 트리를 rmtree하지 못한다(실측: `rm -rf`도 EACCES). 확실한
정리는 **`U`로 실행**해야 한다(실측). 브로커는 SETUID/SETGID만 쥐어 `CAP_DAC_OVERRIDE`로 우회하지도 못한다.
그래서 두 번째 요청 종류를 브로커에 추가한다(아래 위치·필드는 2026-10-15 코드 기준).

- **분기 위치**: `_Broker._advance`의 `verify_ticket` 직후(`isolation_broker.py:954`), `_spawn_request` 앞.
  상주 부모라 nonce 단일 사용이 그대로 권위 있다.
- **티켓**: 서명된 payload에 `"op":"cleanup"`과 지울 절대경로 목록(워크스페이스/`projects/<name>`/transcript/
  `session-env/<sid>`/`tasks/<sid>`), live 검사용 workspace 문자열(=resolved `rw[0]`), `/dev/shm` 정리 여부를
  싣는다. 지금 payload엔 `uid, workspace, ro, rw, env_allow, nonce, exp`만 있고 gid·op가 없으니 필드를 더한다.
  **spawn 경로는 `op`가 있는데 `"spawn"`이 아닌 티켓을 명시적으로 거부**해야 cleanup 티켓을 spawn으로 못 쓴다
  (지금 `_spawn_request`는 모르는 키를 무시한다).
- **header**: fd 3개 검사(`:948`)가 verify보다 앞이므로, 게이트웨이가 `/dev/null` fd 3개를 보내거나 그 검사를
  op별로 옮긴다. 게이트웨이는 직접 소켓에 붙을 수 있다(peer pin이 APP_UID, 소켓 0660 group=app gid; 이미
  `landlock._broker_reachable`가 이렇게 연결한다).
- **실행**: 상주 loop는 단일 스레드라 parent에서 rmtree하면 안 된다. `_spawn`처럼 fork하고, child가
  `_become_supervisor`로 `(broker, U, broker)`가 된 뒤(fsuid=U) `shutil.rmtree(path, dir_fd=)`로 지우고
  결과를 기존 `ST_EXITED`/`ST_REFUSED` record로 보낸다. 새 record 종류는 불필요.
- **parent 선제 거부**: 같은 `(uid, workspace)`의 live supervisor(지금 `_check_uid_free`는 **다른** workspace만
  막는다 — 새 검사 필요), 그 uid의 orphan. "상태는 보냈지만 아직 reap 전" 창 때문에 검사 직전 `self._reap()`를
  한 번 돈다.
- **디렉터리 루트 자체 제거**: `U`는 내용만 비우고, 비sticky 부모의 소유자(= app 게이트웨이)가 빈 루트를
  rmdir한다(브로커에 REMOVE_DIR 권한을 더 주지 않으려면). 또는 Landlock으로 cleaner를 가두되 부모 REMOVE_DIR를
  허용한다. **익명(`_tmp_`)**은 임대가 독점이라 전부 지워도 되고, **named**는 같은 uid의 live 세션이 없을
  때만 지운다.
- **`/dev/shm` 잔여물**: 런이 남긴 객체를 cleaner가 `U`로 지운다(지금은 아무도 안 지워 다음 임대가 읽는다 —
  `security-process-isolation.md` §8의 공백). Docker 기본 `/dev/shm`이 1777 sticky면 `U` 소유 항목은 `U`(또는
  root)만 지운다. ⚠️ **uid 모드의 `/dev/shm` 파일은 umask 0022면 0644라 DAC로 안 갈린다** — 브로커·런처의
  "DAC가 나눈다" 주석은 틀렸다. 세션 umask를 077로 두거나(§10) `/dev/shm`을 세션별로 가를 방법을 정한다.

### 3.4 대안 β (워크스페이스를 `U`가 소유) — 비권장, 기록
`U`가 워크스페이스 루트를 소유(브로커가 spawn 때 `U`로 생성)하고, 게이트웨이는 상위 `<base>/<user>`에 건
default ACL로 traverse만 얻는다. 격리는 더 깔끔하다(진짜 "`U`의 디렉터리"). 그러나 `U`가 루트를 0700으로 좁히면
게이트웨이가 탐색조차 못 해, **파일 API·quota·이미지 핸들러·미러가 전부 브로커 대행**으로 가야 한다(α는 흔한
경우 직접 접근). 새 브로커 표면이 훨씬 크고, 상주 단일 스레드 브로커가 경로마다 호출돼 지연·가용성 위험이 는다.
α의 "best-effort 직접 + 잠긴 것만 대행"이 비용 대비 낫다. β는 egress/멀티테넌시 요구가 더 세질 때 재검토.

### 3.5 게이트웨이가 워크스페이스를 만지는 경로 (전수 — 소유권 전 점검)
`U`가 트리를 쓰게 되면 아래가 전부 "app이 `U`가 심은 링크를 따라가거나, `U` 소유 파일에서 EACCES"가 된다.

1. ⚠️ **선행 블로커 — 미러 truncation.** `prepare_workspace_resources()`가 **매 resolve**(파일 브라우저 폴링
   포함)에 `<ws>/.claude/{skills,agents}/.oh-my-gateway-managed`에 **symlink를 따라** `write_bytes(b"")`한다
   (코드 `workspace_resources.py:255`; 실측: 심어 둔 marker symlink의 대상이 0바이트로 잘림). `materialize_*`의
   `os.link`·`rmtree`·`rename`도 `U` 소유 트리를 가로지른다. **§6에서 먼저 고친다.**
2. **파일 API**(`terminal_files.py`): 이미 fd-pin + O_NOFOLLOW로 단단하다. `U` 소유 0600/0700에서는 open이
   EACCES → 500(현재 미포착). ACL로 흔한 경우는 열리고, `U`가 잠근 것만 500(자기 것만 잠근 셈) 또는 대행.
3. **quota 워커**(`workspace_quota.py`): 재귀 walk. 못 읽는 하위는 **fail-closed 503**(사용량을 숨기지 않음,
   실측 설계대로). ACL로 흔한 경우는 읽히고, `U`가 0600/0700로 잠그면 그 사용자 quota-증가 작업이 503. 수용.
4. **이미지 핸들러**(`image_handler.py`): `<ws>/.claude_images`에 app이 쓴다 → `U` 소유 dir면 EACCES. ACL로
   해결(default ACL 상속) 또는 입력 이미지 쓰기를 spawn 전 app 단계로 유지.
5. **argv 파일**(`argv_files.py`): spawn 전에 app이 `O_NOFOLLOW`·`O_EXCL`로 쓰고 직후 지운다. uid 모드 0644라
   `U`가 자기 것으로 읽는다(의도). 워크스페이스 최종 성분만 O_NOFOLLOW인데 워크스페이스 자체 링크는 상류
   `_confined_workspace`가 막는다.
6. **워크스페이스 스코프 스킬**(`agent_catalog.py`): 읽기 전용, symlink·비정규파일 skip, EACCES면 `{}`. 안전.
7. **transcript**: 읽기는 §3.6, 삭제는 §3.3.
8. **pathlib 주의**: Python 3.12는 EACCES 부모에서 `.exists()/.is_file()`이 **False가 아니라 PermissionError를
   올린다**(실측). `U` 소유 0700 하위를 만지는 게이트웨이 코드는 전부 try로 감싸야 한다.
9. **Landlock 비중재**: landlock 모드에서 Landlock은 룰셋 **밖** 파일의 `chmod`/`utime`/`setxattr`를 막지
   않는다(open/rename/unlink만; 실측 ABI 7). CLAUDE.md는 `access(2)`만 적는다 — 문서 보강.

### 3.6 transcript 읽기 대행 (rehydrate)
메모리에 없는 세션의 continuation은 게이트웨이가 transcript를 **내용까지** 읽어 복원한다(코드
`session_manager._try_rehydrate_from_jsonl`). 줄마다 기록된 `cwd`가 요청자의 워크스페이스와 같은지 확인하고
(사용자명 충돌 방어), 외부 user 턴 수를 센다(`turn_counter`, `max_turn` 검사). uid 모드에서 transcript는 `U`
소유 0600이라 mask가 `---`로 접혀 ACL로도 열리지 않는다(§3.2). 그래서 지금은 `TranscriptUnreadable` → 500
`transcript_unreadable`이고, 삭제 대행(§3.3)만으로는 이 경로가 살아나지 않는다. 두 길이 있다.
- **(a) 브로커 `transcript` op**: cleanup과 같은 티켓·fork 경로에서 `U`가 된 자식이 서명된 경로 하나
  (`<cfg>/projects/<name>/<sid>.jsonl`)를 읽고, 내용이 아니라 **요약**(없음 / cwd 불일치 / 손상 / user 턴 수)만
  돌려준다. 기존 `=Ii` `(kind, value)` record에 kind를 더하면 새 프레이밍이 필요 없다. 파싱 규칙(`isMeta`·
  tool_result 제외, 손상 줄이면 거부)은 게이트웨이와 한 곳에서 공유하거나 parity 테스트로 묶고, 파일 크기·처리
  시간에 상한을 둔다. 읽기 전용이라 live 세션 검사는 필요 없다.
- **(b) 게이트웨이 메타데이터**: 턴을 끝낼 때마다 게이트웨이가 자기 저장소에 `(session_id, user, workspace,
  turn_counter)`를 남기고, rehydrate가 transcript 대신 그것을 본다. 브로커 표면은 늘지 않지만, 게이트웨이가
  기록하기 전에 죽으면 CLI의 transcript와 턴 수가 어긋날 수 있다.

권장은 (a)다. transcript가 진실의 원천으로 남고, 지금의 cwd 소유권 검사를 그대로 옮길 수 있다(§10).

### 3.7 기존 데이터 이전과 롤백
default ACL은 생성 시점에만 상속된다(POSIX ACL 규칙). off/landlock 모드에서 쌓인 named 사용자 워크스페이스는
app 소유 0755 디렉터리·0644 파일이라(코드 `workspace_manager.py:299`의 `mkdir` + umask 022), 루트에만 ACL을
다시 걸면 `U`는 기존 내용을 고치지 못한다(`U`는 other로 `r-x`/`r--`).
- **워크스페이스**: uid 모드로 처음 spawn하기 전에 사용자별로 한 번, app이 트리 전체에 §3.1의 access ACL(디렉터리는
  default도)을 건다. 기존 내용은 전부 app 소유라 app이 직접 할 수 있다. `dir_fd` 기반으로 symlink를 따르지 않고
  (§6과 같은 패턴) 돌고, 끝나면 marker를 남겨 다시 돌지 않는다. 트리가 크면 첫 요청이 늦어지므로 기동 백그라운드
  작업이나 운영자 명령으로 미리 돌리는 선택지를 둔다.
- **transcript**: 공유 config dir의 named 사용자 transcript(`<sid>.jsonl`과 `<sid>/` 하위)를 그 uid의
  `P/projects/<name>`으로 옮긴다(§4). app이 복사하므로, `U`가 읽고 이어 쓸 수 있게 `u:<U>:rw-` ACL(또는 0644)로
  쓴다. 사용자명 충돌로 한 디렉터리에 여러 사용자의 transcript가 섞여 있으면 줄에 기록된 `cwd`로 주인을 가른다
  (rehydrate의 소유권 검사와 같은 기준). 주인을 가릴 수 없는 공용 파일(충돌 디렉터리의 `memory/` 등)은 옮기지
  않는다.
- **롤백(uid → landlock/off)**: 그 모드의 CLI는 app uid로 돈다. `U`가 평범하게 만든 파일은 `d:u:<APP_UID>`
  덕에 계속 열리지만, `U`가 0600/0700으로 만든 것(transcript 포함)은 열리지 않는다. 롤백하려면 그 전에 브로커가
  `U`로 그것들의 모드·ACL을 app이 읽을 수 있게 되돌리고, per-uid `P`의 transcript를 공유 config dir로 되돌려야
  한다. 이 절차가 없으면 롤백은 그 사용자들의 잠긴 산출물과 resume을 포기하는 것이다. 롤백도 수용 시험에 넣는다
  (§9 step 7).

---

## 4. 사용자별 config dir (설계 B)

### 4.1 레이아웃 (실측 §5: `CLAUDE_CONFIG_DIR=P`면 상태가 `P`에만 쌓인다)
uid마다 `CLAUDE_CONFIG_DIR = P = <config-base>/<uid>`. `<config-base>`는 HOME·워크스페이스 base·temp 밖, 영속
볼륨 위에 둔다(§8). `P`는 **app 소유**, `U`에 traverse+read(r-x)만, 쓰기는 지정 하위에만.
```
P/                      # app 소유, U에 r-x. 공유 링크를 U가 못 바꾸게.
  skills        -> S/skills            # 공유(ro)
  agents        -> S/agents
  commands      -> S/commands
  output-styles -> S/output-styles
  CLAUDE.md     -> S/CLAUDE.md
  settings.json -> S/settings.json     # 링크 하나로 플러그인까지 로드(§4.2)
  .claude.json  -> state/.claude.json  # CLI 전역 설정. 락은 실패하나 fallback 쓰기가 링크를 따름(실측)
  state/                               # U 쓰기 가능. .claude.json 대상만 둔다
  plugins/                             # 실디렉터리(링크 아님)
    known_marketplaces.json -> S/plugins/known_marketplaces.json
    installed_plugins.json  -> S/plugins/installed_plugins.json
    marketplaces            -> S/plugins/marketplaces   # (= S/plugin-marketplaces 구조)
    data/                               # U 쓰기 가능. 훅의 CLAUDE_PLUGIN_DATA
  projects/<name>/        # U 쓰기 가능. transcript·subagents·memory
  session-env/            # U 쓰기 가능 (그 아래 <sid>는 브로커 자식이 U로 생성)
  tasks/                  # U 쓰기 가능
S = /opt/claude-shared    # HOME 밖, 0755, 상위 전부 o+x. 설치 때와 같은 절대경로.
```
- **최소 쓰기 집합**(실측): `projects/<name>`, `session-env`, `tasks`, TMPDIR. 쓰기 불가 `P`에서 이 셋만 열어도
  세션·`--resume`이 정상이었다. 실험은 CLI uid가 소유한 0500 `P`였다 — 설계의 app 소유 + `U`에 r-x와 CLI 입장에선
  같다(둘 다 쓰기 불가). 실패하는 쓰기(`.claude.json` 락, `sessions/`, `shell-snapshots/`, `telemetry/`)는
  **전부 세션을 깨지 않는다**. `sessions/` 없으면 peer 등록만 빠지고, `shell-snapshots/`는 confinement가 이미
  쓰기 불가로 둔다(`CLAUDE_CODE_SHELL` 래퍼), `telemetry/`는 `DISABLE_TELEMETRY`/
  `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC`로 끈다.
- **"`U` 쓰기 가능" 항목은 app이 만들고 §3.1과 같은 ACL(`u:<U>:rwx` + default, `d:u:<APP_UID>` 포함)로
  연다** — app은 `U`로 chown할 수 없다. `session-env/<sid>`처럼 브로커 자식이 `U`로 만드는 것은 그 부모가 이렇게
  열려 있어야 한다.
- `P`를 r-x로 두는 이유: `U`가 공유 링크(특히 `settings.json`)를 바꾸지 못하게. 단 `.claude.json`은 CLI가 `P`
  바로 아래에 락+tmp+rename으로 쓰므로, **`P` 전체를 쓰게 하지 않고 `.claude.json`만 `U` 쓰기 가능한 파일로
  symlink**한다(실측: 락은 실패하나 fallback이 링크 대상에 전체 설정을 저장). 실험은 대상을 미리 만들어
  두었다(CLI uid 소유 0600 `{}`). 설계에선 브로커 자식이 spawn 때 `U`로 `state/.claude.json`을 없을 때만 만든다.
  대상이 없는 링크와 app 소유+ACL 대상은 실측하지 않았다.
- ⚠️ **정책 강제는 공유 settings.json으로 못 한다**: `U`가 쓸 수 있는 하위를 통해 자기 링크를 갈아끼울 여지가
  있으면 바꿀 수 있다(실측). 강제해야 하는 설정은 `--settings` 플래그나 managed settings로 넣는다(플래그가
  워크스페이스 settings를 이긴다). 공유 `settings.json`의 `enabledPlugins`는 `U`가 깨도 자기 플러그인만 잃는다.

### 4.2 플러그인 (실측 §4.2 — 변형 전수)
- 권장: **`settings.json` 링크 + 실 `P/plugins/`(레지스트리·marketplaces는 S로 링크, `data`는 실디렉터리)**.
  이 구성에서 플러그인 skill·command·agent가 init에 뜨고 훅 3/3이 돌며 `CLAUDE_PLUGIN_DATA=P/plugins/data/<id>`
  쓰기 OK, **S 쓰기 0건**.
- `P/plugins -> S/plugins`(통째 링크)는 **실패**: 훅이 `EACCES mkdir plugins/data`로 안 돈다(턴은 success로
  보고 — 조용히 깨짐). `CLAUDE_CODE_PLUGIN_CACHE_DIR`로 루트를 옮겨도 data가 공유라 같은 실패.
- `S`는 **설치 때와 같은 절대경로**에 있어야 한다: `installLocation`·`extraKnownMarketplaces` 경로가 끊기면
  `cache-miss`로 로드 실패(실측). `installPath`만 끊기면 로드는 된다(루트가 cache가 아니라 marketplace 소스).
- cache 링크는 넣지 않는다: cache GC가 `.orphaned_at`을 S에 쓰려다 EROFS/EACCES(무해하나 불필요).
- 레지스트리에 쓰기가 일어나면 tmp+rename으로 **링크가 사용자별 파일로 갈라진다**(S는 온전, 오류 없음). 그
  사용자는 이후 S의 레지스트리 변경을 못 따라간다 — admin install은 공유 S에 설치하고 per-uid에 enable을
  반영하는 식으로 다룬다(§4.4).

### 4.3 `S`는 HOME 밖 (실측)
`/home/app` 0700이라 그 아래로는 다른 uid가 전혀 못 닿는다(실측). `S`를 `/opt/claude-shared`(0755, 상위 전부
o+x)에 두고 `P`에서 링크한다. `$HOME`·`/app`·임시 디렉터리·시스템 루트를 가리키는 링크는 Landlock 필터가
경고와 함께 뺀다(코드 `_GrantFilter`) — `S`는 그중 어디도 아니어야 한다. `CLAUDE_CONFIG_DIR`를 `~/.claude` 밖
`P`로 옮기면 기본 plugin clone 루트(`$HOME/.claude/plugin-marketplaces`)가 `$HOME` 안이라 빠지므로,
`CLAUDE_PLUGIN_CLONE_ROOT`를 symlink 아닌 `S` 경로로 설정한다.

### 4.4 config-dir 소비자 재배선 (코드 — 안 고치면 조용히 깨짐)
`security-process-isolation.md` §5·§7 step 2가 짚은 소비자들은 이미 리졸버 하나로 모였다(코드
`src/claude_config_paths.py`, #244의 PR-B: `config_dir`·`config_dir_strict`·`projects_root`·`settings_path_default`·
`plugin_clone_root`·`global_config_file`을 `session_manager`·`plugin_service`·`plugin_admin_service`·`agent_catalog`·
`claude_settings_env`·`workspace_sandbox`·`landlock`이 부른다. `docker/install_plugins.py`만 src를 import하지 않아
자체 사본을 쓴다). 다만 이 리졸버는 **프로세스 env**(`CLAUDE_CONFIG_DIR`/`HOME`)를 호출 시점에 읽어, 게이트웨이
전체에 config dir가 하나다. 사용자별 dir에 남은 일은 리졸버를 **uid(또는 user key)를 받아 per-uid `P`와 공유
`S`를 돌려주는 형태로 매개변수화**하고, 아래 소비자가 세션의 uid로 부르게 하는 것이다.
- **게이트웨이가 자식에 HOME/CLAUDE_CONFIG_DIR를 아예 안 넘긴다**(코드): 지금 둘 다 게이트웨이 프로세스 env에서
  그대로 흘러간다. per-uid면 `options.env`(uid 모드는 티켓이 아니라 런처가 넘기는 env)에 **per-uid
  `CLAUDE_CONFIG_DIR=P`와, HOME을 바꿀 거면 HOME**을 실어야 한다. env allowlist(`security-process-isolation.md`
  §6.4)와 함께.
- `_GrantFilter.rw`는 rw 항목이 **그 config dir 하나 안**이어야 한다고 강제한다(코드). per-uid `P`가 그
  config dir가 되도록, 그리고 `P`가 `$HOME`·워크스페이스 base·temp 안이면 거부되는 규칙을 per-uid base에 맞게
  연다.
- transcript 경로(`session_manager._projects_root`·`_session_jsonl_path`), `remove_session_dirs`,
  `_release_session_dirs`, guard 훅(`make_claude_home_guard_hook`), `_project_dir_name` 기반 own-root는 전부
  uid/user를 받지 않는다 → 리졸버로 per-uid 경로를 받게 한다.
- `claude_settings_env._write_settings`는 공유 `settings.json`을 **app 소유 0600**으로 되쓰고 symlink를 일반
  파일로 바꾼다(코드). per-uid면 `settings.json`은 공유 `S`에 **읽을 수 있는 모드로** 쓰고, admin env 블록은
  별도 공유 정책 채널로 다룬다(CLI가 exec 뒤 다시 적용하므로 env allowlist로는 못 거른다).
- `install_plugins.py`는 "stdlib only, never import src"라 자체 clone_root 사본을 유지한다(코드) — 리졸버와
  별개로 `S`에 설치하도록 경로를 맞춘다.
- 읽기 모델(`/admin/api/plugins`·`/v1/agent-resources`)은 이미 리졸버를 거친다(코드 `plugin_service`·
  `agent_catalog`). 공유 자산은 사용자별 `P`가 아니라 `S`에서 읽도록 리졸버가 `S`를 돌려주면 따라온다.

### 4.5 기동 정리 작업과 보존 (실측)
CLI는 기동 약 5초 뒤 백그라운드로 `cleanupPeriodDays`(기본 30) 지난 항목을 지운다. 스로틀은 `P/.last-cleanup`
기준 약 24시간. **user 설정 소스가 켜졌을 때만** 돈다(compose 기본 `user,project,local`은 켜짐). 공유 config
dir면 **한 사용자의 5초 이상 세션 하나가 모든 사용자의 30일 지난 transcript·session-env·tasks를 지운다**(실측).
모두에게 같은 보존 기간이 적용된 결과라 기밀성 문제는 아니고, 정리 주체가 "아무 사용자의 세션"일 뿐이다.
(`projects`가 0311이면 CLI는 정상 동작하고 정리만 `projects`를 조용히 건너뛴다 — 실측.)

사용자별 `P`면 각 `U`가 자기 것만 지운다. 대신 정리가 **그 uid의 CLI가 뜰 때만** 돌아, 다시 오지 않는 사용자의
transcript·`session-env`·`tasks`는 **만료되지 않는다** — 보존 기간이 그 사용자에게선 사라진다. 그래서 보존은
게이트웨이가 맡는다: 주기 작업이 `P`마다 기한(공유 `settings.json`의 `cleanupPeriodDays`, 없으면 30일)이 지난
항목을 골라 브로커 `cleanup` op(§3.3)로 `U`가 되어 지운다(app은 `U`의 0600/0700을 못 지운다).

---

## 5. CLI 쓰기 인벤토리 (실측 요약, 설계 근거)
`CLAUDE_CONFIG_DIR=P`, 게이트웨이 실옵션 기준.
- `P`에 쌓임: `.claude.json`(0600) + `backups/`, `projects/<name>/`(transcript 0600, `<sid>/subagents/*`,
  빈 `memory/`), `session-env/<sid>/`, `tasks/<sid>/`(Task tools 켰을 때만 — 5계열은 `CLAUDE_CODE_ENABLE_TODO_TOOLS=1`
  일 때만, 게이트웨이 미설정), `telemetry/1p_failed_events.*`(CLI 1회당 0.3–0.65MB; 끄면 없음). 세션 중에만:
  `sessions/<pid>.json`, `shell-snapshots/*.sh`.
- `H`(HOME)에: **MCP 로그만** — `H/.cache/claude-cli-nodejs/.../mcp-logs-*`(XDG_CACHE_HOME 기준,
  `CLAUDE_CONFIG_DIR` 무관). H가 0500이면 로그만 버려지고 MCP 호출은 정상.
- TMPDIR(`<ws>/.cli-tmp`)에: `claude-<uid>/.../tasks/<agentId>.output`(P의 subagent transcript로 가는 symlink),
  fswatch probe.
- `claude -v`(SDK 버전 확인)는 아무것도 안 쓴다.
- 상태를 옮기는 env 노브: `CLAUDE_CONFIG_DIR`(projects·session-env·tasks·plugins·sessions·shell-snapshots·
  telemetry·backups·file-history·debug·plans·image-cache·paste-cache·`.last-cleanup` 전부 따라감; 전역 설정은
  `<cfg>/.config.json`이 있으면 그것, 없으면 `(CLAUDE_CONFIG_DIR||HOME)/.claude.json`), `CLAUDE_CODE_PROJECT_DIR_NAME`
  (projects/<name>만; `CLAUDE_CONFIG_DIR` 있을 때만, 잘못된 값은 경고 없이 cwd 이름으로 fallback),
  `CLAUDE_CODE_TMPDIR`, `CLAUDE_CODE_PLUGIN_CACHE_DIR`(plugins 루트 전체), `XDG_CACHE_HOME`(mcp-logs).

---

## 6. 선행 블로커: 미러를 symlink 안 따르게 (소유권보다 먼저)
`workspace_resources.py`는 `U`가 소유하게 될 트리를 app 손으로 가로지른다. `U`가 쓰게 되는 순간 공격면이
열리므로 **소유권 작업 전에** 고친다.
- marker 쓰기·`_link_or_copy`·`_sync_managed_mirror`의 `rmdir`/`rename`/`os.link`를 **워크스페이스 dir fd 기준
  `*at` + `O_NOFOLLOW`**로 바꾼다(파일 API가 이미 쓰는 패턴, `terminal_files.py`의 `_open_dir_beneath` 등 재사용).
- marker는 `O_NOFOLLOW|O_CREAT`로 열어 symlink면 거부. `native` 자체가 symlink·비디렉터리면 미러 전체 skip
  (이미 `_real_directory` 검사가 있으나 최종 marker 쓰기 전까지 유지).
- 비확정: 소유권 모델별로 `U`가 `<ws>/.claude/skills/`에 실제로 쓰게 되는지 검증(모델마다 다름).

---

## 7. 기동 프로브 (ACL/per-uid 인지)
지금 `_probe_uid_sessions`→`_session_dir_problems`는 `_dac_allows`로 **mode bit만** 본다(코드 `landlock.py:484`).
ACL로 연 접근을 **거짓 거부**하고(ACL은 group 비트를 mask로 접어 named 엔트리가 안 보인다), per-uid `P`가 아니라
게이트웨이 config dir를 본다.
- 바꿈: 프로브가 **실제 동작을 시도**한다 — 브로커로 ephemeral uid가 per-uid `P`를 받아 `projects/<name>`을
  만들고 그 안에 파일을 쓰고 `session-env/<sid>`를 만들어 본다(이미 `launcher --version`을 띄우므로 거기에 확장).
  mode 추정 대신 커널이 ACL·per-uid dir를 평가한 결과로 판정.
- 프로브가 만든 per-uid dir·`.cli-tmp`를 lease 해제 전에 정리(지금은 `projects/<name>` rmdir 하나뿐).

---

## 8. 프로비저닝 (컨테이너 — prod에서 확정)
- **passwd/group**: 브로커 uid 19999 + 사용자 대역 20000–29999를 `/etc/passwd`·`/etc/group`에. 정적 생성
  스크립트 또는 `nss_wrapper`. 없으면 `whoami`·node `os.userInfo()` 실패. uid별 `USER`/`LOGNAME`도 env로
  (`security-process-isolation.md` §3 V).
- **공유 트리 `S`**: `/opt/claude-shared`(HOME 밖, 0755, 상위 전부 o+x), 설치 때와 같은 절대경로. 파일 자산
  (`CLAUDE.md`·`settings.json`)은 링크 하나뿐인 일반 파일(하드 링크면 Landlock 필터가 뺌).
- **per-uid config base·워크스페이스 base**: 상위 전부 o+x로 `U`가 자기 dir까지 탐색 가능. base는 app 소유.
- **영속 볼륨**: 지금은 `claude_home` 볼륨(`/home/app/.claude`)이 transcript·플러그인 상태를 지킨다(코드
  `docker-compose.yml`). `<config-base>`도 볼륨 위에 둬야 컨테이너를 다시 만든 뒤에도 named 사용자 resume이 된다.
  `S`를 이미지 레이어(`/opt`)에 두면 기동 때 `install_plugins.py`가 다시 채우는 것만 남는다. 런타임 쓰기(코드:
  `plugin_admin_service`가 marketplace를 clone한다)까지 유지하려면 `S`도 볼륨에 두거나, "`S`는 기동마다
  재생성"을 운영 규칙으로 명시한다.
- **ACL 인코더**: `os.setxattr` 기반 stdlib 헬퍼(이미지에 `acl` 없음). 프로비저닝 편의로 `acl` 패키지를 넣는
  선택지.
- **브로커 소켓 tmpfs**: `/run/gateway-isolation`(쓰기 가능 마운트), entrypoint가 0750 broker:app으로 생성.
- **`scripts/isolation_probe.py`를 이미지에** 넣어 prod에서 `/home/app` 모드·커널 ABI·seccomp·`S` 도달성 확인.
- uid 모드의 CLI 자식은 룰셋·셸 래퍼를 브로커 디렉터리에서 가져오므로 런처·브로커·룰셋·`claude-session-bash`를
  한 디렉터리(`/usr/local/bin`)에.

---

## 9. 구현 순서 (제안)
1. **미러 symlink-안전화**(§6) + CLAUDE.md의 Landlock 비중재(chmod/utime/setxattr) 문서 보강. 독립적·무특권,
   지금 landlock 모드에도 이득. **먼저.**
2. **config-dir 리졸버 매개변수화**(§4.4): 리졸버(`src/claude_config_paths.py`)는 이미 있다. user/uid →
   (per-uid `P`, 공유 `S`)를 받도록 넓히고 소비자가 세션의 uid로 부르게 한다. 아직 per-uid 값을 안 넣어도 되는
   선에서(동작 보존).
3. **ACL 헬퍼 + 워크스페이스 ACL 적용**(§3.1–3.2): spawn 직전 app이 루트에 default+access ACL
   (`d:u:<APP_UID>` 포함, ACL 뒤 chmod 없음). 기존 워크스페이스 이전(§3.7). 프로브를 실제 동작 시도로(§7).
4. **브로커 `cleanup` op**(§3.3) + 게이트웨이 배선: 익명 워크스페이스·`U` 잠금 잔여물·`session-env/<sid>`·
   transcript·`/dev/shm` 정리. **워크스페이스 ACL과 한 묶음**(둘 다 없으면 익명 풀이 말라 503). transcript 읽기
   대행(§3.6)도 같은 브로커 경로로.
5. **사용자별 config dir 배선**(§4.1–4.4): `CLAUDE_CONFIG_DIR=P` + 공유 링크, 플러그인 레이아웃, settings 공유
   정책 writer, transcript 이전(§3.7), 게이트웨이 보존 sweep(§4.5).
6. **프로비저닝**(§8): passwd/group, `S`, 영속 볼륨, 소켓 tmpfs, 프로브 이미지 포함.
7. **컨테이너 수용 시험**(`docs/isolation-uid-verification.md` §3, 롤백 포함 — §3.7) → 통과 후 기본값 전환·
   guard 격하.

---

## 10. 열린 질문 / 미결정
- **α vs β 최종 확정**(§3.1·3.4). 권장은 α(낮은 표면). egress/멀티테넌시 요구가 세지면 β.
- **ACL 노출**: 다른 세션 uid `V`가 워크스페이스 루트의 ACL을 읽어 `u:<U>`를 본다 → 어느 uid가 어느 워크스페이스
  주인인지 드러난다(실측). 또 `stat`이 존재 oracle(이름 추측 시). 기밀은 아니나 문서화. (Bash argv의 워크스페이스
  경로 노출(§6.5 `security-process-isolation.md`)과 같은 급.)
- **`.credentials.json`**: CLI 로그인 인증을 격리 모드와 같이 쓰면 ro 목록에 없어 모든 confined CLI가 인증 없이
  뜬다(cut 항목). per-uid 설계에서 함께 다룰지, 비호환으로 문서화할지. (compose는 `ANTHROPIC_AUTH_TOKEN`을 써
  무관.)
- **`/dev/shm` DAC**(§3.3): umask 0022면 세션 파일이 0644라 안 갈린다. 세션 umask 077 강제 vs 세션별 shm 경로.
  α의 default ACL 아래선 umask가 적용되지 않으므로(§3.2), 077 강제가 워크스페이스의 app 접근을 바꾸지는 않는다.
- **git `safe.directory`**: α(app 소유 루트)에서 `U`의 `git status`가 "dubious ownership"으로 실패(실측).
  `U`의 kept env(`security-process-isolation.md` §6.4)에 `GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=safe.directory
  GIT_CONFIG_VALUE_0=<workspace>`를
  싣거나(값 `*`도 됨), 워크스페이스를 `U` 소유로(β)면 불필요. 세션별 `GIT_CONFIG_GLOBAL`에 `safe.directory`를
  더하는 길도 있다(지금 writer가 `core.excludesFile`만 씀).
- **익명 lease 키잉**: ephemeral uid는 재사용되므로 per-uid dir를 uid로 키잉하면 lease 해제 직전 브로커가 `U`로
  비워야 한다(§3.3 cleanup과 같은 경로).
- **`WORKSPACE_LEGACY_LOCALPART_KEY`**: 켜면 워크스페이스는 localpart, uid는 전체 키라 "한 uid ↔ 한 워크스페이스"가
  깨진다(코드). uid 모드와 함께 쓰지 않도록 기동 검사 추가 검토.
- **transcript 읽기 대행 방식**(§3.6): (a) 브로커 `transcript` op(권장) vs (b) 게이트웨이 메타데이터.
- **롤백 지원 범위**(§3.7): 브로커가 `U`의 잠긴 산출물을 되돌리는 op까지 지원할지, "롤백 = 잠긴 산출물·resume
  포기"로 문서화할지.
