"""Unit tests for src.image_handler."""

import base64
import os
import time
from pathlib import Path

import pytest

from src.image_handler import ImageHandler, MAX_IMAGE_SIZE

# Helper: create a tiny valid PNG (1x1 pixel red)
TINY_PNG = base64.b64encode(
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
    b"\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00"
    b"\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00"
    b"\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
).decode()

TINY_JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"\x00" * 100 + b"\xff\xd9"
TINY_JPEG = base64.b64encode(TINY_JPEG_BYTES).decode()


def test_save_base64_image_png(tmp_path):
    """Save a PNG image and verify the file exists with .png extension."""
    handler = ImageHandler(tmp_path)
    path = handler.save_base64_image(TINY_PNG, "image/png")
    assert path.exists()
    assert path.suffix == ".png"
    assert path.parent == (tmp_path / ".claude_images").resolve()


def test_save_base64_image_jpeg(tmp_path):
    """Save a JPEG image and verify the file exists with .jpg extension."""
    handler = ImageHandler(tmp_path)
    path = handler.save_base64_image(TINY_JPEG, "image/jpeg")
    assert path.exists()
    assert path.suffix == ".jpg"


def test_unsupported_media_type(tmp_path):
    """Unsupported media type raises ValueError."""
    handler = ImageHandler(tmp_path)
    with pytest.raises(ValueError, match="Unsupported image type"):
        handler.save_base64_image(TINY_PNG, "image/bmp")


def test_image_too_large(tmp_path):
    """Image data exceeding 5 MB raises ValueError."""
    handler = ImageHandler(tmp_path)
    # Create base64 data that decodes to > 5 MB
    large_bytes = b"\x00" * (MAX_IMAGE_SIZE + 1)
    large_b64 = base64.b64encode(large_bytes).decode()
    with pytest.raises(ValueError, match="exceeds"):
        handler.save_base64_image(large_b64, "image/png")


def test_content_hash_dedup(tmp_path):
    """Same image saved twice returns the same path; only one file is created."""
    handler = ImageHandler(tmp_path)
    path1 = handler.save_base64_image(TINY_PNG, "image/png")
    path2 = handler.save_base64_image(TINY_PNG, "image/png")
    assert path1 == path2
    image_files = list((tmp_path / ".claude_images").iterdir())
    assert len(image_files) == 1


def test_different_images_different_paths(tmp_path):
    """Two different images produce different file paths."""
    handler = ImageHandler(tmp_path)
    path_png = handler.save_base64_image(TINY_PNG, "image/png")
    path_jpeg = handler.save_base64_image(TINY_JPEG, "image/jpeg")
    assert path_png != path_jpeg


def test_parse_data_url_valid(tmp_path):
    """Parsing a well-formed data URL returns (media_type, base64_data)."""
    media_type, data = ImageHandler.parse_data_url("data:image/png;base64,abc123")
    assert media_type == "image/png"
    assert data == "abc123"


def test_parse_data_url_no_data_prefix(tmp_path):
    """Non-data URL raises ValueError."""
    with pytest.raises(ValueError, match="Only data: URLs"):
        ImageHandler.parse_data_url("https://example.com/img.png")


def test_parse_data_url_malformed(tmp_path):
    """Data URL without comma separator raises ValueError."""
    with pytest.raises(ValueError, match="Malformed data URL"):
        ImageHandler.parse_data_url("data:image/png;base64")


def test_save_openai_format(tmp_path):
    """OpenAI image_url object with data URL is saved correctly."""
    handler = ImageHandler(tmp_path)
    path = handler.save_openai_image({"url": f"data:image/png;base64,{TINY_PNG}"})
    assert path.exists()
    assert path.suffix == ".png"


def test_save_openai_missing_url(tmp_path):
    """OpenAI image_url object missing 'url' raises ValueError."""
    handler = ImageHandler(tmp_path)
    with pytest.raises(ValueError, match="missing 'url' field"):
        handler.save_openai_image({})


def test_save_anthropic_format(tmp_path):
    """Anthropic base64 source object is saved correctly."""
    handler = ImageHandler(tmp_path)
    path = handler.save_anthropic_image(
        {"type": "base64", "media_type": "image/png", "data": TINY_PNG}
    )
    assert path.exists()
    assert path.suffix == ".png"


def test_save_anthropic_non_base64(tmp_path):
    """Anthropic source with non-base64 type raises ValueError."""
    handler = ImageHandler(tmp_path)
    with pytest.raises(ValueError, match="Only base64 image source supported"):
        handler.save_anthropic_image(
            {"type": "url", "media_type": "image/png", "data": "https://example.com/img.png"}
        )


def test_save_responses_format(tmp_path):
    """Responses API data URL string is saved correctly."""
    handler = ImageHandler(tmp_path)
    path = handler.save_responses_image(f"data:image/png;base64,{TINY_PNG}")
    assert path.exists()
    assert path.suffix == ".png"


def test_cleanup_old_images(tmp_path):
    """Files older than max_age_seconds are removed by cleanup."""
    handler = ImageHandler(tmp_path)
    path = handler.save_base64_image(TINY_PNG, "image/png")
    # Set mtime to 2 hours ago
    old_time = time.time() - 7200
    os.utime(path, (old_time, old_time))
    removed = handler.cleanup(max_age_seconds=3600)
    assert removed == 1
    assert not path.exists()


def test_cleanup_preserves_recent(tmp_path):
    """Recent files are not removed by cleanup."""
    handler = ImageHandler(tmp_path)
    path = handler.save_base64_image(TINY_PNG, "image/png")
    removed = handler.cleanup(max_age_seconds=3600)
    assert removed == 0
    assert path.exists()


def test_image_dir_created(tmp_path):
    """ImageHandler creates the .claude_images directory on init."""
    target = tmp_path / "subdir"
    # subdir does not exist yet
    assert not target.exists()
    ImageHandler(target)
    assert (target / ".claude_images").is_dir()


def test_readonly_base_dir_falls_back_to_temp(tmp_path, monkeypatch):
    """When base_dir mkdir raises PermissionError, ImageHandler falls back to temp."""
    from unittest.mock import patch

    original_mkdir = Path.mkdir

    def _fail_mkdir(self, *args, **kwargs):
        if ".claude_images" in str(self):
            raise PermissionError("read-only filesystem")
        return original_mkdir(self, *args, **kwargs)

    with patch.object(Path, "mkdir", _fail_mkdir):
        handler = ImageHandler(tmp_path)

    # Should have fallen back — image_dir is NOT under tmp_path
    assert handler.image_dir != tmp_path / ".claude_images"
    assert handler.image_dir.exists()
    # Saving should still work
    path = handler.save_base64_image(TINY_PNG, "image/png")
    assert path.exists()
