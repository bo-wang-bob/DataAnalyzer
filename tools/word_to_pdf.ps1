param(
    [Parameter(Mandatory = $true)]
    [string]$InputPath,
    [Parameter(Mandatory = $true)]
    [string]$OutputPath
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
$Word = $null
$Document = $null

try {
    $Word = New-Object -ComObject Word.Application
    $Word.Visible = $false
    $Word.DisplayAlerts = 0
    try {
        # msoAutomationSecurityForceDisable. Documents are opened read-only.
        $Word.AutomationSecurity = 3
    } catch {
        # Older Word versions may not expose AutomationSecurity through COM.
    }

    $Document = $Word.Documents.Open($InputPath, $false, $true, $false)
    # 17 = wdExportFormatPDF.
    $Document.ExportAsFixedFormat($OutputPath, 17)
} finally {
    if ($null -ne $Document) {
        try {
            $Document.Close(0)
        } catch {
        }
        try {
            [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($Document)
        } catch {
        }
    }
    if ($null -ne $Word) {
        try {
            $Word.Quit(0)
        } catch {
        }
        try {
            [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($Word)
        } catch {
        }
    }
    [GC]::Collect()
    [GC]::WaitForPendingFinalizers()
}

if (-not (Test-Path -LiteralPath $OutputPath)) {
    throw "Microsoft Word did not create the PDF output."
}
