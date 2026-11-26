@echo off
setlocal EnableExtensions
cd /d "%~dp0"
set "DAILY_SIGNAL_ROOT=%~dp0"

echo Stopping Daily Signal (port 8083)...

powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='SilentlyContinue'; $root=[IO.Path]::GetFullPath($env:DAILY_SIGNAL_ROOT).TrimEnd('\'); $expected=[IO.Path]::GetFullPath((Join-Path $root '.venv\Scripts\python.exe')); $files=@((Join-Path $root 'logs\server.pid'),(Join-Path $root 'runtime.pid')); $ids=@(); foreach($f in $files){if(Test-Path $f){$v=0;if([int]::TryParse((Get-Content -Raw $f).Trim(),[ref]$v)-and $v -gt 0){$ids+= $v}}}; $all=Get-CimInstance Win32_Process; $targets=@(); foreach($id in ($ids|Select-Object -Unique)){$p=$all|Where-Object ProcessId -eq $id|Select-Object -First 1;if($p){$exe='';try{$exe=[IO.Path]::GetFullPath($p.ExecutablePath)}catch{};$cmd=[string]$p.CommandLine;if($exe -ieq $expected -and $cmd -match '(?i)-m\s+uvicorn\s+api:app'){$targets+= $p}}}; $listeners=Get-NetTCPConnection -LocalPort 8083 -State Listen; foreach($l in $listeners){$p=$all|Where-Object ProcessId -eq $l.OwningProcess|Select-Object -First 1;if($p){$exe='';try{$exe=[IO.Path]::GetFullPath($p.ExecutablePath)}catch{};$cmd=[string]$p.CommandLine;if($exe -ieq $expected -and $cmd -match '(?i)-m\s+uvicorn\s+api:app' -and $cmd -match [regex]::Escape($root)){$targets+= $p}}}; $targets=$targets|Sort-Object ProcessId -Unique; if(-not $targets){if(-not $listeners){Write-Output 'Daily Signal is already stopped.'}else{Write-Output 'Port 8083 is in use, but the process does not match this project. It was left untouched.'}}else{foreach($p in $targets){Write-Output ('Stopping project server PID '+$p.ProcessId); & taskkill.exe /PID $p.ProcessId /T /F | Out-Host}; Start-Sleep -Seconds 2}; foreach($f in $files){if(Test-Path $f){Remove-Item $f -Force}}; $left=Get-NetTCPConnection -LocalPort 8083 -State Listen; if($left){Write-Output 'WARNING: Port 8083 is still listening. Check Task Manager or logs before restarting.'; exit 2}else{Write-Output 'Daily Signal stopped. Port 8083 is free.'; exit 0}"
set "STOP_RESULT=%ERRORLEVEL%"

if not "%STOP_RESULT%"=="0" (
  echo.
  echo WARNING: The service may still be running, or port 8083 belongs to another app.
  echo Check Task Manager and logs before starting again.
  pause
  exit /b %STOP_RESULT%
)

echo Done.
endlocal
exit /b 0
