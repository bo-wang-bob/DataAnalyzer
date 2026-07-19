from __future__ import annotations

import json
import platform
import shutil
import subprocess
from pathlib import Path
from typing import Protocol

from .models import BBox, TextLine
from .platform_tools import subprocess_no_window_flag


class OCRUnavailable(RuntimeError):
    pass


class OCREngine(Protocol):
    name: str

    def recognize(self, image_path: Path) -> list[TextLine]: ...


class AppleVisionOCR:
    name = "apple-vision"

    def __init__(self, root_dir: Path):
        self.source = root_dir / "tools" / "vision_ocr.m"
        self.binary = root_dir / ".runtime" / "vision_ocr"

    def ensure_ready(self) -> None:
        clang = shutil.which("clang")
        if platform.system() != "Darwin" or not clang:
            raise OCRUnavailable("Apple Vision OCR 仅可在安装 Xcode 命令行工具的 macOS 上使用")
        needs_build = not self.binary.exists()
        if self.binary.exists() and self.source.exists():
            needs_build = self.binary.stat().st_mtime < self.source.stat().st_mtime
        if needs_build:
            self.binary.parent.mkdir(parents=True, exist_ok=True)
            module_cache = self.binary.parent / "clang-module-cache"
            module_cache.mkdir(parents=True, exist_ok=True)
            result = subprocess.run(
                [
                    clang,
                    "-O2",
                    "-fobjc-arc",
                    f"-fmodules-cache-path={module_cache}",
                    "-framework",
                    "Foundation",
                    "-framework",
                    "AppKit",
                    "-framework",
                    "Vision",
                    str(self.source),
                    "-o",
                    str(self.binary),
                ],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                raise OCRUnavailable(f"Apple Vision OCR 编译失败: {result.stderr.strip()}")

    def recognize(self, image_path: Path) -> list[TextLine]:
        self.ensure_ready()
        result = subprocess.run(
            [str(self.binary), str(image_path)],
            capture_output=True,
            text=True,
            creationflags=subprocess_no_window_flag(),
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "Apple Vision OCR 识别失败")
        raw = json.loads(result.stdout or "[]")
        lines: list[TextLine] = []
        for item in raw:
            x, y, width, height = [float(value) for value in item["bbox"]]
            # Vision uses a bottom-left origin; the application uses top-left.
            box = BBox(x, 1.0 - y - height, x + width, 1.0 - y).clamped()
            text = str(item.get("text", "")).strip()
            if text:
                lines.append(
                    TextLine(text, box, float(item.get("confidence", 0.0)))
                )
        return lines


class PaddleOCRBackend:
    name = "paddleocr"

    def __init__(self, device: str = "cpu", lang: str = "ch") -> None:
        try:
            from paddleocr import PaddleOCR  # type: ignore
        except ImportError as exc:
            raise OCRUnavailable(
                "未安装 PaddleOCR；请安装项目的 paddle 可选依赖"
            ) from exc
        try:
            self._ocr = PaddleOCR(
                lang=lang,
                device=device,
                use_doc_orientation_classify=True,
                use_doc_unwarping=True,
                use_textline_orientation=True,
            )
        except TypeError:
            self._ocr = PaddleOCR(
                lang=lang,
                use_angle_cls=True,
                use_gpu=device.lower().startswith("gpu"),
                show_log=False,
            )

    def recognize(self, image_path: Path) -> list[TextLine]:
        from PIL import Image

        with Image.open(image_path) as image:
            width, height = image.size
        lines: list[TextLine] = []
        if hasattr(self._ocr, "predict"):
            results = self._ocr.predict(input=str(image_path))
            for result in results:
                payload = getattr(result, "json", result)
                if callable(payload):
                    payload = payload()
                if isinstance(payload, str):
                    payload = json.loads(payload)
                data = payload.get("res", payload) if isinstance(payload, dict) else {}
                texts = data.get("rec_texts", [])
                scores = data.get("rec_scores", [])
                boxes = data.get("rec_boxes", [])
                for text, score, box in zip(texts, scores, boxes):
                    x0, y0, x1, y1 = [float(v) for v in box]
                    lines.append(
                        TextLine(
                            str(text),
                            BBox(x0 / width, y0 / height, x1 / width, y1 / height),
                            float(score),
                        )
                    )
            return lines

        # Compatibility path for PaddleOCR 2.x.
        result = self._ocr.ocr(str(image_path), cls=True)
        for page in result or []:
            for polygon, (text, score) in page or []:
                xs = [float(point[0]) for point in polygon]
                ys = [float(point[1]) for point in polygon]
                lines.append(
                    TextLine(
                        str(text),
                        BBox(min(xs) / width, min(ys) / height, max(xs) / width, max(ys) / height),
                        float(score),
                    )
                )
        return lines


def create_ocr_engine(
    backend: str,
    root_dir: Path,
    paddle_device: str = "cpu",
    paddle_lang: str = "ch",
) -> OCREngine:
    normalized = backend.strip().lower()
    if normalized in {"auto", "apple", "apple-vision"}:
        if platform.system() == "Darwin":
            engine = AppleVisionOCR(root_dir)
            engine.ensure_ready()
            return engine
        if normalized != "auto":
            raise OCRUnavailable("当前平台不支持 Apple Vision OCR")
    if normalized in {"auto", "paddle", "paddleocr"}:
        return PaddleOCRBackend(device=paddle_device, lang=paddle_lang)
    raise OCRUnavailable(f"未知 OCR 后端: {backend}")
