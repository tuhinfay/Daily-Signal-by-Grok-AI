@echo off
setlocal EnableExtensions
cd /d "%~dp0"
set "DAILY_SIGNAL_ROOT=%~dp0"

echo Stopping Daily Signal...

if not exist runtime.pid (
  echo No runtime.pid found. Nothing was stopped.
  echo For safety, stop.bat will not kill an unknown process using port 8083.
  goto :END
)

powershell -NoProfile -ExecutionPolicy Bypass -Command "$targetId=0; [void][int]::TryParse((Get-Content -Raw 'runtime.pid'),[ref]$targetId); if($targetId -le 0){Write-Output 'Invalid PID file. No process was stopped.'; exit 2}; $p=Get-CimInstance Win32_Process -Filter ('ProcessId = '+$targetId) -ErrorAction SilentlyContinue; if(-not $p){Write-Output 'Recorded process is not running.'; exit 0}; $expected=[IO.Path]::GetFullPath((Join-Path $env:DAILY_SIGNAL_ROOT '.venv\Scripts\python.exe')); $actual=''; try{$actual=[IO.Path]::GetFullPath($p.ExecutablePath)}catch{}; $cmd=[string]$p.CommandLine; if($actual -ieq $expected -and $cmd -match '(?i)-m\s+uvicorn\s+api:app'){Stop-Process -Id $targetId -Force -ErrorAction Stop; Write-Output ('Stopped Daily Signal PID '+$targetId); exit 0}; Write-Output ('PID '+$targetId+' does not match this project Uvicorn process. Nothing was stopped.'); exit 3"
set "STOP_RESULT=%ERRORLEVEL%"

if "%STOP_RESULT%"=="0" (
  del runtime.pid >nul 2>&1
) else (
  echo Safety check prevented stopping an unrelated process.
)

:END
echo Done.
endlocal
