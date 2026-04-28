@echo off
REM Mavigo daily trigger — chạy bởi Windows Task Scheduler 8h sáng VN.
REM Gọi gh CLI dispatch workflow daily.yml trên repo vinh261994-art/ban-tin-mavigo.
REM Log mọi lần chạy vào %~dp0..\data\local_cron.log để biết miss ngày nào.

set LOGFILE=%~dp0..\data\local_cron.log
echo [%date% %time%] === Mavigo daily trigger === >> "%LOGFILE%"

"C:\Program Files\GitHub CLI\gh.exe" workflow run daily.yml -R vinh261994-art/ban-tin-mavigo >> "%LOGFILE%" 2>&1
if errorlevel 1 (
  echo [%date% %time%] FAIL exit=%errorlevel% >> "%LOGFILE%"
  exit /b %errorlevel%
)
echo [%date% %time%] OK dispatched >> "%LOGFILE%"
exit /b 0
