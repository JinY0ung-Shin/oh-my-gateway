#!/usr/bin/env python3
"""
SSE 이벤트 타입별 확인 스크립트.

tool_use, tool_result, task_* 이벤트가 구조화된 JSON으로 나오는지 테스트.
Responses API (/v1/responses)와 Chat Completions API (/v1/chat/completions) 둘 다 테스트.
"""

import httpx
import json
import sys
from dotenv import load_dotenv
import os

load_dotenv()

BASE_URL = "http://localhost:8000"
API_KEY = os.getenv("API_KEY", "")
# 에이전트 tool 사용을 유도하는 프롬프트
PROMPT = "이 디렉토리에 어떤 파일이 있는지 확인해줘 subagent를 사용해서 시켜줘"

COLORS = {
    "text_delta": "\033[37m",       # 흰색
    "tool_use": "\033[33m",         # 노란색
    "tool_result": "\033[36m",      # 시안
    "task_started": "\033[35m",     # 마젠타
    "task_progress": "\033[34m",    # 파랑
    "task_notification": "\033[32m", # 초록
    "lifecycle": "\033[90m",        # 회색
    "error": "\033[31m",            # 빨강
    "reset": "\033[0m",
}


def colorize(category: str, text: str) -> str:
    color = COLORS.get(category, COLORS["reset"])
    return f"{color}{text}{COLORS['reset']}"


def classify_event(event_type: str) -> str:
    if "tool_use" in event_type:
        return "tool_use"
    if "tool_result" in event_type:
        return "tool_result"
    if "task_started" in event_type:
        return "task_started"
    if "task_progress" in event_type:
        return "task_progress"
    if "task_notification" in event_type:
        return "task_notification"
    if "text.delta" in event_type or "output_text.delta" in event_type:
        return "text_delta"
    if "failed" in event_type or "error" in event_type:
        return "error"
    return "lifecycle"


def print_event(event_type: str, data: dict, api: str):
    cat = classify_event(event_type)

    if cat == "text_delta":
        delta = data.get("delta", "")
        sys.stdout.write(colorize("text_delta", delta))
        sys.stdout.flush()
        return

    if cat == "lifecycle":
        print(colorize("lifecycle", f"  [{event_type}]"))
        return

    # 구조화된 이벤트 — 트리 구조 표시
    parent = data.get("parent_tool_use_id")
    indent = "  │ " if parent else ""
    compact = json.dumps(data, ensure_ascii=False, indent=2)
    if parent:
        compact = compact.replace("\n", f"\n{indent}")
    label = colorize(cat, f"{indent}━━ {event_type} ━━")
    if parent:
        label += colorize("lifecycle", f"  (child of {parent[:12]}...)")
    print(f"\n{label}")
    print(colorize(cat, f"{indent}{compact}"))
    print()


def test_responses_api():
    print("=" * 60)
    print(colorize("task_started", "▶ Responses API (/v1/responses) 테스트"))
    print("=" * 60)

    url = f"{BASE_URL}/v1/responses"
    payload = {
        "model": "opus",
        "stream": True,
        "input": PROMPT,
    }

    event_counts: dict[str, int] = {}

    headers = {"Authorization": f"Bearer {API_KEY}"} if API_KEY else {}

    with httpx.stream("POST", url, json=payload, headers=headers, timeout=120.0) as resp:
        if resp.status_code != 200:
            resp.read()
            print(colorize("error", f"HTTP {resp.status_code}: {resp.text}"))
            return

        for line in resp.iter_lines():
            if not line:
                continue

            # SSE 파싱: event: 라인과 data: 라인
            if line.startswith("event: "):
                continue  # data: 라인에서 type 읽음

            if not line.startswith("data: "):
                continue

            if line.strip() == "data: [DONE]":
                break

            try:
                data = json.loads(line[6:])
            except json.JSONDecodeError:
                continue

            event_type = data.get("type", "unknown")
            cat = classify_event(event_type)
            event_counts[cat] = event_counts.get(cat, 0) + 1
            print_event(event_type, data, "responses")

    print("\n")
    print(colorize("task_notification", "━━ 이벤트 통계 ━━"))
    for cat, count in sorted(event_counts.items()):
        print(colorize(cat, f"  {cat}: {count}"))
    print()


def test_chat_completions_api():
    print("=" * 60)
    print(colorize("task_started", "▶ Chat Completions API (/v1/chat/completions) 테스트"))
    print("=" * 60)

    url = f"{BASE_URL}/v1/chat/completions"
    payload = {
        "model": "opus",
        "stream": True,
        "messages": [{"role": "user", "content": PROMPT}],
    }

    event_counts: dict[str, int] = {}

    headers = {"Authorization": f"Bearer {API_KEY}"} if API_KEY else {}

    with httpx.stream("POST", url, json=payload, headers=headers, timeout=120.0) as resp:
        if resp.status_code != 200:
            resp.read()
            print(colorize("error", f"HTTP {resp.status_code}: {resp.text}"))
            return

        for line in resp.iter_lines():
            if not line or not line.startswith("data: "):
                continue

            if line.strip() == "data: [DONE]":
                break

            try:
                data = json.loads(line[6:])
            except json.JSONDecodeError:
                continue

            # Chat Completions: system_event 필드에 구조화 이벤트
            system_event = data.get("system_event")
            if system_event:
                event_type = system_event.get("type", "unknown")
                cat = classify_event(event_type)
                event_counts[cat] = event_counts.get(cat, 0) + 1
                print_event(event_type, system_event, "chat")
                continue

            # 일반 텍스트 delta
            delta = data.get("choices", [{}])[0].get("delta", {})
            content = delta.get("content", "")
            if content:
                event_counts["text_delta"] = event_counts.get("text_delta", 0) + 1
                sys.stdout.write(colorize("text_delta", content))
                sys.stdout.flush()

    print("\n")
    print(colorize("task_notification", "━━ 이벤트 통계 ━━"))
    for cat, count in sorted(event_counts.items()):
        print(colorize(cat, f"  {cat}: {count}"))
    print()


if __name__ == "__main__":
    api = sys.argv[1] if len(sys.argv) > 1 else "both"

    if api in ("responses", "both"):
        test_responses_api()

    if api in ("chat", "both"):
        test_chat_completions_api()

    if api not in ("responses", "chat", "both"):
        print(f"Usage: python {sys.argv[0]} [responses|chat|both]")
