$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

Write-Host "NahaLabs Desktop Agent setup" -ForegroundColor Cyan

function Get-PyLauncher {
    $cmd = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $windowsPy = Join-Path $env:WINDIR "py.exe"
    if (Test-Path $windowsPy) { return $windowsPy }
    return $null
}

$PyExe = Get-PyLauncher

if (-not $PyExe) {
    if (-not (Get-Command winget.exe -ErrorAction SilentlyContinue)) {
        throw "Python 3.12 is required. Install Python 3.12+ or winget, then run this setup again."
    }

    Write-Host "Installing Python 3.12..." -ForegroundColor Yellow
    winget install --id Python.Python.3.12 --exact --silent --accept-package-agreements --accept-source-agreements | Out-Host
    $PyExe = Get-PyLauncher
}

if (-not $PyExe) {
    throw "Python launcher was not found after installation."
}

$PythonSelector = "3.12"
& $PyExe -3.12 -c "import sys; print(sys.version)" 2>$null
if ($LASTEXITCODE -ne 0) {
    $PythonSelector = "3.13"
    & $PyExe -3.13 -c "import sys; print(sys.version)" 2>$null
    if ($LASTEXITCODE -ne 0) {
        throw "Python 3.12 or 3.13 is required."
    }
}

if (-not (Test-Path ".venv")) {
    & $PyExe "-$PythonSelector" -m venv .venv
}

$Python = (Join-Path (Get-Location) ".venvScriptspython.exe")
& $Python -m pip install --upgrade pip
& $Python -m pip install -r desktopequirements.txt
& $Python -m piper.download_voices en_US-lessac-medium --data-dir .naha-modelspiper

$OllamaExe = $null
$cmd = Get-Command ollama.exe -ErrorAction SilentlyContinue
if ($cmd) {
    $OllamaExe = $cmd.Source
}

if (-not $OllamaExe -and (Get-Command winget.exe -ErrorAction SilentlyContinue)) {
    Write-Host "Installing Ollama..." -ForegroundColor Yellow
    winget install --id Ollama.Ollama --exact --silent --accept-package-agreements --accept-source-agreements | Out-Host

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
    throw "Ollama is required. Install Ollama and run this setup again."
}

$env:PATH = (Split-Path $OllamaExe) + ";" + $env:PATH

try {
    & $OllamaExe list *> $null
    if ($LASTEXITCODE -ne 0) { throw "Ollama server unavailable" }
}
catch {
    Start-Process -FilePath $OllamaExe -ArgumentList "serve" -WindowStyle Hidden
    $ready = $false
    for ($i = 0; $i -lt 20; $i++) {
        Start-Sleep -Seconds 1
        & $OllamaExe list *> $null
        if ($LASTEXITCODE -eq 0) {
            $ready = $true
            break
        }
    }
    if (-not $ready) {
        throw "Ollama server did not become ready."
    }
}

& $OllamaExe pull llama3.2:3b
& $OllamaExe pull qwen2.5vl:3b

Write-Host ""
Write-Host "Naha desktop setup complete." -ForegroundColor Green
Write-Host "Run .scriptsstart-desktop.ps1"
