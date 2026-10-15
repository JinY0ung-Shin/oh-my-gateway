# CLAUDE.md로 에이전트 지시 넣기

gateway가 띄우는 Claude CLI가 어떤 `CLAUDE.md`를 읽는지, 그리고 모든 사용자에게 공통 지시를 넣으려면
어디에 두어야 하는지 정리한다.

## 요약

- gateway는 새 사용자의 워크스페이스를 만들 때 **`CLAUDE.md`를 만들지 않는다.**
- oh-my-gateway를 배포한 폴더(저장소 checkout)의 `.claude/CLAUDE.md`는 **읽히지 않는다.** CLI는 그 폴더가
  아니라 각 사용자의 워크스페이스에서 실행된다.
- 모든 사용자 공통 지시는 **컨테이너 HOME의 `~/.claude/CLAUDE.md`**(`/home/app/.claude/CLAUDE.md`)에 둔다.

## 새 사용자가 오면 gateway가 만드는 것

`WorkspaceManager.resolve()`(`src/workspace_manager.py`)가 처음 만드는 것은 다음뿐이다.

```text
<USER_WORKSPACES_DIR>/<사용자>/<claude 작업 폴더>/
├── skills/            # 사용자가 파일 관리자에서 편집하는 원본
├── agents/
└── .claude/
    ├── skills/        # 위 원본을 Claude 탐색 경로에 비춘 사본 (workspace_resources.py)
    └── agents/
```

- `.claude/*` 아래에는 gateway 관리 표시 파일(`.oh-my-gateway-managed`)이 함께 생긴다. 자세한 내용은
  [workspace-agent-resources.md](workspace-agent-resources.md)에 있다.
- `WORKSPACE_INITIAL_DIRS`를 설정하면 처음 만들 때 그 폴더들도 생긴다. 폴더만 만들고 파일은 넣지 않는다.
- 플랜 모드를 쓰면 CLI가 `.claude/plans`를 만든다(`CLAUDE_PLANS_DIRECTORY`).

`CLAUDE.md`는 사용자가 `/init`을 실행하거나, 에이전트가 직접 파일을 쓸 때만 생긴다.

## CLI가 읽는 CLAUDE.md

| 위치 | 적용 범위 | 읽히는 조건 |
|---|---|---|
| 워크스페이스의 `CLAUDE.md`, `.claude/CLAUDE.md` | 그 사용자 한 명 | project 설정 소스. 기본값(`project,local`)에 들어 있어 항상 읽힌다 |
| 컨테이너 HOME의 `~/.claude/CLAUDE.md` | **모든 사용자** | `CLAUDE_SETTING_SOURCES`에 `user`가 있을 때만 |

- 설정 소스는 `CLAUDE_SETTING_SOURCES`로 정한다(`src/backends/claude/client.py` `_get_setting_sources`).
  - 설정하지 않았을 때 기본값은 `project,local`이다.
  - docker-compose는 기본으로 `user,project,local`을 넣는다. 그래서 Docker 배포에서는 HOME의 `CLAUDE.md`가 읽힌다.
- 모든 세션은 HOME 하나를 같이 쓴다. HOME 보호 훅(`make_claude_home_guard_hook`)이 `~/.claude/CLAUDE.md`를
  **읽기 전용 공유 자산**으로 다룬다(`_SHARED_CLAUDE_ASSETS`). 세션이 파일 도구로 이 파일을 고칠 수 없다.
  - 다만 Bash 쪽 방어는 경계가 아니다(루트 `CLAUDE.md`의 Gotchas 참고). 그래서 아래처럼 **읽기 전용 마운트**를 권한다.

## 모든 사용자에게 공통 지시 넣기 (Docker)

컨테이너의 HOME은 `/home/app`이다(`Dockerfile`). docker-compose는 `~/.claude`를 호스트 폴더가 아니라
named volume `claude_home`으로 마운트한다.

```yaml
volumes:
  - claude_home:/home/app/.claude
```

그래서 호스트 어딘가에 파일을 둔다고 반영되지 않는다. 둘 중 하나로 넣는다.

### 권장: 읽기 전용 바인드 마운트

docker-compose(또는 override 파일)에 한 줄을 더한다.

```yaml
volumes:
  - claude_home:/home/app/.claude
  - ./claude/CLAUDE.md:/home/app/.claude/CLAUDE.md:ro
```

- 파일을 git으로 관리할 수 있다.
- `:ro`라 세션이 어떤 경로로도 고칠 수 없다.

### 볼륨에 직접 복사

```bash
docker cp CLAUDE.md <컨테이너>:/home/app/.claude/CLAUDE.md
```

- 볼륨이라 컨테이너를 다시 만들어도 남는다.
- 버전 관리가 안 되고, 마운트처럼 읽기 전용으로 막히지도 않는다.

## 적용 시점

- 새로 시작하는 세션부터 확실히 반영된다.
- 이미 이어지고 있는 세션(`previous_response_id`로 계속되는 대화)에는 바로 적용된다고 보장하지 않는다.

## 대안: 관리자 시스템 프롬프트

전 사용자 공통 지시가 목적이면 gateway의 관리자 시스템 프롬프트로도 같은 일을 할 수 있다.

- 방법: 프롬프트 라이브러리(`/admin/api/prompt-library`) 또는 `SYSTEM_PROMPT_HOST_FILE`.
- 장점: 버전과 배포 이력(`data/prompt_deployments.jsonl`)이 남고, 세션마다 어느 버전을 받았는지
  기록된다(`base_prompt_ref`).

`CLAUDE.md`는 Claude Code 고유의 메모리 형식이라 모델에게 프로젝트 맥락처럼 주어진다. 반면 시스템 프롬프트는
gateway가 소유하는 기본 지시다. 운영 정책처럼 추적이 필요한 지시는 시스템 프롬프트에 두는 편이 낫다.
