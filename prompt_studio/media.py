"""Reference image validation and PNG normalization."""

import io
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError


def normalize_png(data: bytes) -> bytes:
    if not 0 < len(data) <= 15_000_000:
        raise ValueError("Reference images must be nonempty and at most 15 MB.")
    try:
        with Image.open(io.BytesIO(data)) as original:
            if (
                original.format not in {"PNG", "JPEG", "WEBP"}
                or getattr(original, "n_frames", 1) != 1
            ):
                raise ValueError("Use a single PNG, JPEG, or WebP reference image.")
            if original.width * original.height > 36_000_000:
                raise ValueError("Reference images must be at most 36 megapixels.")
            normalized = ImageOps.exif_transpose(original).convert("RGBA")
            normalized.thumbnail((1536, 1536))
            output = io.BytesIO()
            normalized.save(output, "PNG")
            return output.getvalue()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ValueError("Could not read the reference image. Use PNG, JPEG, or WebP.") from exc


def normalize_image(data: bytes, output: Path) -> Path:
    output.write_bytes(normalize_png(data))
    return output
