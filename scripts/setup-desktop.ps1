$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

Write-Host "NahaLabs Desktop Agent setup" -ForegroundColor Cyan

if (-not (Get-Command py -ErrorAction SilentlyContinue)) {
    throw "Python launcher 'py' is required. Install Python 3.12+ first."
}

if (-not (Test-Path ".venv")) {
    py -3.12 -m venv .venv
}

.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m pip install -r desktop\requirements.txt
.\.venv\Scripts\python.exe -m piper.download_voices en_US-lessac-medium --data-dir .naha-models\piper

if (Get-Command winget -ErrorAction SilentlyContinue) {
    if (-not (Get-Command ollama -ErrorAction SilentlyContinue)) {
        winget install --id Ollama.Ollama --exact --silent --accept-package-agreements --accept-source-agreements
    }
}

if (-not (Get-Command ollama -ErrorAction SilentlyContinue)) {
    throw "Ollama is required. Install Ollama, then run this setup again."
}

ollama pull llama3.2:3b
ollama pull qwen2.5vl:3b

Write-Host ""
Write-Host "Naha desktop setup complete." -ForegroundColor Green
Write-Host "Run .\scripts\start-desktop.ps1"
