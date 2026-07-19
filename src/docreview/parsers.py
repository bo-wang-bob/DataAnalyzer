from __future__ import annotations

import math
import statistics
import subprocess
from pathlib import Path

import pdfplumber
from PIL import Image, ImageOps

from .models import AppSettings, BBox, ContentBlock, ParseResult, TextLine
from .ocr import OCREngine
from .platform_tools import find_libreoffice, find_pdftoppm, subprocess_no_window_flag


class ParseError(RuntimeError):
    pass


def parse_document(
    source_path: Path,
    file_type: str,
    document_id: str,
    settings: AppSettings,
    ocr: OCREngine,
) -> ParseResult:
    if file_type == "image":
        return parse_image(source_path, document_id, settings, ocr)
    if file_type == "pdf":
        return parse_pdf(source_path, document_id, settings, ocr)
    if file_type == "word":
        converted = convert_word_to_pdf(source_path, document_id, settings)
        result = parse_pdf(converted, document_id, settings, ocr)
        for block in result.blocks:
            prefix = "word_render"
            block.source_detail = f"{prefix};{block.source_detail}" if block.source_detail else prefix
        return result
    raise ParseError(f"不支持的文件类型: {file_type}")


def parse_image(
    source_path: Path,
    document_id: str,
    settings: AppSettings,
    ocr: OCREngine,
) -> ParseResult:
    page_dir = settings.data_dir / "pages" / document_id
    page_dir.mkdir(parents=True, exist_ok=True)
    page_image = page_dir / "page-0001.png"
    try:
        with Image.open(source_path) as original:
            image = ImageOps.exif_transpose(original).convert("RGB")
            image.save(page_image, format="PNG", optimize=True)
    except Exception as exc:
        raise ParseError(f"图片无法打开: {exc}") from exc

    lines = ocr.recognize(page_image)
    blocks = _lines_to_blocks(lines, 1, "ocr_image", page_image, "source_image")
    return ParseResult(blocks=blocks, page_count=1)


def parse_pdf(
    source_path: Path,
    document_id: str,
    settings: AppSettings,
    ocr: OCREngine,
) -> ParseResult:
    page_dir = settings.data_dir / "pages" / document_id
    region_dir = settings.data_dir / "regions" / document_id
    page_dir.mkdir(parents=True, exist_ok=True)
    region_dir.mkdir(parents=True, exist_ok=True)
    blocks: list[ContentBlock] = []
    warnings: list[str] = []

    try:
        pdf = pdfplumber.open(source_path)
    except Exception as exc:
        raise ParseError(f"PDF 无法打开: {exc}") from exc

    with pdf:
        total_pages = len(pdf.pages)
        page_count = min(total_pages, settings.max_pages) if settings.max_pages else total_pages
        for page_index in range(page_count):
            page_no = page_index + 1
            page = pdf.pages[page_index]
            page_image = _render_pdf_page(
                source_path,
                page_no,
                page_dir,
                settings.render_dpi,
                settings.pdftoppm_path,
            )
            native_lines = _extract_pdf_lines(page)
            native_chars = sum(len(line.text.strip()) for line in native_lines)
            has_native_text = native_chars >= settings.native_text_min_chars

            if has_native_text:
                blocks.extend(
                    _lines_to_blocks(
                        native_lines,
                        page_no,
                        "native_text",
                        page_image,
                        "pdf_text_layer",
                    )
                )
                image_boxes = _extract_image_boxes(page, settings.embedded_image_min_area)
                for image_index, image_box in enumerate(image_boxes, start=1):
                    # A full-page raster with a valid text layer is normally a
                    # scanned page that has already been OCRed. Re-running OCR
                    # would create duplicate evidence.
                    if image_box.area > 0.72:
                        continue
                    region_path = region_dir / f"page-{page_no:04d}-image-{image_index:03d}.png"
                    _crop_normalized(page_image, image_box, region_path)
                    try:
                        local_lines = ocr.recognize(region_path)
                    except Exception as exc:
                        warnings.append(
                            f"第 {page_no} 页内嵌图片 {image_index} OCR 失败: {exc}"
                        )
                        continue
                    local_blocks = _lines_to_blocks(
                        local_lines,
                        page_no,
                        "embedded_image_ocr",
                        page_image,
                        f"embedded_image:{image_index}",
                    )
                    for block in local_blocks:
                        block.bbox = block.bbox.map_inside(image_box)
                    blocks.extend(local_blocks)
            else:
                try:
                    ocr_lines = ocr.recognize(page_image)
                except Exception as exc:
                    raise ParseError(f"第 {page_no} 页 OCR 失败: {exc}") from exc
                blocks.extend(
                    _lines_to_blocks(
                        ocr_lines,
                        page_no,
                        "scanned_page_ocr",
                        page_image,
                        "full_page_scan",
                    )
                )

    return ParseResult(blocks=blocks, page_count=page_count, warnings=warnings)


def convert_word_to_pdf(
    source_path: Path, document_id: str, settings: AppSettings
) -> Path:
    soffice = find_libreoffice(settings.libreoffice_path)
    if not soffice:
        raise ParseError(
            "未找到 LibreOffice。请安装 LibreOffice，或在 settings.json 设置 libreoffice_path"
        )
    output_dir = settings.data_dir / "converted" / document_id
    output_dir.mkdir(parents=True, exist_ok=True)
    expected = output_dir / f"{source_path.stem}.pdf"
    if expected.exists():
        return expected
    profile = output_dir / "libreoffice-profile"
    profile.mkdir(parents=True, exist_ok=True)
    command = [
        str(soffice),
        "--headless",
        f"-env:UserInstallation={profile.resolve().as_uri()}",
        "--convert-to",
        "pdf",
        "--outdir",
        str(output_dir),
        str(source_path.resolve()),
    ]
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=180,
        creationflags=subprocess_no_window_flag(),
    )
    if result.returncode != 0 or not expected.exists():
        alternatives = list(output_dir.glob("*.pdf"))
        if alternatives:
            return alternatives[0]
        detail = result.stderr.strip() or result.stdout.strip() or "未生成 PDF"
        raise ParseError(f"Word 转 PDF 失败: {detail}")
    return expected


def _render_pdf_page(
    source_path: Path,
    page_no: int,
    page_dir: Path,
    dpi: int,
    pdftoppm_path: Path | None = None,
) -> Path:
    target = page_dir / f"page-{page_no:04d}.png"
    if target.exists():
        return target
    pdfium_error = ""
    try:
        _render_with_pdfium(source_path, page_no, target, dpi)
        return target
    except Exception as exc:
        pdfium_error = str(exc)
    pdftoppm = find_pdftoppm(pdftoppm_path)
    if not pdftoppm:
        detail = f"；PDFium 错误: {pdfium_error}" if pdfium_error else ""
        raise ParseError(
            "PDF 页面渲染不可用。请安装 pypdfium2，或安装 Poppler/设置 pdftoppm_path"
            + detail
        )
    prefix = target.with_suffix("")
    result = subprocess.run(
        [
            str(pdftoppm),
            "-f",
            str(page_no),
            "-l",
            str(page_no),
            "-png",
            "-singlefile",
            "-r",
            str(dpi),
            str(source_path),
            str(prefix),
        ],
        capture_output=True,
        text=True,
        timeout=180,
        creationflags=subprocess_no_window_flag(),
    )
    if result.returncode != 0 or not target.exists():
        raise ParseError(result.stderr.strip() or f"第 {page_no} 页渲染失败")
    return target


def _render_with_pdfium(
    source_path: Path, page_no: int, target: Path, dpi: int
) -> None:
    import pypdfium2 as pdfium  # type: ignore

    document = pdfium.PdfDocument(str(source_path))
    page = None
    bitmap = None
    try:
        page = document.get_page(page_no - 1)
        bitmap = page.render(scale=max(1.0, dpi / 72.0))
        image = bitmap.to_pil().convert("RGB")
        image.save(target, format="PNG", optimize=True)
    finally:
        if bitmap is not None and hasattr(bitmap, "close"):
            bitmap.close()
        if page is not None and hasattr(page, "close"):
            page.close()
        if hasattr(document, "close"):
            document.close()


def _extract_pdf_lines(page: pdfplumber.page.Page) -> list[TextLine]:
    try:
        words = page.extract_words(
            x_tolerance=2,
            y_tolerance=3,
            keep_blank_chars=False,
            use_text_flow=False,
        )
    except Exception:
        words = []
    normalized = [
        TextLine(
            str(item.get("text", "")).strip(),
            BBox(
                float(item["x0"]) / float(page.width),
                float(item["top"]) / float(page.height),
                float(item["x1"]) / float(page.width),
                float(item["bottom"]) / float(page.height),
            ).clamped(),
            1.0,
        )
        for item in words
        if str(item.get("text", "")).strip()
    ]
    ordered = sorted(normalized, key=lambda item: (item.bbox.y0, item.bbox.x0))
    rows: list[list[TextLine]] = []
    for word in ordered:
        if not rows:
            rows.append([word])
            continue
        current = rows[-1]
        baseline_close = abs(word.bbox.y0 - current[0].bbox.y0) <= 0.006
        column_break = word.bbox.x0 - current[-1].bbox.x1 > 0.12
        if baseline_close and not column_break:
            current.append(word)
        else:
            rows.append([word])

    lines: list[TextLine] = []
    for row in rows:
        row.sort(key=lambda item: item.bbox.x0)
        box = row[0].bbox
        for word in row[1:]:
            box = box.union(word.bbox)
        text = _join_word_tokens([word.text for word in row])
        lines.append(TextLine(text=text, bbox=box, confidence=1.0))
    return lines


def _join_word_tokens(tokens: list[str]) -> str:
    text = " ".join(token for token in tokens if token)
    for punctuation in ",.;:!?)]}，。；：！？》】”’":
        text = text.replace(f" {punctuation}", punctuation)
    for opening in "([{《【“‘":
        text = text.replace(f"{opening} ", opening)
    return text


def _extract_image_boxes(
    page: pdfplumber.page.Page, min_area: float
) -> list[BBox]:
    boxes: list[BBox] = []
    try:
        images = page.images
    except Exception:
        images = []
    for item in images:
        try:
            box = BBox(
                float(item["x0"]) / float(page.width),
                float(item["top"]) / float(page.height),
                float(item["x1"]) / float(page.width),
                float(item["bottom"]) / float(page.height),
            ).clamped()
        except (KeyError, TypeError, ValueError):
            continue
        if box.area < min_area or box.width < 0.05 or box.height < 0.03:
            continue
        if any(_nearly_same(box, existing) for existing in boxes):
            continue
        boxes.append(box)
    return boxes


def _nearly_same(left: BBox, right: BBox, tolerance: float = 0.01) -> bool:
    return all(
        abs(a - b) <= tolerance
        for a, b in zip(
            (left.x0, left.y0, left.x1, left.y1),
            (right.x0, right.y0, right.x1, right.y1),
        )
    )


def _crop_normalized(source: Path, box: BBox, target: Path) -> None:
    with Image.open(source) as original:
        image = original.convert("RGB")
    width, height = image.size
    crop = image.crop(
        (
            max(0, int(box.x0 * width)),
            max(0, int(box.y0 * height)),
            min(width, max(1, int(math.ceil(box.x1 * width)))),
            min(height, max(1, int(math.ceil(box.y1 * height)))),
        )
    )
    crop.save(target, format="PNG", optimize=True)


def _lines_to_blocks(
    lines: list[TextLine],
    page_no: int,
    kind: str,
    page_image: Path,
    source_detail: str,
) -> list[ContentBlock]:
    paragraphs = group_lines_into_paragraphs(lines)
    return [
        ContentBlock(
            page_no=page_no,
            kind=kind,
            text=text,
            bbox=box,
            confidence=confidence,
            page_image=page_image,
            source_detail=source_detail,
        )
        for text, box, confidence in paragraphs
        if text.strip()
    ]


def group_lines_into_paragraphs(
    lines: list[TextLine],
) -> list[tuple[str, BBox, float]]:
    if not lines:
        return []
    ordered = sorted(lines, key=lambda line: (round(line.bbox.y0, 3), line.bbox.x0))
    heights = [line.bbox.height for line in ordered if line.bbox.height > 0]
    median_height = statistics.median(heights) if heights else 0.02
    max_gap = max(0.012, median_height * 1.15)
    groups: list[list[TextLine]] = []

    for line in ordered:
        if not groups:
            groups.append([line])
            continue
        previous = groups[-1][-1]
        vertical_gap = line.bbox.y0 - previous.bbox.y1
        horizontal_overlap = _overlap_ratio(line.bbox, previous.bbox)
        aligned = abs(line.bbox.x0 - previous.bbox.x0) <= 0.12
        same_flow = vertical_gap <= max_gap and (horizontal_overlap >= 0.15 or aligned)
        if vertical_gap < -median_height * 0.5:
            same_flow = False
        if same_flow and len(groups[-1]) < 12:
            groups[-1].append(line)
        else:
            groups.append([line])

    result: list[tuple[str, BBox, float]] = []
    for group in groups:
        text = "\n".join(line.text.strip() for line in group if line.text.strip())
        box = group[0].bbox
        for line in group[1:]:
            box = box.union(line.bbox)
        confidence = sum(line.confidence for line in group) / len(group)
        result.append((text, box.clamped(), confidence))
    return result


def _overlap_ratio(left: BBox, right: BBox) -> float:
    overlap = max(0.0, min(left.x1, right.x1) - max(left.x0, right.x0))
    denominator = max(0.001, min(left.width, right.width))
    return overlap / denominator
