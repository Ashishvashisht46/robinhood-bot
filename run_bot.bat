@echo off
REM Supervised runner for the copy-trader.
REM
REM Solves: the bot exiting (crash, RPC death, unhandled error) and leaving open
REM positions with no stop-loss running. Restarting is safe -- main.py reconciles
REM every saved position against its real on-chain balance before resuming.
REM
REM It does NOT survive the console window being closed. Windows kills child
REM processes when their console goes. For that, use Task Scheduler -- see
REM SUPERVISOR.md.
REM
REM Usage:  run_bot.bat
REM Stop:   Ctrl+C twice (once for python, once to break this loop)

setlocal enabledelayedexpansion
cd /d "%~dp0"

set RESTARTS=0
set WINDOW_START=%TIME%

:loop
echo.
echo [supervisor] starting main.py at %DATE% %TIME%  (restart #!RESTARTS!)
echo.

".venv\Scripts\python.exe" main.py
set EXITCODE=!ERRORLEVEL!

echo.
echo [supervisor] main.py exited with code !EXITCODE! at %TIME%

REM Exit code 0 means a clean shutdown -- the operator pressed Ctrl+C. Respect it
REM rather than fighting them for control of the terminal.
if "!EXITCODE!"=="0" (
    echo [supervisor] clean exit, not restarting.
    goto end
)

set /a RESTARTS+=1

REM A bot that cannot stay up is usually misconfigured, not unlucky. Restarting it
REM forever hides that and burns gas on repeated startup work.
if !RESTARTS! GEQ 10 (
    echo.
    echo [supervisor] STOPPING: 10 restarts. Something is wrong, not transient.
    echo [supervisor] Check logs\bot.log. If positions are open they are NOT
    echo [supervisor] being monitored - run:  .venv\Scripts\python.exe status.py
    goto end
)

echo [supervisor] restarting in 15s... (Ctrl+C to stop)
timeout /t 15 /nobreak >nul
goto loop

:end
echo.
echo [supervisor] finished. Checking for unmanaged positions:
".venv\Scripts\python.exe" status.py
endlocal
