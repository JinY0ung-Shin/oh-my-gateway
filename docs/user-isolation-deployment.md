# 사용자 격리 배포 가이드 (ChatDRAGON ↔ Oh My Gateway)

> 기준: 2026-10-24 `main`. 이 문서는 **지금 구현된 환경변수와 Compose 설정**에 맞춘 운영 절차다.
> `landlock`/ `uid`는 Claude CLI 및 그 하위 Bash·Python·stdio MCP의 **OS 파일 접근 경계**이고,
> 요청의 `user`가 누구 것인지 확인하는 **HTTP 인증 경계**는 별개다. 두 경계를 모두 확인해야 한다.

## 1. 먼저 선택하기

| 모드 | 설정 | 보장/한계 | 권장 상황 |
| --- | --- | --- | --- |
| `off` | `CLAUDE_ISOLATION_MODE=off` (기본) | OS 격리 없음. 워크스페이스 경로 검사·툴 훅만으로는 작성한 스크립트의 파일 읽기를 못 막음 | 신뢰할 수 없는 다중 사용자 환경에서는 사용 금지 |
| `landlock` | `CLAUDE_ISOLATION_MODE=landlock` | CLI·자식 프로세스에 커널 파일 접근 정책 적용. **같은 컨테이너 UID**를 공유하므로 사용자별 OS 계정/프로세스 격리는 아님 | **현재 환경을 최소 변경으로 보호하는 첫 단계** |
| `uid` | `docker-compose.uid.yml` 사용 | **사용자별 UID + Landlock**. 다른 사용자 파일의 DAC, config dir, 프로세스 접근 경계를 추가. ACL/브로커/영속 볼륨 필요 | 다중 사용자 서비스의 더 강한 OS 격리 |
| gVisor/microVM | **현재 main의 설정만으로 활성화 불가** | 별도 샌드박스 런타임/호스트/네트워크 통합 필요. #248은 스파이크/설계 단계 | 별도 프로젝트로 추진 |

**공통 전제:** Linux Landlock ABI **3 이상**(통상 커널 6.2 이상 또는 백포트) + 필요한 syscall을 허용하는 seccomp. 실제 판정은 **배포 컨테이너 안에서** 한다. 이 요건이 없으면 `landlock`/`uid` 기동이 거부된다(암묵적으로 `off`로 내려가지 않음).

**워크스페이스 아래 symlink 금지(두 모드 공통):** 격리 모드는 spawn마다 워크스페이스가 정확히 `realpath(USER_WORKSPACES_DIR)/<사용자>/...`로 풀리는지 검사한다. 사용자 워크스페이스를 다른 볼륨으로 잇는 링크처럼 base **아래**의 symlink를 거치면 그 요청은 HTTP 500 `workspace_refused`가 된다(base 자체가 링크인 것은 괜찮다). 켜기 전에 `find <workspace base> -mindepth 1 -type l`로 확인한다.

## 2. 인증과 OS 격리는 별도 설정 (중요)

### A. ChatDRAGON BFF만 Gateway에 접속하는 현재 구조

사용자마다 Gateway API 키를 등록하기 어렵다면 아래처럼 **BFF 전용 공용 서비스 키 하나**를 쓸 수 있다.

```dotenv
# Oh My Gateway .env — 실제 무작위 값으로 교체
ADMIN_API_KEY=<관리자_전용_비밀>
API_KEY=<BFF와_공유할_긴_무작위_서비스_키>
GATEWAY_REQUIRE_SCOPED_IDENTITY=false
# USER_API_KEYS는 설정하지 않음
```

- `API_KEY`는 **사용자별 인증이 아닌 unscoped 서비스 키**다. 키 보유자는 요청 `user`를 바꿔 다른 사용자의 워크스페이스/세션을 선택할 수 있다.
- 그러므로 **브라우저·최종 사용자에게 이 키를 노출하면 안 된다**. BFF가 실제 로그인 사용자를 인증하고, body/query/header의 사용자 식별값을 서버 측에서 강제로 정한 다음 Gateway로 전달해야 한다. 클라이언트가 보낸 `user`, `X-User-Email`을 그대로 신뢰하면 안 된다.
- **Gateway 포트는 BFF만 접근 가능**하게 방화벽/사설망/프록시 ACL을 적용한다. 기본 `docker-compose.yml`의 `ports: - "8000:8000"`에 해당하는 설정은 모든 인터페이스로 열릴 수 있다. 동일 호스트에서만 BFF가 접근하면 **기존 ports 항목을** `127.0.0.1:8000:8000`으로 바꾸고, 다른 호스트라면 BFF 전용 사설망과 방화벽으로 제한한다. 단순히 Compose override에 ports를 한 줄 추가하면 기존 공개 매핑도 남을 수 있으므로 **최종 Compose 결과**를 확인한다.
- admin 키(`ADMIN_API_KEY`)는 서비스 키와 분리하고, 관리자 API·파일 API·MCP relay·진단 엔드포인트도 노출 범위를 점검한다.
- `landlock`이나 `uid`를 켜도 **API 호출자의 사용자 사칭은 해결하지 못한다**. 이것은 OS 격리와 별개인 BFF/네트워크 인증 계약이다.

### B. Gateway가 최종 사용자 토큰을 직접 검사하는 구조

```dotenv
USER_API_KEYS='{"alice":"<alice의_비밀>","bob":"<bob의_비밀>"}'
GATEWAY_REQUIRE_SCOPED_IDENTITY=true
```

`USER_API_KEYS`는 **사용자 ID → 서로 다른 bearer token** JSON 매핑이다. 이 모드에서는 per-user HTTP 표면에 legacy `API_KEY`만 보내면 **403**이고, 유효한 사용자 토큰을 보내야 한다. 매핑 없이 `GATEWAY_REQUIRE_SCOPED_IDENTITY=true`를 켜면 startup **fail-fast**다. **공용 키 BFF 구성에서 이 값을 true로 바꾸지 말 것.** `USER_API_KEYS={}`는 인증을 제공하지 않는다.

장기적으로 BFF가 서명한 짧은 수명의 사용자별 신원 증명(JWT 등)을 Gateway가 검증하는 통합은 가능하지만 **현재 main에 이 기능은 없다**. `API_KEY` 하나로 사용자별 인증이 된다고 주장하면 안 된다.

참조: [credential-scoped user isolation](security-user-isolation.md), [#249](https://github.com/JinY0ung-Shin/oh-my-gateway/issues/249), [병합된 PR #250](https://github.com/JinY0ung-Shin/oh-my-gateway/pull/250).

## 3. 방법 1 — Landlock 빠른 적용

1. `.env`에 (위 공용 BFF 키 설정에 더해) 아래를 지정한다.

   ```dotenv
   CLAUDE_ISOLATION_MODE=landlock
   GATEWAY_NON_DUMPABLE=true
   WORKSPACE_LEGACY_LOCALPART_KEY=false
   # USER_WORKSPACES_DIR=/data/workspaces   # 기본 Compose에서 이미 지정
   # CLAUDE_ISOLATION_ALLOW_UNSAFE_LAUNCHER=false  # 기본값 유지
   ```

2. **기본 Compose만** 사용한다. `docker-compose.uid.yml`은 uid 모드로 덮어쓰므로 함께 적용하지 않는다.

   ```bash
   docker compose up -d --build gateway
   docker compose logs --tail=150 gateway
   ```

3. 컨테이너 내부 Landlock 지원을 확인한다.

   ```bash
   docker compose exec -T -u app gateway python3 -I -S - < scripts/isolation_probe.py
   ```

   출력에서 `Landlock usable (ABI>=3)` 판정과 기동 로그의 격리 모드를 확인한다. **`docker compose exec`로 직접 연 셸은 Claude 세션 격리 런처를 통과하지 않으므로**, 그 셸에서 파일을 읽을 수 있다는 사실만으로 Landlock 실패라고 판단하지 않는다. 실제 보호 검사는 **Gateway API로 시작한 Claude 턴에서** 수행한다.

4. 서로 다른 실험 사용자 A/B를 만들고 A의 워크스페이스 안에서 Bash/Python 스크립트가 B의 비밀 테스트 파일이나 `/app/data`를 열려고 하게 한다. 정상 동작이라면 거부되어야 한다. 동시에 **자기 워크스페이스의 쓰기/읽기와 일반 Bash·Python·MCP 도구는 정상**인지 확인한다. 실제 비밀값은 테스트에 사용하지 않는다.

**Landlock 모드의 한계:** UID가 같고 `/dev/shm` 등 OS 자원을 공유한다. 파일 경계가 생기더라도 별도 컨테이너·네트워크·PID 격리나 모든 프로세스/IPC 분리를 뜻하지 않는다. `GATEWAY_NON_DUMPABLE=true`는 Gateway 본체의 `/proc` 노출 방어이지만 CLI 자식에게 상속되는 환경변수를 완전히 제거하지는 않는다. `SYSINFO_CHILD_ENV_MASK`와 자식 환경변수 정리(#247)를 별도로 점검한다.

**landlock 모드에서는 다른 사용자 파일의 존재·메타데이터가 보인다(#253):** Landlock은 `open()`은 막지만 `stat(2)`·경로 탐색은 다루지 않는다. 그래서 같은 uid를 쓰는 landlock 모드에서는 다른 사용자 워크스페이스의 경로를 짐작해 `os.path.exists`·`os.stat`으로 다음을 알아낼 수 있다.
- 파일·디렉터리가 있는지
- 크기, mtime, inode, 소유자

파일 **내용**은 열리지 않는다(EACCES). seccomp는 경로를 보고 거르지 못하므로, 이 모드에서는 구조적으로 닫을 방법이 없다. 존재·활동 시각까지 숨겨야 하면 **uid 모드**를 쓴다(§4).

**명령줄은 다른 세션에 보인다(landlock·uid 공통):** CLI가 `/proc` 없이는 뜨지 않아 `/proc`는 읽기 전용으로 열려 있고, `/proc/<pid>/cmdline`은 uid와 관계없이 누구나 읽는다. 게이트웨이는 MCP 설정(`--mcp-config`)과 시스템 프롬프트를 1회용 파일로 넘겨 명령줄에서 뺐지만, 다음은 실행되는 동안 다른 세션이 볼 수 있다.
- 각 Bash 도구 호출의 **명령 텍스트**. 워크스페이스 경로가 들어 있어 사용자 키도 드러난다.
- 요청의 `text.format` JSON 스키마(`--json-schema`).

파일 **내용**은 보이지 않는다. 명령에 비밀값을 직접 쓰는 사용 패턴은 피한다. 상세: [설계 §6.5](security-process-isolation.md).

**켜면 달라지는 동작(landlock·uid 공통)** — "정상 기능" 확인에서 걸리는 것들이다.
- **공유 캐시를 쓸 수 없다.** npm·uv 캐시와 uv의 Python·도구 설치 경로가 세션마다 `<워크스페이스>/.cli-tmp` 아래로 옮겨진다. 한 세션이 심은 패키지를 다른 세션이 실행하지 못하게 하려는 것이다.
  - `npx -y`·`uvx`는 워크스페이스마다 새로 받고, 그 용량은 `USER_WORKSPACE_QUOTA_MB`에 잡힌다.
  - stateless 런은 매번 빈 캐시로 시작한다. npx·uvx로 띄우는 MCP 서버는 이미지에 미리 설치해 두는 편이 낫다.
- **`~/.gitconfig`·`~/.config/git`은 읽히지 않는다.** git은 워크스페이스별 `<워크스페이스>/.cli-tmp/gitconfig`를 전역 설정으로 쓴다. 운영 전역 git 설정은 `/etc/gitconfig`에 둔다.
- **플러그인:**
  - landlock 모드에서는 `~/.claude/plugins/data`가 읽기 전용이다. 훅은 돌지만 그 안에 쓰는 것은 실패한다. uid 모드에서는 사용자별 config dir에 쓸 수 있다.
  - 플러그인의 `bin/`이 Bash `PATH`에 오르지 않는다.
- **CLI 자체 Bash 샌드박스(bubblewrap)는 강제로 꺼진다.** Landlock 도메인 안에서는 뜰 수 없기 때문이다. `CLAUDE_SANDBOX_ENABLED=true`가 설정돼 있으면 기동 때 경고한다.

## 4. 방법 2 — 사용자별 UID + Landlock (더 강한 OS 격리)

메인 Compose와 **이미 포함된** `docker-compose.uid.yml`을 합쳐 실행한다. 이 override는 아래를 이미 설정한다.

```dotenv
CLAUDE_ISOLATION_MODE=uid
CLAUDE_CONFIG_DIR=/data/claude/shared
CLAUDE_PLUGIN_CLONE_ROOT=/data/claude/shared/plugin-marketplaces
CLAUDE_ISOLATION_CONFIG_BASE=/data/claude/users
CLAUDE_ISOLATION_MIGRATE_FROM=/home/app/.claude
```

또한 `claude_isolation:/data/claude` 볼륨을 추가한다. **이 다섯 값만 .env에 적고 기본 Compose로 시작하는 것보다 override를 쓰는 게 안전**하다. 기존 `claude_home`의 transcript/memory 이관 및 공유 자산 배치를 위한 레이아웃이다.

```bash
# 시작 전 실제 호스트 권한/볼륨과 인증 설정 검토
docker compose -f docker-compose.yml -f docker-compose.uid.yml config

# uid 모드 기동
docker compose -f docker-compose.yml -f docker-compose.uid.yml up -d --build gateway
docker compose -f docker-compose.yml -f docker-compose.uid.yml logs --tail=200 gateway

# 컨테이너 지원능력 읽기 전용 진단
docker compose -f docker-compose.yml -f docker-compose.uid.yml \
  exec -T -u app gateway python3 -I -S - < scripts/isolation_probe.py
```

필수 전제:
- Docker 컨테이너 **root 시작 단계** 유지(`user:`로 비root 시작하지 않음). entrypoint가 전용 broker를 생성한 다음 Gateway 본체 권한을 내린다. 일반 구성에서 추가 `cap_add`는 필요하지 않다.
- 호스트 커널 Landlock ABI≥3, POSIX ACL 지원, workspace/config base 상위 디렉터리 탐색 권한(`o+x`), 볼륨 영속성. entrypoint/기동 probe가 거부하면 로그의 실패 원인을 먼저 고친다.
- 기본 UID 대역은 `CLAUDE_ISOLATION_UID_MIN=20000`, `CLAUDE_ISOLATION_UID_MAX=29999`; broker UID 기본 19999. 사용자별 매핑은 `/app/data/isolation_uids.json`에 보존되므로 **이 파일과 `/data/claude` 볼륨을 백업**하고, 배포 후 임의로 UID 대역을 줄이거나 registry를 삭제하지 않는다.
- `/data/claude/shared`는 여러 사용자가 **읽는 공유** 스킬/설정 트리이다. 사용자 비밀이나 쓰기 가능한 테넌트별 데이터를 넣지 않는다. 기존 `~/.claude`의 수동 설치 자산은 자동 이전 대상이 아니므로 옮겨야 할 자산을 검토한다.
- `CLAUDE_ISOLATION_FALLBACK` **미설정 권장**: uid 모드가 불가능할 때 실패하도록 둔다. `landlock`을 명시해 폴백하면 **사용자별 UID 경계가 사라지고**, UID 소유 0600 파일·기존 세션 재개가 막힐 수 있다. 자동 안전 강등으로 간주하지 않는다.
- 다중 worker/다중 복제본은 UID registry 파일 잠금과 세션 상태의 전역 조율이 요구된다. 같은 registry를 쓴 두 Gateway 프로세스는 기동이 거부될 수 있다. 일반적인 worker 증설을 격리 확장 방식으로 보지 않는다.
- **되돌리기(uid → landlock/off):** 세션 uid가 평범하게 만든 워크스페이스 파일(0666/0777)은 ACL 덕분에 계속 쓸 수 있다. 하지만 세션 uid가 0600/0700으로 만든 파일과 사용자별 config dir의 transcript·memory는 열리지 않는다. 그 세션들은 이어지지 않는다. 처음 켜기 전에 `/data/claude` 볼륨과 registry 백업을 먼저 확보한다.
- **stat oracle(#253):** uid 모드는 워크스페이스 루트의 ACL이 그 세션 uid에게만 탐색을 허용하고 `other`에는 아무 권한도 주지 않는다. 그래서 다른 세션 uid가 그 안의 경로를 `stat`하면 파일이 있든 없든 똑같이 EACCES다. 존재도 메타데이터도 드러나지 않는다(`tests/test_uid_stat_oracle.py`가 root에서 확인한다). 남는 것은 `<base>/<사용자>`와 워크스페이스 루트 자체다. 사용자 이름을 짐작하면 그 사용자의 워크스페이스가 있는지와 그 디렉터리의 mtime은 보인다.
- **SysV IPC는 사용자별로 나뉘지 않는다.** Landlock이 SysV 공유 메모리·세마포어를 다루지 않기 때문이다. 살아 있는 세션이 world-accessible로 만든 객체는 다른 세션도 붙을 수 있다. 세션별 IPC 네임스페이스는 후속 작업이다.

세션 검증은 [uid 격리 수용 체크리스트](isolation-uid-verification.md)를 따른다. 특히 **사용자 A가 B의 워크스페이스·transcript를 열지 못하는지**, `/app/data`와 Gateway `/proc/<pid>/environ` 접근이 차단되는지, 세션 subprocess UID가 서로 다른지, 같은 사용자 재개·익명 세션 정리가 되는지 확인한다.

## 5. 배포 확인표

| 확인 항목 | 기대 결과 |
| --- | --- |
| 격리 모드 | Gateway 로그에서 요청한 `landlock` 또는 `uid`가 실제 적용됨 |
| 호스트 지원 | 컨테이너 probe에서 Landlock ABI≥3 사용 가능 |
| A→B 파일/스크립트 읽기 | 거부(자기 워크스페이스는 정상) |
| 인증 | BFF만 서비스 키 보유, 다른 사용자로의 임의 `user` 변경은 BFF에서 불가능 |
| 네트워크 | Gateway public 포트·관리자 경로가 불특정 클라이언트에게 노출되지 않음 |
| 비밀 | Gateway/admin/API/upstream 키가 사용자 CLI·MCP 환경에 유출되지 않음 |
| uid 추가 검증 | 사용자별 OS UID·config dir, ACL, 재기동 후 resume, 브로커 프로브 통과 |
| 정상 기능 | Bash/Python/MCP, 파일 업로드, 스킬·플러그인, 세션 이어가기 성공 |

```bash
# CLI 시작 실패/격리 실패 원인 확인
docker compose logs --tail=250 gateway

# 실제 Docker Compose의 포트·환경변수·볼륨 병합 상태 확인 (비밀 포함 가능: 외부 공유 금지)
docker compose config
```

**주의:** `docker compose config`는 서비스 키나 관리자 키를 출력할 수 있으므로 **로그·이슈·채팅에 결과를 그대로 붙이지 않는다**. 격리 모드를 바꾸면 새 Gateway 프로세스가 필요하다. `landlock`/ `uid`의 실패를 해결하려고 `CLAUDE_ISOLATION_MODE=off`로 돌아가는 것은 다중 사용자 운영에서 안전한 해결책이 아니다.

## 6. 관련 문서·미해결 경계

- [프로세스 격리 설계와 위협 모델](security-process-isolation.md)
- [uid 소유권·config dir 구현](isolation-uid-ownership-design.md)
- [uid 실제 검증 절차](isolation-uid-verification.md)
- [Credential-scoped user isolation](security-user-isolation.md)
- [세션 별도 컨테이너/gVisor 조사 #248](https://github.com/JinY0ung-Shin/oh-my-gateway/issues/248) — 현재 `CLAUDE_ISOLATION_MODE` 옵션이 아님
- [자식 환경변수 허용 목록과 비밀 노출 문제 #247](https://github.com/JinY0ung-Shin/oh-my-gateway/issues/247) — OS 파일 격리와 별개
