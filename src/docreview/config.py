from __future__ import annotations

import json
from pathlib import Path

from .models import AppSettings


def load_settings(root_dir: Path, config_path: Path | None = None) -> AppSettings:
    settings = AppSettings.defaults(root_dir.resolve())
    if config_path is None:
        candidate = root_dir / "settings.json"
        config_path = candidate if candidate.exists() else None
    if config_path and config_path.exists():
        raw = json.loads(config_path.read_text(encoding="utf-8-sig"))
        settings.data_dir = _resolve(root_dir, raw.get("data_dir", ".data"))
        settings.output_dir = _resolve(root_dir, raw.get("output_dir", "output"))
        settings.ocr_backend = str(raw.get("ocr_backend", "auto"))
        settings.render_dpi = int(raw.get("render_dpi", 180))
        settings.native_text_min_chars = int(raw.get("native_text_min_chars", 20))
        settings.embedded_image_min_area = float(raw.get("embedded_image_min_area", 0.02))
        value = raw.get("max_pages")
        settings.max_pages = int(value) if value not in (None, "") else None
        settings.paddle_device = str(raw.get("paddle_device", "cpu"))
        settings.paddle_lang = str(raw.get("paddle_lang", "ch"))
        settings.libreoffice_path = _resolve_optional(root_dir, raw.get("libreoffice_path"))
        settings.pdftoppm_path = _resolve_optional(root_dir, raw.get("pdftoppm_path"))
        settings.node_path = _resolve_optional(root_dir, raw.get("node_path"))
        settings.artifact_node_modules = _resolve_optional(
            root_dir, raw.get("artifact_node_modules")
        )
    settings.ensure_directories()
    return settings


def _resolve(root: Path, value: str) -> Path:
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (root / path).resolve()


def _resolve_optional(root: Path, value: str | None) -> Path | None:
    if not value:
        return None
    return _resolve(root, str(value))
