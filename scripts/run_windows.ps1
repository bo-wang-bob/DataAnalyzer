param(
    [string]$HostAddress = "127.0.0.1",
    [int]$Port = 8765
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    throw ".venv was not found. Run scripts\setup_windows.ps1 first."
}
$Config = Join-Path $Root "settings.windows.json"
if (-not (Test-Path $Config)) {
    throw "settings.windows.json was not found. Run scripts\setup_windows.ps1 first."
}

$env:PYTHONUTF8 = "1"
Set-Location $Root
& $Python -m docreview --config $Config serve --host $HostAddress --port $Port
exit $LASTEXITCODE
