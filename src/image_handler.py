"""Image decoding, saving, and cleanup for Claude Code Read tool integration.

Accepts base64 image data from OpenAI, Anthropic, and Responses API formats,
saves to disk in the backend's working directory, and returns absolute file
paths that Claude Code can read natively via its Read tool.

Only synchronous operations — no remote URL fetching (SSRF-free).
"""

import base64
import binascii
import hashlib
import logging
import tempfile
import time
from pathlib import Path
from typing import Tuple

logger = logging.getLogger(__name__)

SUPPORTED_MEDIA_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
EXTENSION_MAP = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
}
MAX_IMAGE_SIZE = 5 * 1024 * 1024  # 5 MB (under 10 MB request body limit)


class ImageHandler:
    """Synchronous image file manager for a specific backend workspace."""

    def __init__(self, base_dir: Path):
        self.image_dir = base_dir / ".claude_images"
        try:
            self.image_dir.mkdir(parents=True, exist_ok=True)
        except PermissionError:
            fallback = Path(tempfile.mkdtemp(prefix="claude_images_"))
            logger.warning(
                "Cannot create %s (read-only?). Using fallback: %s",
                self.image_dir,
                fallback,
            )
            self.image_dir = fallback

    # ------------------------------------------------------------------
    # Core: decode + save
    # ------------------------------------------------------------------

    def save_base64_image(self, data: str, media_type: str) -> Path:
        """Decode base64 *data* and write to disk.  Returns the absolute path."""
        if media_type not in SUPPORTED_MEDIA_TYPES:
            raise ValueError(
                f"Unsupported image type: {media_type}. "
                f"Supported: {', '.join(sorted(SUPPORTED_MEDIA_TYPES))}"
            )

        try:
            image_bytes = base64.b64decode(data, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("Malformed image base64 payload") from exc

        if len(image_bytes) > MAX_IMAGE_SIZE:
            raise ValueError(
                f"Image size {len(image_bytes)} bytes exceeds {MAX_IMAGE_SIZE} byte limit"
            )

        content_hash = hashlib.sha256(image_bytes).hexdigest()[:16]
        ext = EXTENSION_MAP[media_type]
        filepath = self.image_dir / f"img_{content_hash}{ext}"

        if not filepath.exists():
            filepath.write_bytes(image_bytes)
            logger.debug("Saved image (%d bytes): %s", len(image_bytes), filepath.name)

        return filepath.resolve()

    # ------------------------------------------------------------------
    # Data-URL parsing
    # ------------------------------------------------------------------

    @staticmethod
    def parse_data_url(data_url: str) -> Tuple[str, str]:
        """Parse ``data:image/png;base64,...`` into *(media_type, base64_data)*.

        Raises ``ValueError`` for non-data URLs or malformed payloads.
        """
        if not data_url.startswith("data:"):
            raise ValueError("Only data: URLs are supported for images (remote URLs not supported)")
        header, sep, b64data = data_url.partition(",")
        if not sep or not b64data:
            raise ValueError("Malformed data URL: missing base64 payload")
        # header looks like  "data:image/png;base64"
        media_type = header.split(":")[1].split(";")[0]
        return media_type, b64data

    # ------------------------------------------------------------------
    # Format-specific entry points
    # ------------------------------------------------------------------

    def save_responses_image(self, image_url: str) -> Path:
        """Responses API format: *image_url* is a ``data:`` URL string."""
        media_type, b64data = self.parse_data_url(image_url)
        return self.save_base64_image(b64data, media_type)

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def cleanup(self, max_age_seconds: int = 3600) -> int:
        """Remove image files older than *max_age_seconds*.  Returns count removed."""
        if not self.image_dir.exists():
            return 0
        cutoff = time.time() - max_age_seconds
        removed = 0
        for f in self.image_dir.iterdir():
            if f.is_file() and f.stat().st_mtime < cutoff:
                f.unlink(missing_ok=True)
                removed += 1
        if removed:
            logger.debug("Cleaned up %d old image file(s)", removed)
        return removed
