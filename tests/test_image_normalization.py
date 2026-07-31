"""Palette/greyscale image normalization (issue #143).

Some vLLM vision backends fail to decode palette/indexed-color (Pillow mode
``P``) PNGs — the encoding screenshot and icon tools emit constantly — with
``unrecognized data stream contents when reading image file``. The 500 is
wrapped upstream and surfaces as the model hallucinating an image or claiming
there is none, so the bytes are normalized before they leave the gateway.
"""

import base64
import io

import pytest
from PIL import Image

from src.image_handler import MAX_IMAGE_SIZE, ImageHandler


def _png(mode: str, size=(8, 8), **info) -> bytes:
    buffer = io.BytesIO()
    img = Image.new(mode, size)
    img.save(buffer, format="PNG", **info)
    return buffer.getvalue()


def _data_url(payload: bytes, media_type: str = "image/png") -> str:
    return f"data:{media_type};base64,{base64.b64encode(payload).decode()}"


def _mode_of(payload: bytes) -> str:
    with Image.open(io.BytesIO(payload)) as img:
        return img.mode


class TestNormalizeImageBytes:
    def test_palette_png_becomes_truecolor(self):
        original = _png("P")
        assert _mode_of(original) == "P"
        normalized, media_type = ImageHandler.normalize_image_bytes(original, "image/png")
        assert normalized is not original
        assert media_type == "image/png"
        assert _mode_of(normalized) in ("RGB", "RGBA")

    def test_greyscale_png_becomes_truecolor(self):
        normalized, _ = ImageHandler.normalize_image_bytes(_png("L"), "image/png")
        assert _mode_of(normalized) == "RGB"

    def test_bilevel_png_becomes_truecolor(self):
        normalized, _ = ImageHandler.normalize_image_bytes(_png("1"), "image/png")
        assert _mode_of(normalized) == "RGB"

    def test_transparency_is_preserved(self):
        """Icons ride on palette PNGs with transparency — alpha must survive."""
        normalized, _ = ImageHandler.normalize_image_bytes(
            _png("P", transparency=0), "image/png"
        )
        assert _mode_of(normalized) == "RGBA"

    def test_rgb_png_is_untouched(self):
        original = _png("RGB")
        normalized, media_type = ImageHandler.normalize_image_bytes(original, "image/png")
        # Fast path: identity, not a re-encode (the common case stays cheap).
        assert normalized is original
        assert media_type == "image/png"

    def test_rgba_png_is_untouched(self):
        original = _png("RGBA")
        assert ImageHandler.normalize_image_bytes(original, "image/png")[0] is original

    def test_jpeg_is_untouched(self):
        buffer = io.BytesIO()
        Image.new("RGB", (8, 8)).save(buffer, format="JPEG")
        original = buffer.getvalue()
        normalized, media_type = ImageHandler.normalize_image_bytes(original, "image/jpeg")
        assert normalized is original
        assert media_type == "image/jpeg"

    def test_animated_gif_keeps_its_frames(self):
        buffer = io.BytesIO()
        # 프레임이 실제로 달라야 Pillow가 애니메이션으로 기록한다
        frames = [Image.new("RGB", (8, 8), color=(i * 80, 0, 0)).convert("P") for i in range(3)]
        frames[0].save(
            buffer, format="GIF", save_all=True, append_images=frames[1:], duration=100, loop=0
        )
        original = buffer.getvalue()
        with Image.open(io.BytesIO(original)) as probe:
            assert probe.n_frames == 3  # fixture sanity
        # Flattening an animation would silently drop information.
        assert ImageHandler.normalize_image_bytes(original, "image/gif")[0] is original

    def test_undecodable_bytes_pass_through(self):
        """A compatibility nudge must not become a new validation gate."""
        junk = b"not an image at all"
        normalized, media_type = ImageHandler.normalize_image_bytes(junk, "image/png")
        assert normalized is junk
        assert media_type == "image/png"

    def test_oversize_after_reencode_is_rejected(self, monkeypatch):
        """Re-encoding can grow a payload — the size limit still governs."""
        monkeypatch.setattr("src.image_handler.MAX_IMAGE_SIZE", 32)
        with pytest.raises(ValueError, match="after normalization"):
            ImageHandler.normalize_image_bytes(_png("P", size=(64, 64)), "image/png")


class TestDataUrlToImageBlock:
    def test_palette_png_block_carries_normalized_payload(self):
        block = ImageHandler.data_url_to_image_block(_data_url(_png("P")))
        assert block["type"] == "image"
        assert block["source"]["media_type"] == "image/png"
        payload = base64.b64decode(block["source"]["data"])
        assert _mode_of(payload) in ("RGB", "RGBA")

    def test_rgb_png_block_keeps_original_payload(self):
        original = _png("RGB")
        block = ImageHandler.data_url_to_image_block(_data_url(original))
        assert base64.b64decode(block["source"]["data"]) == original

    def test_unsupported_media_type_still_rejected(self):
        with pytest.raises(ValueError, match="Unsupported image type"):
            ImageHandler.data_url_to_image_block(_data_url(b"x", "image/bmp"))

    def test_malformed_base64_still_rejected(self):
        with pytest.raises(ValueError, match="Malformed image base64"):
            ImageHandler.data_url_to_image_block("data:image/png;base64,!!!!")

    def test_oversize_still_rejected(self, monkeypatch):
        monkeypatch.setattr("src.image_handler.MAX_IMAGE_SIZE", 8)
        with pytest.raises(ValueError, match="exceeds"):
            ImageHandler.data_url_to_image_block(_data_url(_png("RGB", size=(64, 64))))


class TestSavePathNormalization:
    def test_saved_palette_png_is_truecolor(self, tmp_path):
        """A Read-tool image reaches the same backend — normalize it too."""
        handler = ImageHandler(tmp_path)
        path = handler.save_base64_image(base64.b64encode(_png("P")).decode(), "image/png")
        assert _mode_of(path.read_bytes()) in ("RGB", "RGBA")
        assert path.suffix == ".png"

    def test_saved_rgb_png_is_byte_identical(self, tmp_path):
        handler = ImageHandler(tmp_path)
        original = _png("RGB")
        path = handler.save_base64_image(base64.b64encode(original).decode(), "image/png")
        assert path.read_bytes() == original

    def test_max_image_size_constant_unchanged(self):
        # Guard the documented contract (5 MB, under the 10 MB body limit).
        assert MAX_IMAGE_SIZE == 5 * 1024 * 1024
