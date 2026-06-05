@echo off
setlocal
cd /d "%~dp0"
npm install
if errorlevel 1 (
  echo.
  echo Failed to install browser reader dependency.
  pause
  exit /b 1
)
echo.
echo Browser reader dependency installed.
pause
