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
    if not node:
        raise ExportError(
            "未找到 Node.js。请安装 Node.js，或在 settings.json 设置 node_path"
        )
    if not artifact_modules:
        raise ExportError(
            "未找到 @oai/artifact-tool。请设置 DOCREVIEW_NODE_MODULES 或 "
            "settings.json 的 artifact_node_modules"
        )
    builder = settings.root_dir / "tools" / "build_review_workbook.mjs"
    if not builder.exists():
        raise ExportError(f"缺少 Excel 构建器: {builder}")
    matches = db.list_matches(limit=100_000)
    unsupported = db.list_unsupported()
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
        detail = result.stderr.strip() or result.stdout.strip() or "Excel 导出失败"
        raise ExportError(detail)
    return output_path
