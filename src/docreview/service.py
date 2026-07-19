from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Callable

from .db import ReviewDatabase
from .evidence import create_evidence_images
from .matching import find_matches
from .models import AppSettings, KeywordRule, SUPPORTED_EXTENSIONS
from .ocr import create_ocr_engine
from .parsers import parse_document


ProgressCallback = Callable[[int, int, str], None]


class Analyzer:
    def __init__(self, settings: AppSettings, db: ReviewDatabase):
        self.settings = settings
        self.db = db

    def analyze_directory(
        self,
        source_dir: Path,
        rules: list[KeywordRule],
        progress: ProgressCallback | None = None,
    ) -> dict[str, int]:
        source_dir = source_dir.expanduser().resolve()
        if not source_dir.is_dir():
            raise ValueError(f"目录不存在: {source_dir}")
        if not rules:
            raise ValueError("至少需要一个关键词")
        files = self._discover_files(source_dir)
        ocr = create_ocr_engine(
            self.settings.ocr_backend,
            self.settings.root_dir,
            self.settings.paddle_device,
            self.settings.paddle_lang,
        )
        total = len(files)
        for index, path in enumerate(files, start=1):
            if progress:
                progress(index - 1, total, f"准备处理 {path.name}")
            self._analyze_file(path, rules, ocr)
            if progress:
                progress(index, total, f"已处理 {path.name}")
        return self.db.counts()

    def _discover_files(self, source_dir: Path) -> list[Path]:
        excluded_roots = {
            self.settings.data_dir.resolve(),
            self.settings.output_dir.resolve(),
            (self.settings.root_dir / ".runtime").resolve(),
            (self.settings.root_dir / ".venv").resolve(),
            (self.settings.root_dir / "node_modules").resolve(),
        }
        files: list[Path] = []
        for path in source_dir.rglob("*"):
            if not path.is_file() or path.name.startswith("."):
                continue
            resolved = path.resolve()
            if any(_is_relative_to(resolved, root) for root in excluded_roots):
                continue
            files.append(path)
        return sorted(files, key=lambda item: str(item).casefold())

    def _analyze_file(self, path: Path, rules: list[KeywordRule], ocr) -> None:
        sha256 = _hash_file(path)
        document_id = hashlib.sha256(
            f"{path.resolve()}\0{sha256}".encode("utf-8")
        ).hexdigest()[:24]
        extension = path.suffix.lower()
        file_type = SUPPORTED_EXTENSIONS.get(extension)
        if not file_type:
            self.db.start_document(
                document_id,
                sha256,
                path,
                "unsupported",
                status="unsupported",
                message=f"第一版暂不支持 {extension or '无扩展名'} 文件",
            )
            self.db.finish_document(
                document_id,
                "unsupported",
                0,
                f"第一版暂不支持 {extension or '无扩展名'} 文件",
            )
            return

        self.db.start_document(document_id, sha256, path, file_type)
        try:
            result = parse_document(
                path, file_type, document_id, self.settings, ocr
            )
            seen: set[tuple] = set()
            for block in result.blocks:
                block_id = self.db.insert_block(document_id, block)
                for match in find_matches(block.text, rules):
                    key = (
                        match.keyword.casefold(),
                        block.page_no,
                        round(block.bbox.x0, 3),
                        round(block.bbox.y0, 3),
                        round(block.bbox.x1, 3),
                        round(block.bbox.y1, 3),
                        block.text.casefold(),
                    )
                    if key in seen:
                        continue
                    seen.add(key)
                    token = (
                        f"{document_id}:{block.page_no}:{block.kind}:"
                        f"{match.keyword}:{match.start}:{block.bbox}"
                    )
                    crop, annotated = create_evidence_images(
                        block.page_image,
                        block.bbox,
                        self.settings.data_dir / "evidence" / document_id,
                        token,
                    )
                    self.db.insert_match(block_id, match, crop, annotated)
            message = "；".join(result.warnings)
            self.db.finish_document(document_id, "complete", result.page_count, message)
        except Exception as exc:
            self.db.finish_document(document_id, "error", 0, str(exc))


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
