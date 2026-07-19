from __future__ import annotations

from pathlib import Path

from PIL import Image

from .db import ReviewDatabase
from .models import AppSettings


def export_with_xlsxwriter(
    db: ReviewDatabase,
    settings: AppSettings,
    filename: str,
) -> Path:
    """Build a portable server-side workbook when artifact-tool is unavailable."""
    try:
        import xlsxwriter  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "服务器未安装 XlsxWriter；请安装 docreview[server]，或配置 @oai/artifact-tool"
        ) from exc

    output_path = settings.output_dir / filename
    output_path.parent.mkdir(parents=True, exist_ok=True)
    matches = db.list_matches(limit=100_000)
    unsupported = db.list_unsupported()
    counts = db.counts()

    workbook = xlsxwriter.Workbook(
        str(output_path),
        {"strings_to_urls": False, "nan_inf_to_errors": True},
    )
    workbook.set_properties(
        {
            "title": "文档关键词审核结果",
            "subject": "服务器端文档分析归档",
            "author": "DocReview",
        }
    )
    title = workbook.add_format(
        {
            "bold": True,
            "font_size": 18,
            "font_color": "#17324D",
            "bottom": 2,
            "bottom_color": "#0F766E",
        }
    )
    section = workbook.add_format(
        {
            "bold": True,
            "font_color": "#FFFFFF",
            "bg_color": "#17324D",
            "border": 1,
            "border_color": "#D8E0E8",
        }
    )
    header = workbook.add_format(
        {
            "bold": True,
            "font_color": "#FFFFFF",
            "bg_color": "#0F766E",
            "border": 1,
            "border_color": "#D8E0E8",
            "text_wrap": True,
            "valign": "vcenter",
        }
    )
    cell = workbook.add_format(
        {"border": 1, "border_color": "#E5E7EB", "valign": "top"}
    )
    wrapped = workbook.add_format(
        {
            "border": 1,
            "border_color": "#E5E7EB",
            "valign": "top",
            "text_wrap": True,
        }
    )
    muted = workbook.add_format({"font_color": "#637180", "italic": True})
    pending = workbook.add_format(
        {"bg_color": "#EAF1F8", "font_color": "#315B7D", "border": 1}
    )
    bad = workbook.add_format(
        {"bg_color": "#FEE2E2", "font_color": "#991B1B", "border": 1}
    )
    good = workbook.add_format(
        {"bg_color": "#DCFCE7", "font_color": "#166534", "border": 1}
    )

    summary = workbook.add_worksheet("审核汇总")
    summary.hide_gridlines(2)
    summary.set_column("A:A", 22)
    summary.set_column("B:B", 18)
    summary.set_column("D:D", 22)
    summary.set_column("E:E", 18)
    summary.merge_range("A1:E2", "文档关键词审核结果", title)
    summary.write("A4", "指标", section)
    summary.write("B4", "数量", section)
    metrics = [
        ("文件总数", counts.get("documents", 0)),
        ("关键词命中", counts.get("matches", 0)),
        ("待审核", counts.get("待审核", 0)),
        ("有问题", counts.get("有问题", 0)),
        ("正常", counts.get("正常", 0)),
        ("待确认", counts.get("待确认", 0)),
        ("误识别", counts.get("误识别", 0)),
        ("不支持文件", counts.get("unsupported", 0)),
        ("处理错误", counts.get("errors", 0)),
    ]
    for row, (label, value) in enumerate(metrics, start=4):
        summary.write_string(row, 0, label, cell)
        summary.write_number(row, 1, int(value), cell)
    summary.write("D4", "工作表说明", section)
    summary.write("E4", "用途", section)
    summary.write_string(4, 3, "审核明细", cell)
    summary.write_string(4, 4, "逐条筛选、审核和回溯来源", wrapped)
    summary.write_string(5, 3, "不支持文件", cell)
    summary.write_string(5, 4, "检查未处理文件及原因", wrapped)
    summary.write_string(15, 0, "说明：Excel 是当前审核状态的归档快照。", muted)

    detail = workbook.add_worksheet("审核明细")
    detail.hide_gridlines(2)
    detail.freeze_panes(1, 0)
    detail_headers = [
        "序号",
        "审核状态",
        "关键词",
        "命中文字",
        "文件名",
        "来源路径",
        "页码",
        "内容类型",
        "段落/图片文字",
        "位置坐标",
        "识别置信度",
        "审核备注",
        "证据截图",
    ]
    for column, label in enumerate(detail_headers):
        detail.write_string(0, column, label, header)
    widths = [8, 12, 16, 18, 28, 44, 8, 20, 64, 28, 14, 30, 20]
    for column, width in enumerate(widths):
        detail.set_column(column, column, width)
    status_formats = {"有问题": bad, "正常": good, "待审核": pending}
    for row, item in enumerate(matches, start=1):
        detail.set_row(row, 78)
        detail.write_number(row, 0, row, cell)
        status = str(item.get("review_status", "待审核"))
        detail.write_string(row, 1, status, status_formats.get(status, cell))
        values = [
            item.get("keyword", ""),
            item.get("matched_text", ""),
            item.get("filename", ""),
            _display_source_path(settings, item.get("source_path", "")),
        ]
        for column, value in enumerate(values, start=2):
            _write_text(detail, row, column, value, wrapped)
        detail.write_number(row, 6, int(item.get("page_no", 0)), cell)
        _write_text(detail, row, 7, item.get("kind", ""), wrapped)
        _write_text(detail, row, 8, item.get("text", ""), wrapped)
        coordinates = "({x0:.4f}, {y0:.4f}, {x1:.4f}, {y1:.4f})".format(
            x0=float(item.get("x0", 0)),
            y0=float(item.get("y0", 0)),
            x1=float(item.get("x1", 0)),
            y1=float(item.get("y1", 0)),
        )
        detail.write_string(row, 9, coordinates, cell)
        detail.write_number(row, 10, float(item.get("confidence", 0)), cell)
        _write_text(detail, row, 11, item.get("note", ""), wrapped)
        _insert_evidence(detail, row, 12, Path(str(item.get("crop_path", ""))))
    if matches:
        detail.autofilter(0, 0, len(matches), len(detail_headers) - 1)

    rejected = workbook.add_worksheet("不支持文件")
    rejected.hide_gridlines(2)
    rejected.freeze_panes(1, 0)
    rejected_headers = ["文件名", "扩展名", "来源路径", "原因"]
    for column, label in enumerate(rejected_headers):
        rejected.write_string(0, column, label, header)
    for column, width in enumerate([30, 12, 54, 56]):
        rejected.set_column(column, column, width)
    for row, item in enumerate(unsupported, start=1):
        _write_text(rejected, row, 0, item.get("filename", ""), cell)
        _write_text(rejected, row, 1, item.get("extension", ""), cell)
        _write_text(
            rejected,
            row,
            2,
            _display_source_path(settings, item.get("source_path", "")),
            wrapped,
        )
        _write_text(rejected, row, 3, item.get("message", ""), wrapped)
    if unsupported:
        rejected.autofilter(0, 0, len(unsupported), len(rejected_headers) - 1)

    workbook.close()
    return output_path


def _write_text(worksheet, row: int, column: int, value, cell_format) -> None:
    text = str(value or "")
    if text:
        worksheet.write_string(row, column, text, cell_format)
    else:
        worksheet.write_blank(row, column, None, cell_format)


def _display_source_path(settings: AppSettings, value) -> str:
    source = Path(str(value or ""))
    upload_root = settings.data_dir.parent / "uploads"
    try:
        return source.resolve().relative_to(upload_root.resolve()).as_posix()
    except ValueError:
        pass
    return str(value or "")


def _insert_evidence(worksheet, row: int, column: int, path: Path) -> None:
    if not path.is_file():
        worksheet.write_string(row, column, "证据图不存在")
        return
    try:
        with Image.open(path) as image:
            width, height = image.size
        scale = min(150 / max(1, width), 96 / max(1, height), 1.0)
        worksheet.insert_image(
            row,
            column,
            str(path),
            {
                "x_scale": scale,
                "y_scale": scale,
                "x_offset": 4,
                "y_offset": 4,
                "object_position": 1,
                "description": "关键词命中证据",
            },
        )
    except Exception as exc:
        worksheet.write_string(row, column, f"证据图写入失败: {exc}")
