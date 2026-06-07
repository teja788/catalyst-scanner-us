@echo off
REM ============================================================
REM  catalyst-scanner-us - launch the Streamlit dashboard (Windows)
REM  Opens a local web UI over the stored scan data.
REM ============================================================
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo [dashboard] .venv not found. Run setup.bat first.
    exit /b 1
)
".venv\Scripts\python.exe" -m streamlit run dashboard.py
endlocal
