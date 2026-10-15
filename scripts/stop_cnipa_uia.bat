@echo off
chcp 65001 >nul
echo [stop] killing run_cnipa_*_uia.py ...
wmic process where "CommandLine like '%%run_cnipa_%%uia.py%%'" get ProcessId 2>nul | findstr /R "[0-9]" > "%TEMP%\cnipa_uia_pids.txt"
for /f "usebackq tokens=*" %%p in ("%TEMP%\cnipa_uia_pids.txt") do (
  echo taskkill PID %%p
  taskkill /PID %%p /T /F >nul 2>&1
)
del "%TEMP%\cnipa_uia_pids.txt" >nul 2>&1
if not "%~1"=="" (
  echo [stop] cancel task_id=%~1
  curl -s -X POST "http://127.0.0.1:8572/cnipa/v1/tasks/%~1/cancel"
  echo.
)
echo [stop] done. mouse should be free.
