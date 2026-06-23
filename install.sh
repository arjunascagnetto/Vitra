#!/usr/bin/env bash
# Auto-installer per Linux/macOS.
# Crea il virtualenv, installa le dipendenze, prepara .env e configura il database.
#   bash install.sh
set -euo pipefail
cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3}"

if [ ! -d .venv ]; then
  echo "[1/4] Creazione virtualenv (.venv)..."
  "$PYTHON" -m venv .venv
else
  echo "[1/4] Virtualenv .venv gia presente."
fi
VENV_PY=".venv/bin/python"

echo "[2/4] Installazione dipendenze..."
"$VENV_PY" -m pip install --upgrade pip
"$VENV_PY" -m pip install -r requirements.txt

if [ ! -f .env ]; then
  cp .env.example .env
  echo "[3/4] Creato .env da .env.example - inserisci la tua OPENAI_API_KEY."
else
  echo "[3/4] File .env gia presente."
fi

echo "[4/4] Configurazione database PostgreSQL..."
"$VENV_PY" scripts/setup_db.py

echo ""
echo "Installazione completata."
echo "Avvio: .venv/bin/python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000"
