$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

$Python = Join-Path (Get-Location) ".venvScriptspython.exe"
if (-not (Test-Path $Python)) {
    throw "Desktop environment not found. Run .scriptssetup-desktop.ps1 first."
}

$OllamaExe = $null
$cmd = Get-Command ollama.exe -ErrorAction SilentlyContinue
if ($cmd) {
    $OllamaExe = $cmd.Source
}

if (-not $OllamaExe) {
    $candidatePaths = @(
        (Join-Path $env:LOCALAPPDATA "ProgramsOllamaollama.exe"),
        "C:Program FilesOllamaollama.exe"
    )
    foreach ($candidate in $candidatePaths) {
        if (Test-Path $candidate) {
            $OllamaExe = $candidate
            break
        }
    }
}

if (-not $OllamaExe) {
    throw "Ollama is not installed. Run .scriptssetup-desktop.ps1 first."
}

$env:PATH = (Split-Path $OllamaExe) + ";" + $env:PATH
$env:PYTHONPATH = "$(Get-Location);$env:PYTHONPATH"
$env:NAHA_PIPER_DATA_DIR = Join-Path (Get-Location) ".naha-modelspiper"
$env:NAHA_PIPER_MODEL = "en_US-lessac-medium"
$env:NAHA_LLM_MODEL = "llama3.2:3b"
$env:NAHA_VISION_MODEL = "qwen2.5vl:3b"

& $OllamaExe list *> $null
if ($LASTEXITCODE -ne 0) {
    Start-Process -FilePath $OllamaExe -ArgumentList "serve" -WindowStyle Hidden
    Start-Sleep -Seconds 2
}

Write-Host "Starting Naha desktop agent..." -ForegroundColor Cyan
& $Python desktop
aha_desktop.py
