from __future__ import annotations

import hashlib
from pathlib import Path

from PIL import Image, ImageDraw, ImageOps

from .models import BBox


def create_evidence_images(
    page_image: Path,
    bbox: BBox,
    output_dir: Path,
    token: str,
) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]
    crop_path = output_dir / f"{digest}-crop.png"
    annotated_path = output_dir / f"{digest}-page.png"
    if crop_path.exists() and annotated_path.exists():
        return crop_path, annotated_path

    with Image.open(page_image) as original:
        image = ImageOps.exif_transpose(original).convert("RGB")
    width, height = image.size
    box = bbox.clamped()
    left = int(box.x0 * width)
    top = int(box.y0 * height)
    right = max(left + 1, int(box.x1 * width))
    bottom = max(top + 1, int(box.y1 * height))

    annotated = image.copy()
    draw = ImageDraw.Draw(annotated)
    stroke = max(3, round(min(width, height) / 300))
    draw.rectangle((left, top, right, bottom), outline="#e11d48", width=stroke)
    annotated.save(annotated_path, format="PNG", optimize=True)

    margin_x = max(24, int((right - left) * 0.15))
    margin_y = max(20, int((bottom - top) * 0.8))
    crop_box = (
        max(0, left - margin_x),
        max(0, top - margin_y),
        min(width, right + margin_x),
        min(height, bottom + margin_y),
    )
    crop = annotated.crop(crop_box)
    max_width = 1400
    if crop.width > max_width:
        ratio = max_width / crop.width
        crop = crop.resize((max_width, max(1, int(crop.height * ratio))))
    crop.save(crop_path, format="PNG", optimize=True)
    return crop_path, annotated_path
