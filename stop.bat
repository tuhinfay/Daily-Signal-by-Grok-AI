@echo off
setlocal EnableExtensions
cd /d "%~dp0"

echo Stopping Daily Signal...

REM 1) Kill by PID file if process still exists
if exist runtime.pid (
  set /p PID=<runtime.pid
  powershell -NoProfile -Command "if (Get-Process -Id %PID% -ErrorAction SilentlyContinue) { Stop-Process -Id %PID% -Force; Write-Output ('Stopped PID ' + %PID%) } else { Write-Output 'PID file process not running.' }"
  del runtime.pid >nul 2>&1
) else (
  echo No runtime.pid found.
)

REM 2) Fallback: kill anything listening on port 8083 (covers stale PID / child processes)
powershell -NoProfile -Command "$conns=Get-NetTCPConnection -LocalPort 8083 -State Listen -ErrorAction SilentlyContinue; if (-not $conns) { Write-Output 'Port 8083 is free.'; exit 0 }; $pids=$conns.OwningProcess | Sort-Object -Unique; foreach ($procId in $pids) { try { Stop-Process -Id $procId -Force -ErrorAction Stop; Write-Output ('Killed port 8083 PID ' + $procId) } catch { Write-Output ('Could not kill PID ' + $procId) } }"

echo Done.
endlocal
