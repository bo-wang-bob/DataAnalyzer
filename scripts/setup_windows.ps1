param(
    [ValidateSet("cpu", "gpu")]
    [string]$Device = "cpu",
    [string]$ArtifactNodeModules = ""
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Executable,
        [Parameter(ValueFromRemainingArguments = $true)]
        [string[]]$Arguments
    )

    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed with exit code ${LASTEXITCODE}: $Executable $($Arguments -join ' ')"
    }
}

$PyLauncher = Get-Command py -ErrorAction SilentlyContinue
if ($PyLauncher) {
    & $PyLauncher.Source -3.11 -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 11) else 1)"
    if ($LASTEXITCODE -eq 0) {
        $LauncherVersion = "-3.11"
    } else {
        & $PyLauncher.Source -3.12 -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else 1)"
        if ($LASTEXITCODE -eq 0) {
            $LauncherVersion = "-3.12"
        } else {
            throw "Python 3.11 or 3.12 was not found through the Windows py launcher."
        }
    }
    Invoke-Checked -Executable $PyLauncher.Source -Arguments @($LauncherVersion, "-m", "venv", ".venv")
} else {
    $PythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if (-not $PythonCommand) {
        throw "Python was not found. Install 64-bit Python 3.11 or 3.12 first."
    }
    Invoke-Checked -Executable $PythonCommand.Source -Arguments @("-c", "import sys; raise SystemExit(0 if sys.version_info[:2] in ((3, 11), (3, 12)) and sys.maxsize > 2**32 else 1)")
    Invoke-Checked -Executable $PythonCommand.Source -Arguments @("-m", "venv", ".venv")
}
$Python = Join-Path $Root ".venv\Scripts\python.exe"
Invoke-Checked -Executable $Python -Arguments @("-c", "import sys; raise SystemExit(0 if sys.maxsize > 2**32 else 1)")
Invoke-Checked -Executable $Python -Arguments @("-m", "pip", "install", "--upgrade", "pip", "setuptools", "wheel")

if ($Device -eq "gpu") {
    Write-Host "Installing PaddlePaddle for CUDA 11.8..."
    Invoke-Checked -Executable $Python -Arguments @("-m", "pip", "install", "paddlepaddle-gpu==3.2.0", "-i", "https://www.paddlepaddle.org.cn/packages/stable/cu118/")
    $PaddleDevice = "gpu:0"
} else {
    Write-Host "Installing PaddlePaddle CPU runtime..."
    Invoke-Checked -Executable $Python -Arguments @("-m", "pip", "install", "paddlepaddle==3.2.0", "-i", "https://www.paddlepaddle.org.cn/packages/stable/cpu/")
    $PaddleDevice = "cpu"
}

Invoke-Checked -Executable $Python -Arguments @("-m", "pip", "install", "-e", ".")
Invoke-Checked -Executable $Python -Arguments @("-m", "pip", "install", "paddleocr>=3.0,<4")

$SofficeCandidates = @(
    "$env:ProgramFiles\LibreOffice\program\soffice.exe",
    "${env:ProgramFiles(x86)}\LibreOffice\program\soffice.exe"
)
$Soffice = $SofficeCandidates | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
$NodeCommand = Get-Command node -ErrorAction SilentlyContinue
$NodePath = if ($NodeCommand) { $NodeCommand.Source } else { $null }

if (-not $ArtifactNodeModules -and $env:DOCREVIEW_NODE_MODULES) {
    $ArtifactNodeModules = $env:DOCREVIEW_NODE_MODULES
}

$Settings = [ordered]@{
    source_dir = "datas"
    data_dir = ".data"
    output_dir = "output"
    ocr_backend = "paddleocr"
    render_dpi = 180
    native_text_min_chars = 20
    embedded_image_min_area = 0.02
    max_pages = $null
    paddle_device = $PaddleDevice
    paddle_lang = "ch"
    libreoffice_path = $Soffice
    pdftoppm_path = $null
    node_path = $NodePath
    artifact_node_modules = if ($ArtifactNodeModules) { $ArtifactNodeModules } else { $null }
}
$Settings | ConvertTo-Json | Set-Content -Encoding UTF8 "settings.windows.json"

Write-Host ""
Write-Host "Installation complete. Running diagnostics..."
& $Python -m docreview --config settings.windows.json doctor
Write-Host ""
Write-Host "Run: powershell -ExecutionPolicy Bypass -File scripts\run_windows.ps1"
if (-not $Soffice) {
    Write-Warning "LibreOffice was not found. Install it or set libreoffice_path in settings.windows.json."
}
if (-not $ArtifactNodeModules) {
    Write-Warning "@oai/artifact-tool is not configured. The app works, but Excel export is disabled."
}
