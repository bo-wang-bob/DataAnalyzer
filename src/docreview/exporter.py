from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from .db import ReviewDatabase
from .models import AppSettings
from .platform_tools import (
    find_artifact_node_modules,
    find_node,
    subprocess_no_window_flag,
)


class ExportError(RuntimeError):
    pass


def export_review_workbook(
    db: ReviewDatabase,
    settings: AppSettings,
    filename: str = "document_review_results.xlsx",
) -> Path:
    artifact_modules = find_artifact_node_modules(
        settings.root_dir, settings.artifact_node_modules
    )
    node = find_node(settings.root_dir, settings.node_path, artifact_modules)
    if not node or not artifact_modules:
        from .xlsx_fallback import export_with_xlsxwriter

        try:
            return export_with_xlsxwriter(db, settings, filename)
        except Exception as exc:
            raise ExportError(str(exc)) from exc
    builder = settings.root_dir / "tools" / "build_review_workbook.mjs"
    if not builder.exists():
        raise ExportError(f"缺少 Excel 构建器: {builder}")
    matches = _with_display_source_paths(
        db.list_matches(limit=100_000), settings
    )
    unsupported = _with_display_source_paths(db.list_unsupported(), settings)
    payload = {
        "matches": matches,
        "unsupported": unsupported,
        "counts": db.counts(),
    }
    exchange_dir = settings.data_dir / "export"
    exchange_dir.mkdir(parents=True, exist_ok=True)
    input_json = exchange_dir / "review_data.json"
    input_json.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    output_path = settings.output_dir / filename
    qa_dir = exchange_dir / "qa"
    environment = os.environ.copy()
    environment["DOCREVIEW_NODE_MODULES"] = str(artifact_modules)
    result = subprocess.run(
        [str(node), str(builder), str(input_json), str(output_path), str(qa_dir)],
        cwd=settings.root_dir,
        capture_output=True,
        text=True,
        timeout=300,
        env=environment,
        creationflags=subprocess_no_window_flag(),
    )
    if result.returncode != 0 or not output_path.exists():
        detail = _subprocess_error_detail(result.stderr, result.stdout)
        raise ExportError(detail)
    return output_path


def _subprocess_error_detail(stderr: str, stdout: str) -> str:
    raw = stderr.strip() or stdout.strip()
    if not raw:
        return "Excel 导出失败"
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    for prefix in ("Error:", "TypeError:", "RangeError:", "ReferenceError:"):
        for line in lines:
            if line.startswith(prefix):
                return line[:1000]
    for line in reversed(lines):
        if not line.startswith(("at ", "Node.js ")) and len(line) <= 1000:
            return line
    return "Excel 构建器执行失败；请在终端运行 docreview export 查看详细日志"


def _with_display_source_paths(rows: list[dict], settings: AppSettings) -> list[dict]:
    upload_root = settings.data_dir.parent / "uploads"
    if not upload_root.is_dir():
        return rows
    root = upload_root.resolve()
    result: list[dict] = []
    for row in rows:
        item = dict(row)
        try:
            item["source_path"] = Path(str(item.get("source_path", ""))).resolve().relative_to(root).as_posix()
        except ValueError:
            pass
        result.append(item)
    return result
