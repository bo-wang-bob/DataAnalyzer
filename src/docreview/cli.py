from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import load_settings
from .db import ReviewDatabase
from .doctor import required_checks_pass, run_diagnostics
from .exporter import export_review_workbook
from .matching import parse_keyword_text
from .service import Analyzer
from .web import run_server


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="本地文档关键词证据审核")
    parser.add_argument("--config", type=Path, help="JSON 配置文件")
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve = subparsers.add_parser("serve", help="启动本地审核页面")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)

    server = subparsers.add_parser("server", help="启动 Linux 云服务器上传分析版")
    server.add_argument("--host", default="0.0.0.0")
    server.add_argument("--port", type=int, default=8765)
    server.add_argument("--storage", type=Path, default=Path(".server-data"))
    server.add_argument("--max-upload-mb", type=int, default=2048)
    server.add_argument("--max-files", type=int, default=2000)

    scan = subparsers.add_parser("scan", help="在命令行分析目录")
    scan.add_argument("--source", type=Path, default=Path("datas"))
    scan.add_argument("--keywords", nargs="*", default=[])
    scan.add_argument("--keywords-file", type=Path)
    scan.add_argument("--max-pages", type=int)

    export = subparsers.add_parser("export", help="导出 Excel 审核汇总")
    export.add_argument("--filename", default="document_review_results.xlsx")
    subparsers.add_parser("doctor", help="检查当前系统依赖和平台配置")
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    root_dir = Path.cwd().resolve()
    settings = load_settings(root_dir, args.config)
    db = ReviewDatabase(settings.data_dir / "docreview.db")

    if args.command == "doctor":
        checks = run_diagnostics(settings)
        for check in checks:
            marker = "OK" if check.ok else ("WARN" if not check.required else "FAIL")
            print(f"[{marker:4}] {check.name}: {check.detail}")
        if not required_checks_pass(checks):
            raise SystemExit(1)
        return

    if args.command == "serve":
        run_server(settings, db, args.host, args.port)
        return
    if args.command == "server":
        from .server import run_production_server

        storage = args.storage if args.storage.is_absolute() else root_dir / args.storage
        run_production_server(
            settings,
            storage,
            args.host,
            args.port,
            args.max_upload_mb,
            args.max_files,
        )
        return
    if args.command == "export":
        path = export_review_workbook(db, settings, args.filename)
        print(path)
        return
    if args.command == "scan":
        if args.max_pages:
            settings.max_pages = args.max_pages
        keyword_text = "\n".join(args.keywords)
        if args.keywords_file:
            keyword_text += "\n" + args.keywords_file.read_text(encoding="utf-8-sig")
        rules = parse_keyword_text(keyword_text)
        if not rules:
            print("错误：请通过 --keywords 或 --keywords-file 提供关键词", file=sys.stderr)
            raise SystemExit(2)
        source = args.source if args.source.is_absolute() else root_dir / args.source
        analyzer = Analyzer(settings, db)

        def progress(current: int, total: int, message: str) -> None:
            print(f"[{current}/{total}] {message}")

        counts = analyzer.analyze_directory(source, rules, progress)
        print(
            f"完成：文件 {counts['documents']}，命中 {counts['matches']}，"
            f"不支持 {counts['unsupported']}，失败 {counts['errors']}"
        )


if __name__ == "__main__":
    main()
