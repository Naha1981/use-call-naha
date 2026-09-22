$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    throw "Desktop environment not found. Run .\scripts\setup-desktop.ps1 first."
}

$env:NAHA_PIPER_DATA_DIR = Join-Path (Get-Location) ".naha-models\piper"
$env:NAHA_PIPER_MODEL = "en_US-lessac-medium"
$env:NAHA_LLM_MODEL = "llama3.2:3b"
$env:NAHA_VISION_MODEL = "qwen2.5vl:3b"

if (-not (Get-Command ollama -ErrorAction SilentlyContinue)) {
    throw "Ollama is not installed."
}

.\.venv\Scripts\python.exe desktop\naha_desktop.py
