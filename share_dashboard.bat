@echo off
REM Put the live dashboard on a public HTTPS URL so someone else can watch it.
REM
REM Starts the dashboard in --share mode (password required, reachable off this
REM machine) and opens a Cloudflare quick tunnel in front of it. The page shows a
REM wallet address, balances and open positions, so it is never exposed without
REM the password.
REM
REM Your PC has to stay on and this window has to stay open. Close it (ctrl+c)
REM when your friend is done looking -- the URL dies with it.

setlocal
cd /d "%~dp0"

set CF="C:\Program Files (x86)\cloudflared\cloudflared.exe"
if not exist %CF% (
  echo cloudflared is not installed. Install it with:
  echo     winget install --id Cloudflare.cloudflared
  pause
  exit /b 1
)

REM Fresh password every session, so an old link cannot be reused.
for /f %%p in ('powershell -NoProfile -Command "-join ((48..57)+(97..122)+(65..90)|Get-Random -Count 12|%%{[char]$_})"') do set PW=%%p

echo Starting dashboard...
start "bot dashboard" /min .venv\Scripts\python.exe dashboard.py --share --password=%PW% --no-open
timeout /t 3 /nobreak >nul

echo.
echo ==================================================================
echo   Share the https://....trycloudflare.com link printed below.
echo   Send these credentials SEPARATELY (not in the same message):
echo.
echo       username:  watch
echo       password:  %PW%
echo.
echo   Press ctrl+c here to stop sharing. The link stops working.
echo ==================================================================
echo.

%CF% tunnel --url http://localhost:8787

echo.
echo Tunnel closed. Stopping the dashboard...
for /f "tokens=2" %%i in ('tasklist /fi "windowtitle eq bot dashboard*" /fo list ^| find "PID:"') do taskkill /pid %%i /f >nul 2>&1
taskkill /f /fi "windowtitle eq bot dashboard*" >nul 2>&1
echo Done. The link is dead and the dashboard is no longer reachable.
endlocal
