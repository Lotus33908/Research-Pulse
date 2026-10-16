# Runs the full pipeline from this folder. Double-click friendly; also used by the scheduled task.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)   # research-tracker/
Set-Location $root

$python = Join-Path (Split-Path -Parent $root) ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    Write-Host "Creating virtual environment..."
    python -m venv (Join-Path (Split-Path -Parent $root) ".venv")
    & $python -m pip install --quiet --upgrade pip
    & $python -m pip install --quiet -r requirements.txt
}

& $python -X utf8 -m aksob_pubs run @args
if ($LASTEXITCODE -ne 0) { throw "Pipeline failed with exit code $LASTEXITCODE" }
Write-Host "Done. Report: $root\output\AKSOB_Publications.xlsx  Dashboard: $root\output\dashboard.html"
