"""Examples should document the currently supported Responses API."""

from pathlib import Path


EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "examples"
STREAMING_DOC = Path(__file__).resolve().parent.parent / "docs" / "streaming-events.md"


def test_examples_do_not_reference_removed_chat_completion_api():
    combined = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(EXAMPLES_DIR.iterdir())
        if path.is_file()
    )

    assert "/v1/chat/completions" not in combined
    assert "chat.completions" not in combined
    assert 'extra_body={"session_id"' not in combined


def test_streaming_events_doc_only_describes_responses_api():
    content = STREAMING_DOC.read_text(encoding="utf-8")

    assert "/v1/chat/completions" not in content
    assert "/v1/messages" not in content
    assert "chat.completion" not in content
