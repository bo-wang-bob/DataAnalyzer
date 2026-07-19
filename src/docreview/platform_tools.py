from __future__ import annotations

import os
import platform
import shutil
import subprocess
from pathlib import Path


def find_libreoffice(configured: Path | None = None) -> Path | None:
    explicit = _first_existing_file(
        configured,
        _env_path("DOCREVIEW_SOFFICE"),
    )
    if explicit:
        return explicit
    for name in ("soffice", "soffice.exe", "libreoffice", "libreoffice.exe"):
        found = shutil.which(name)
        if found:
            return Path(found)
    if platform.system() == "Windows":
        roots = _windows_program_roots()
        candidates = [root / "LibreOffice" / "program" / "soffice.exe" for root in roots]
        return _first_existing_file(*candidates)
    if platform.system() == "Darwin":
        return _first_existing_file(
            Path("/Applications/LibreOffice.app/Contents/MacOS/soffice")
        )
    return None


def find_pdftoppm(configured: Path | None = None) -> Path | None:
    explicit = _first_existing_file(
        configured,
        _env_path("DOCREVIEW_PDFTOPPM"),
    )
    if explicit:
        return explicit
    found = shutil.which("pdftoppm.exe" if platform.system() == "Windows" else "pdftoppm")
    return Path(found) if found else None


def find_powershell() -> Path | None:
    if platform.system() != "Windows":
        return None
    for name in ("pwsh.exe", "powershell.exe"):
        found = shutil.which(name)
        if found:
            return Path(found)
    system_root = os.environ.get("SystemRoot")
    if system_root:
        candidate = (
            Path(system_root)
            / "System32"
            / "WindowsPowerShell"
            / "v1.0"
            / "powershell.exe"
        )
        if candidate.is_file():
            return candidate.resolve()
    return None


def microsoft_word_available() -> bool:
    if platform.system() != "Windows":
        return False
    try:
        import winreg  # type: ignore[attr-defined]

        with winreg.OpenKey(
            winreg.HKEY_CLASSES_ROOT,
            r"Word.Application\CLSID",
            0,
            winreg.KEY_READ,
        ):
            return True
    except (ImportError, OSError):
        return False


def find_artifact_node_modules(
    root_dir: Path, configured: Path | None = None
) -> Path | None:
    candidates = [
        configured,
        _env_path("DOCREVIEW_NODE_MODULES"),
        root_dir / "node_modules",
    ]
    for candidate in candidates:
        if not candidate:
            continue
        resolved = candidate.expanduser().resolve()
        if (resolved / "@oai" / "artifact-tool").exists():
            return resolved
    return None


def find_node(
    root_dir: Path,
    configured: Path | None = None,
    artifact_node_modules: Path | None = None,
) -> Path | None:
    explicit = _first_existing_file(configured, _env_path("DOCREVIEW_NODE"))
    if explicit:
        return explicit
    modules = artifact_node_modules or find_artifact_node_modules(root_dir)
    if modules:
        executable = "node.exe" if platform.system() == "Windows" else "node"
        candidate = modules.resolve().parent / "bin" / executable
        if candidate.is_file():
            return candidate
    for name in ("node.exe", "node") if platform.system() == "Windows" else ("node",):
        found = shutil.which(name)
        if found:
            return Path(found)
    return None


def subprocess_no_window_flag() -> int:
    if platform.system() == "Windows":
        return int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return 0


def _windows_program_roots() -> list[Path]:
    roots: list[Path] = []
    for name in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        value = os.environ.get(name)
        if value:
            path = Path(value)
            if path not in roots:
                roots.append(path)
    return roots


def _env_path(name: str) -> Path | None:
    value = os.environ.get(name, "").strip().strip('"')
    return Path(value) if value else None


def _first_existing_file(*candidates: Path | None) -> Path | None:
    for candidate in candidates:
        if candidate and candidate.expanduser().is_file():
            return candidate.expanduser().resolve()
    return None
