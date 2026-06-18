@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" goto missing_env

".venv\Scripts\python.exe" -m sanzi_photo_tool.main 2>"startup-error.log"
if errorlevel 1 goto startup_failed

if exist "startup-error.log" del /q "startup-error.log"
exit /b 0

:missing_env
echo Project environment is missing.
echo Run: uv sync --extra dev
pause
exit /b 1

:startup_failed
echo Application failed to start.
echo See startup-error.log for details.
type "startup-error.log"
pause
exit /b 1
