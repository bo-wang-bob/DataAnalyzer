from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


SUPPORTED_EXTENSIONS = {
    ".pdf": "pdf",
    ".doc": "word",
    ".docx": "word",
    ".png": "image",
    ".jpg": "image",
    ".jpeg": "image",
    ".tif": "image",
    ".tiff": "image",
    ".bmp": "image",
    ".webp": "image",
}


@dataclass(frozen=True)
class BBox:
    """Normalized top-left coordinates in the range 0..1."""

    x0: float
    y0: float
    x1: float
    y1: float

    def clamped(self) -> "BBox":
        return BBox(
            max(0.0, min(1.0, self.x0)),
            max(0.0, min(1.0, self.y0)),
            max(0.0, min(1.0, self.x1)),
            max(0.0, min(1.0, self.y1)),
        )

    @property
    def width(self) -> float:
        return max(0.0, self.x1 - self.x0)

    @property
    def height(self) -> float:
        return max(0.0, self.y1 - self.y0)

    @property
    def area(self) -> float:
        return self.width * self.height

    def union(self, other: "BBox") -> "BBox":
        return BBox(
            min(self.x0, other.x0),
            min(self.y0, other.y0),
            max(self.x1, other.x1),
            max(self.y1, other.y1),
        )

    def map_inside(self, outer: "BBox") -> "BBox":
        return BBox(
            outer.x0 + self.x0 * outer.width,
            outer.y0 + self.y0 * outer.height,
            outer.x0 + self.x1 * outer.width,
            outer.y0 + self.y1 * outer.height,
        ).clamped()


@dataclass
class TextLine:
    text: str
    bbox: BBox
    confidence: float = 1.0


@dataclass
class ContentBlock:
    page_no: int
    kind: str
    text: str
    bbox: BBox
    confidence: float
    page_image: Path
    source_detail: str = ""


@dataclass
class ParseResult:
    blocks: list[ContentBlock] = field(default_factory=list)
    page_count: int = 0
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class KeywordRule:
    value: str
    mode: str = "literal"


@dataclass(frozen=True)
class BlockMatch:
    keyword: str
    matched_text: str
    start: int
    end: int


@dataclass
class AppSettings:
    root_dir: Path
    data_dir: Path
    output_dir: Path
    ocr_backend: str = "auto"
    render_dpi: int = 180
    native_text_min_chars: int = 20
    embedded_image_min_area: float = 0.02
    max_pages: int | None = None
    paddle_device: str = "cpu"
    paddle_lang: str = "ch"
    word_backend: str = "auto"
    libreoffice_path: Path | None = None
    pdftoppm_path: Path | None = None
    node_path: Path | None = None
    artifact_node_modules: Path | None = None

    @classmethod
    def defaults(cls, root_dir: Path) -> "AppSettings":
        return cls(
            root_dir=root_dir,
            data_dir=root_dir / ".data",
            output_dir=root_dir / "output",
        )

    def ensure_directories(self) -> None:
        for path in (self.data_dir, self.output_dir):
            path.mkdir(parents=True, exist_ok=True)


JsonDict = dict[str, Any]
