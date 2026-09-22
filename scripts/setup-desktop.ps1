$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

if (-not (Get-Command py -ErrorAction SilentlyContinue)) {
    throw "Python launcher py is required. Install Python 3.12+ first."
}

if (-not (Test-Path ".venv")) {
    py -3.12 -m venv .venv
}

.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m pip install -r desktop\requirements.txt

Write-Host ""
Write-Host "Naha desktop setup complete." -ForegroundColor Green
Write-Host "Run .\scripts\start-desktop.ps1"
