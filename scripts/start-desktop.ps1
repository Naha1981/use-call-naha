$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    throw "Desktop environment not found. Run .\scripts\setup-desktop.ps1 first."
}

$env:NAHA_PIPER_DATA_DIR = Join-Path (Get-Location) ".naha-models\piper"
$env:NAHA_PIPER_MODEL = "en_US-lessac-medium"
.\.venv\Scripts\python.exe desktop\naha_desktop.py
