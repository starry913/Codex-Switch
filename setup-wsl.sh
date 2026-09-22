#!/bin/sh
set -eu
cd "$(dirname "$0")"
python3 -m venv .venv-wsl
.venv-wsl/bin/python -m pip install -r requirements.txt
printf '%s\n' 'Ready. Use the Windows window to select this WSL environment, or run .venv-wsl/bin/python -m codex_switch doctor.'
