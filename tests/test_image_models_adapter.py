"""Unit tests for image support in models and message adapter."""

import base64
from pathlib import Path
from unittest.mock import MagicMock


from src.models import ContentPart, Message
from src.message_adapter import MessageAdapter
from src.response_models import ResponseInputItem

# Tiny PNG for testing
TINY_PNG_B64 = base64.b64encode(
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
    b"\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00"
    b"\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00"
    b"\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
).decode()


# ============================================================================
# Model tests
# ============================================================================


class TestContentPart:
    def test_content_part_text(self):
        part = ContentPart(type="text", text="hello")
        assert part.type == "text"
        assert part.text == "hello"
        assert part.image_url is None

    def test_content_part_image_url(self):
        data_url = f"data:image/png;base64,{TINY_PNG_B64}"
        part = ContentPart(type="image_url", image_url={"url": data_url})
        assert part.type == "image_url"
        assert part.image_url["url"] == data_url
        assert part.text is None


class TestMessageNormalization:
    def test_message_text_only_list_normalized(self):
        """Text-only content list is collapsed to a single string."""
        msg = Message(
            role="user",
            content=[
                ContentPart(type="text", text="a"),
                ContentPart(type="text", text="b"),
            ],
        )
        assert isinstance(msg.content, str)
        assert msg.content == "a\nb"

    def test_message_with_images_kept_as_list(self):
        """Content list with image_url parts is preserved as a list."""
        data_url = f"data:image/png;base64,{TINY_PNG_B64}"
        msg = Message(
            role="user",
            content=[
                ContentPart(type="text", text="describe this"),
                ContentPart(type="image_url", image_url={"url": data_url}),
            ],
        )
        assert isinstance(msg.content, list)
        assert len(msg.content) == 2
        assert msg.content[0].type == "text"
        assert msg.content[1].type == "image_url"

    def test_message_string_content_unchanged(self):
        """Plain string content passes through without modification."""
        msg = Message(role="user", content="hello")
        assert msg.content == "hello"


# ============================================================================
# Adapter tests
# ============================================================================


def _make_mock_handler():
    handler = MagicMock()
    handler.save_openai_image.return_value = Path("/tmp/test/img_abc123.png")
    handler.save_responses_image.return_value = Path("/tmp/test/img_abc123.png")
    return handler


class TestFilterContent:
    def test_filter_content_preserves_attached_image(self):
        """<attached_image> references are NOT stripped by filter_content."""
        text = 'Here is the image: <attached_image path="/tmp/img.png" />'
        result = MessageAdapter.filter_content(text)
        assert '<attached_image path="/tmp/img.png" />' in result

    def test_filter_content_strips_base64_data(self):
        """Raw data:image/... base64 strings are stripped."""
        text = f"Look: data:image/png;base64,{TINY_PNG_B64} and more text"
        result = MessageAdapter.filter_content(text)
        assert f"data:image/png;base64,{TINY_PNG_B64}" not in result
        assert "[base64 image data removed]" in result
        assert "and more text" in result


class TestResponseInputWithImages:
    def test_response_input_with_image(self):
        """response_input_to_prompt with input_image part and handler inserts marker."""
        handler = _make_mock_handler()
        data_url = f"data:image/png;base64,{TINY_PNG_B64}"
        items = [
            ResponseInputItem(
                role="user",
                content=[
                    {"type": "input_text", "text": "describe"},
                    {"type": "input_image", "image_url": data_url},
                ],
            ),
        ]
        result = MessageAdapter.response_input_to_prompt(items, image_handler=handler)
        assert "describe" in result
        assert '<attached_image path="' in result
        assert "/tmp/test/img_abc123.png" in result
        handler.save_responses_image.assert_called_once_with(data_url)

    def test_response_input_without_handler(self):
        """response_input_to_prompt with input_image but no handler skips the image."""
        data_url = f"data:image/png;base64,{TINY_PNG_B64}"
        items = [
            ResponseInputItem(
                role="user",
                content=[
                    {"type": "input_text", "text": "describe"},
                    {"type": "input_image", "image_url": data_url},
                ],
            ),
        ]
        result = MessageAdapter.response_input_to_prompt(items, image_handler=None)
        assert "describe" in result
        assert "attached_image" not in result
