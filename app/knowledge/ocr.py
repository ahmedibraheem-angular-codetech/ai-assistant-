"""Local OCR for images using macOS Vision (Arabic and English). No downloads.

The Vision framework is called through JavaScript for Automation
(``osascript -l JavaScript``, see ocr_vision.js), so no compiler or extra
package is needed. Image bytes go through a private temp file and nothing
leaves the machine. On other systems image files are rejected clearly.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from app.files.exceptions import FileAccessError

IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".heic", ".tiff", ".tif", ".bmp", ".gif", ".webp"})
OCR_TIMEOUT_SECONDS = 60
MAX_IMAGE_BYTES = 20 * 1024 * 1024
_SCRIPT = Path(__file__).with_name("ocr_vision.js")


def available() -> bool:
    return sys.platform == "darwin" and shutil.which("osascript") is not None and _SCRIPT.is_file()


def image_text(data: bytes, extension: str) -> str:
    """Return the text recognized in one image."""
    if not available():
        raise FileAccessError("Image text recognition needs macOS.")
    if len(data) > MAX_IMAGE_BYTES:
        raise FileAccessError("That image is too large to read.")
    descriptor, path = tempfile.mkstemp(suffix=extension.lower())
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
        os.chmod(path, 0o600)
        result = subprocess.run(
            ["osascript", "-l", "JavaScript", str(_SCRIPT), path],
            capture_output=True, text=True, timeout=OCR_TIMEOUT_SECONDS,
            env={"PATH": "/usr/bin:/bin", "HOME": tempfile.gettempdir()},
        )
    except subprocess.TimeoutExpired as exc:
        raise FileAccessError("Reading the image took too long.") from exc
    finally:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
    output = result.stdout.strip()
    if result.returncode != 0 or output == "ERROR":
        raise FileAccessError("Text recognition failed for that image.")
    return output
