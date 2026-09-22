@echo off
cd /d "%~dp0"
if exist ".venv\Scripts\pythonw.exe" (
    start "" ".venv\Scripts\pythonw.exe" -m codex_switch gui
) else (
    python -m codex_switch gui
    if errorlevel 1 pause
)
