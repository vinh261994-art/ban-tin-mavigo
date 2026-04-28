@echo off
REM Mavigo weekly trigger — Windows Task Scheduler 9h sáng thứ Hai VN.
set LOGFILE=%~dp0..\data\local_cron.log
echo [%date% %time%] === Mavigo weekly trigger === >> "%LOGFILE%"
"C:\Program Files\GitHub CLI\gh.exe" workflow run weekly.yml -R vinh261994-art/ban-tin-mavigo >> "%LOGFILE%" 2>&1
if errorlevel 1 (
  echo [%date% %time%] FAIL exit=%errorlevel% >> "%LOGFILE%"
  exit /b %errorlevel%
)
echo [%date% %time%] OK dispatched >> "%LOGFILE%"
exit /b 0
