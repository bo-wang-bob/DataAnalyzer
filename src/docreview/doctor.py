from __future__ import annotations

import importlib.util
import platform
import shutil
import sys
from dataclasses import dataclass

from .models import AppSettings
from .platform_tools import (
    find_artifact_node_modules,
    find_libreoffice,
    find_node,
    find_pdftoppm,
    find_powershell,
    microsoft_word_available,
)


@dataclass(frozen=True)
class DiagnosticCheck:
    name: str
    ok: bool
    detail: str
    required: bool = True


def run_diagnostics(settings: AppSettings) -> list[DiagnosticCheck]:
    system = platform.system()
    checks = [
        DiagnosticCheck(
            "Python",
            sys.version_info >= (3, 11),
            f"{platform.python_version()} ({system})",
        ),
        _module_check("Pillow", "PIL"),
        _module_check("pdfplumber", "pdfplumber"),
    ]

    checks.extend(_word_converter_checks(settings, system))

    backend = settings.ocr_backend.strip().lower()
    if backend in {"auto", "apple", "apple-vision"} and system == "Darwin":
        clang = shutil.which("clang")
        checks.append(
            DiagnosticCheck(
                "Apple Vision OCR",
                bool(clang),
                clang or "未找到 clang/Xcode 命令行工具",
            )
        )
    else:
        checks.extend(
            [
                _module_check("PaddlePaddle", "paddle"),
                _module_check("PaddleOCR", "paddleocr"),
                DiagnosticCheck(
                    "OCR device",
                    bool(settings.paddle_device.strip()),
                    settings.paddle_device,
                ),
            ]
        )

    pdfium_available = importlib.util.find_spec("pypdfium2") is not None
    pdftoppm = find_pdftoppm(settings.pdftoppm_path)
    checks.append(
        DiagnosticCheck(
            "PDF renderer",
            pdfium_available or pdftoppm is not None,
            "pypdfium2" if pdfium_available else (
                str(pdftoppm) if pdftoppm else "pypdfium2 与 pdftoppm 均不可用"
            ),
        )
    )
    checks.append(
        DiagnosticCheck(
            "Poppler fallback",
            pdftoppm is not None,
            str(pdftoppm) if pdftoppm else "未安装；PDFium 正常时不影响运行",
            required=False,
        )
    )

    modules = find_artifact_node_modules(
        settings.root_dir, settings.artifact_node_modules
    )
    node = find_node(settings.root_dir, settings.node_path, modules)
    checks.extend(
        [
            DiagnosticCheck(
                "Node.js",
                node is not None,
                str(node) if node else "未找到；Excel 导出不可用",
                required=False,
            ),
            DiagnosticCheck(
                "Excel artifact runtime",
                modules is not None,
                str(modules) if modules else "未配置；Excel 导出不可用",
                required=False,
            ),
        ]
    )
    return checks


def required_checks_pass(checks: list[DiagnosticCheck]) -> bool:
    return all(check.ok for check in checks if check.required)


def _module_check(name: str, module: str) -> DiagnosticCheck:
    available = importlib.util.find_spec(module) is not None
    return DiagnosticCheck(name, available, "已安装" if available else "未安装")


def _word_converter_checks(
    settings: AppSettings, system: str
) -> list[DiagnosticCheck]:
    backend = settings.word_backend.strip().lower().replace("_", "-")
    backend = {"word": "microsoft-word", "msword": "microsoft-word"}.get(
        backend, backend
    )
    libreoffice = find_libreoffice(settings.libreoffice_path)
    word_registered = microsoft_word_available() if system == "Windows" else False
    powershell = find_powershell() if system == "Windows" else None
    word_ready = word_registered and powershell is not None

    if backend == "auto":
        converter_ready = word_ready or libreoffice is not None
        selected = (
            "Microsoft Word COM"
            if word_ready
            else ("LibreOffice" if libreoffice else "均不可用")
        )
    elif backend == "microsoft-word":
        converter_ready = word_ready
        selected = "Microsoft Word COM"
    elif backend == "libreoffice":
        converter_ready = libreoffice is not None
        selected = "LibreOffice"
    else:
        converter_ready = False
        selected = f"未知后端: {settings.word_backend}"

    checks: list[DiagnosticCheck] = []
    if system == "Windows":
        word_detail = (
            f"已注册；PowerShell: {powershell}"
            if word_ready
            else (
                "已注册，但未找到 PowerShell"
                if word_registered
                else "未检测到 Microsoft Word 桌面版"
            )
        )
        checks.append(
            DiagnosticCheck(
                "Microsoft Word COM",
                word_ready,
                word_detail,
                required=False,
            )
        )
    checks.extend(
        [
            DiagnosticCheck(
                "LibreOffice fallback",
                libreoffice is not None,
                str(libreoffice) if libreoffice else "未找到；作为可选备用不影响 Word COM",
                required=False,
            ),
            DiagnosticCheck(
                "Word converter",
                converter_ready,
                f"配置: {backend}；当前选择: {selected}",
            ),
        ]
    )
    return checks
