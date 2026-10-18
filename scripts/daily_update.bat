@echo off
setlocal
set "MODE=%~1"
if "%MODE%"=="" set "MODE=SCHEDULED"
title PortfolioTracker [%MODE%]

rem Ensure working directory is project root
cd /d "%~dp0\.."

rem Locate project virtualenv python or fall back to system python
if exist ".venv\Scripts\python.exe" (
    set "PYTHON_EXE=.venv\Scripts\python.exe"
) else (
    set "PYTHON_EXE=python.exe"
)

echo ========================================================
echo PortfolioTracker Daily Update [%MODE%]
echo Date/Time: %DATE% %TIME%
echo Interpreter: %PYTHON_EXE%
echo Working Dir: %CD%
echo ========================================================
echo.

"%PYTHON_EXE%" -m portfolio_tracker.jobs.daily_update --db "%CD%\data\portfolio.db" --trigger %MODE%
set "EXIT_CODE=%ERRORLEVEL%"

echo.
echo ========================================================
echo Update finished with exit code %EXIT_CODE% at %TIME%
echo ========================================================

exit /b %EXIT_CODE%
