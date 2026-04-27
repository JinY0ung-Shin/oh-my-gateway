# Per-User Workspace Isolation Design

## Summary

`/v1/responses` 엔드포인트에 `user` 파라미터를 추가하고, 유저별로 격리된 working directory를 제공한다. 유저 간 파일 접근이 불가능한 멀티테넌트 격리를 달성한다.

## Motivation

현재 모든 요청이 단일 `CLAUDE_CWD` (또는 temp dir)를 공유한다. 멀티유저 환경에서 파일 작업이 섞이고, 보안 격리가 없다. OpenAI Responses API에도 `user` 파라미터가 존재하지만 현재 래퍼에서는 구현되어 있지 않다.

## Scope

- `/v1/responses` 엔드포인트만 적용
- 제거된 레거시 엔드포인트(`/v1/chat/completions`, `/v1/messages`)는 제외

## Design

### Data Flow

```
POST /v1/responses { user: "alice", input: "...", ... }
  |
  v
ResponseCreateRequest.user -> WorkspaceManager.resolve("alice", sync_template=is_new_session)
  |
  +- base_path = USER_WORKSPACES_DIR or CLAUDE_CWD
  +- sanitize("alice") -> "alice"
  +- workspace = base_path / "alice"
  +- mkdir -p workspace
  +- if sync_template: copytree(CLAUDE_CWD/.claude -> workspace/.claude)
  |
  v
backend.run_completion(prompt=..., cwd=workspace, ...)
  |
  v
SDK: ClaudeAgentOptions(cwd=workspace, ...)
```

### User Identification

- 클라이언트가 보내는 `user` 문자열을 그대로 신뢰 (OpenAI API와 동일한 방식)
- auth-level 유저 검증 없음

### Workspace Lifecycle

| 조건 | 경로 | 생명주기 |
|---|---|---|
| `user` 지정 (e.g. `"alice"`) | `{base_path}/alice/` | 영구 보존, 서버 재시작 후에도 유지 |
| `user` 미지정 | `{base_path}/_tmp_{uuid}/` | 세션 만료 시 삭제 |

### `.claude` Template Sync

- 새 세션 생성 시(`previous_response_id` 없음): `CLAUDE_CWD/.claude` -> `workspace/.claude` 복사 (덮어쓰기)
- 이어하기 요청(`previous_response_id` 있음): 복사 안 함
- `CLAUDE_CWD` 미설정 시: 복사 skip

### Environment Variables

| 변수 | 설명 | 기본값 |
|---|---|---|
| `USER_WORKSPACES_DIR` | 유저 워크스페이스 루트 경로 | 빈 문자열 |

Base path 결정 우선순위:
1. `USER_WORKSPACES_DIR` 설정됨 -> `{USER_WORKSPACES_DIR}/{user}/`
2. `USER_WORKSPACES_DIR` 비어있음 -> `{CLAUDE_CWD}/{user}/`
3. 둘 다 비어있음 -> temp dir (기존 동작 유지)

### Security

- **유저명 allowlist**: `^[a-zA-Z0-9][a-zA-Z0-9_-]{0,62}$` (path traversal 방지)
- **이어하기 시 user 검증**: `session.user != body.user`이면 400 에러 (세션 탈취 방지)
- **임시 워크스페이스 자동 정리**: 세션 만료 시 `_tmp_*` 디렉토리 삭제

## Changed Files

### New

| 파일 | 설명 |
|---|---|
| `src/workspace_manager.py` | WorkspaceManager 클래스: resolve, sanitize, sync_template |

### Modified

| 파일 | 변경 내용 |
|---|---|
| `src/response_models.py` | `ResponseCreateRequest`에 `user: Optional[str]` 필드 추가 |
| `src/session_manager.py` | `Session`에 `user: Optional[str]` 필드 추가, 세션 만료 시 임시 워크스페이스 삭제 |
| `src/backends/base.py` | `BackendClient.run_completion`에 `cwd: Optional[str] = None` 파라미터 추가 |
| `src/backends/claude/client.py` | `_build_sdk_options`, `run_completion`에서 cwd override 지원 |
| `src/routes/responses.py` | workspace resolve 연동, user 일치 검증, per-request ImageHandler |
| `src/constants.py` | `USER_WORKSPACES_DIR` 상수 추가 |
| `.env.example` | `USER_WORKSPACES_DIR` 문서화 |
| `README.md` | per-user workspace 기능 설명 |

### Tests

| 파일 | 설명 |
|---|---|
| `tests/test_workspace_manager.py` | sanitize, resolve, sync_template, 임시 디렉토리 정리 |
| `tests/test_responses_user.py` | user 파라미터 연동, user 불일치 에러, anonymous fallback |

## WorkspaceManager API

```python
class WorkspaceManager:
    def __init__(self, base_path: Path, template_source: Optional[Path] = None):
        """
        base_path: 유저 워크스페이스 루트
        template_source: CLAUDE_CWD (`.claude` 폴더가 있는 디렉토리)
        """

    def resolve(self, user: Optional[str] = None, sync_template: bool = False) -> Path:
        """유저 워크스페이스 경로 반환.
        - user 지정: {base_path}/{sanitized_user}/
        - user 미지정: {base_path}/_tmp_{uuid}/
        - sync_template=True: .claude 폴더 복사
        """

    def cleanup_temp_workspace(self, workspace: Path) -> None:
        """_tmp_ 접두사 워크스페이스 삭제."""

    def _sanitize(self, user: str) -> str:
        """allowlist 기반 검증. 부적절 시 ValueError."""

    def _sync_template(self, workspace: Path) -> None:
        """template_source/.claude -> workspace/.claude 복사(덮어쓰기)."""
```

## BackendClient Protocol Change

```python
# src/backends/base.py
def run_completion(
    self,
    prompt: str,
    # ... existing params ...
    cwd: Optional[str] = None,  # NEW: per-request working directory override
    **_extra: Any,
) -> AsyncIterator[Dict[str, Any]]: ...
```

- `cwd=None`이면 기존 `self.cwd` 사용 (하위 호환)
- Codex 등 다른 백엔드는 파라미터를 무시해도 됨

## ImageHandler Per-Request

```python
# src/routes/responses.py
image_handler = ImageHandler(workspace) if workspace else getattr(backend, "image_handler", None)
prompt = MessageAdapter.response_input_to_prompt(input_for_prompt, image_handler=image_handler)
```

워크스페이스가 있으면 해당 경로로 임시 ImageHandler를 생성하여 이미지를 유저 디렉토리에 저장.

## Session-User Binding

```python
# Session dataclass
@dataclass
class Session:
    session_id: str
    user: Optional[str] = None  # NEW
    workspace: Optional[str] = None  # NEW: 워크스페이스 경로 (임시 디렉토리 정리용)
    # ... existing fields

# /v1/responses 이어하기 시
if session.user != body.user:
    raise HTTPException(400, "user mismatch with existing session")
```

## Rejected Alternatives

| 제안 | 기각 사유 |
|---|---|
| Per-user ClaudeCodeCLI 인스턴스 풀 | 메모리 오버헤드, BackendRegistry 싱글턴과 충돌 |
| Middleware-level cwd 주입 | 단일 엔드포인트에 과한 추상화 |
| Per-user asyncio.Lock | SDK 호출 최대 10분, 같은 유저의 동시 요청 블로킹으로 UX 심각하게 저해 |
| Symlink 체크 | 유저가 서버 파일시스템에 직접 접근 불가, 비현실적 위협 |
| `.claude` 1회성 복사 | 관리자 설정 변경이 새 세션에 반영되어야 함 |
| anonymous 공유 워크스페이스 | 격리 불가 문제 -> uuid 임시 디렉토리로 대체 |
