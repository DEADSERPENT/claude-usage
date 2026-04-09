@echo off
setlocal

REM Install a Windows Task Scheduler job for cu daemon.
REM Run from an elevated prompt if your policy requires it.

set TASK_NAME=ClaudeUsageDaemon
REM Verify cu command exists.
where cu >nul 2>nul
if %ERRORLEVEL% neq 0 (
  echo [ERROR] 'cu' command not found.
  echo Install first: pip install .
  exit /b 1
)

schtasks /Create ^
  /TN "%TASK_NAME%" ^
  /SC ONLOGON ^
  /RL LIMITED ^
  /TR "cmd /c cu daemon start" ^
  /F

if %ERRORLEVEL% neq 0 (
  echo [ERROR] Failed to create scheduled task.
  exit /b 1
)

echo [OK] Installed task: %TASK_NAME%
echo You can run now with:
echo   schtasks /Run /TN "%TASK_NAME%"
endlocal
