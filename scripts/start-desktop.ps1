$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    throw "Desktop environment not found. Run .\scripts\setup-desktop.ps1 first."
}

.\.venv\Scripts\python.exe desktop\naha_desktop.py
