# Auto-installer per Windows (PowerShell).
# Crea il virtualenv, installa le dipendenze, prepara .env e configura il database.
#   powershell -ExecutionPolicy Bypass -File install.ps1
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$python = if ($env:PYTHON) { $env:PYTHON } else { "python" }

if (-not (Test-Path ".venv")) {
    Write-Host "[1/4] Creazione virtualenv (.venv)..."
    & $python -m venv .venv
} else {
    Write-Host "[1/4] Virtualenv .venv gia presente."
}
$venvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"

Write-Host "[2/4] Installazione dipendenze..."
& $venvPython -m pip install --upgrade pip
& $venvPython -m pip install -r requirements.txt

if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host "[3/4] Creato .env da .env.example - inserisci la tua OPENAI_API_KEY."
} else {
    Write-Host "[3/4] File .env gia presente."
}

Write-Host "[4/4] Configurazione database PostgreSQL..."
& $venvPython scripts\setup_db.py

Write-Host ""
Write-Host "Installazione completata."
Write-Host "Avvio: .venv\Scripts\python.exe -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000"
