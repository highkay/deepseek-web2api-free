@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

if "%PORT%"=="" set PORT=8080
if "%HOST%"=="" set HOST=127.0.0.1

echo ========================================
echo   DeepSeek Chat API Proxy Server
echo   HOST=%HOST%  PORT=%PORT%
echo ========================================
echo.

:: ---------------------------------------------------------------------------
:: Kill any process occupying the configured port, but only if we can verify
:: it actually IS our process (or is unknown). The previous version of this
:: script blindly `taskkill /F`'d whatever held the port — which is unsafe
:: because port 8080 is also used by IDEs, Tomcat, web servers, etc.
:: ---------------------------------------------------------------------------
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":%PORT% " ^| findstr LISTENING') do (
    set "PID=%%a"
    set "IS_OURS=0"

    :: Try to read the command line of the process holding the port.
    for /f "tokens=*" %%c in ('wmic process where "ProcessId=%%a" get CommandLine /format:list 2^>nul ^| findstr /c:"CommandLine="') do (
        set "CMDLINE=%%c"
        :: Strip the "CommandLine=" prefix.
        set "CMDLINE=!CMDLINE:~13!"
        echo   PID %%a command: !CMDLINE!
        :: Match if it looks like our server (uvicorn, server.py, etc.).
        echo !CMDLINE! | findstr /i "uvicorn server.py ds2api" >nul && set "IS_OURS=1"
    )

    if "!IS_OURS!"=="1" (
        echo Killing OUR process %%a holding port %PORT% ...
        taskkill /F /PID %%a >nul 2>&1
        if !errorlevel! neq 0 (
            taskkill /F /PID %%a /T >nul 2>&1
        )
    ) else (
        echo.
        echo   ============================================================
        echo   Port %PORT% is held by PID %%a, but it is NOT a ds2api process.
        echo   Refusing to kill it. Either:
        echo     * free the port yourself (e.g. stop the other service), or
        echo     * run this script with a different PORT:
        echo         set PORT=9090 ^&^& start.bat
        echo   ============================================================
        echo.
        pause
        exit /b 1
    )
)
timeout /t 1 /nobreak >nul

echo Starting server on %HOST%:%PORT% ...
python -m uvicorn server:app --host %HOST% --port %PORT%
if errorlevel 1 (
    echo.
    echo Failed to start. Reason: port %PORT% occupied or missing deps.
    pause
)
