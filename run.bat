@echo off
REM ============================================================
REM  catalyst-scanner-us - convenience launcher (Windows)
REM  Forwards all args to the Typer CLI inside the venv.
REM  Examples:
REM     run.bat --help
REM     run.bat version
REM     run.bat scan
REM     run.bat ask "What did NVIDIA file today?"
REM ============================================================
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo [run] .venv not found. Run setup.bat first.
    exit /b 1
)
".venv\Scripts\python.exe" -m scanner.cli %*
endlocal
