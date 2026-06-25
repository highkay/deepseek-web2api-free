@echo off
REM build_webui.bat — install deps + build the React webui
setlocal enabledelayedexpansion
cd /d "%~dp0\.."

if not exist node_modules (
  if exist pnpm-lock.yaml (
    echo ==^> pnpm install
    call pnpm install --frozen-lockfile=false
  ) else (
    echo ==^> npm install
    call npm install --no-audit --no-fund
  )
)

echo ==^> npm run build
call npm run build
if errorlevel 1 exit /b 1

echo.
echo Build complete: webui-new\dist\
dir /b dist\
