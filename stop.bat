@echo off
setlocal
cd /d "%~dp0"
if not exist runtime.pid (
  echo Daily Signal PID file not found.
  exit /b 0
)
for /f %%P in (runtime.pid) do powershell -NoProfile -Command "$p=Get-Process -Id %%P -ErrorAction SilentlyContinue; if ($p) { Stop-Process -Id %%P -Force; Write-Output 'Daily Signal stopped.' } else { Write-Output 'Process was not running.' }"
del runtime.pid
endlocal
