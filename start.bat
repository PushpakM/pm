@echo off
REM Starts the weighbridge software. Open http://127.0.0.1:8080 in Chrome or Edge.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo First run: creating the Python environment...
  py -3.12 -m venv .venv || python -m venv .venv
  ".venv\Scripts\python.exe" -m pip install --upgrade pip
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
)
if not exist "config.toml" (
  copy config.example.toml config.toml
  echo.
  echo config.toml created. Edit it, then run:  .venv\Scripts\python -m weighbridge init
  pause
  exit /b
)
".venv\Scripts\python.exe" -m weighbridge run
pause
