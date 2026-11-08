@echo off
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
    py app.py --console %*
) else (
    python app.py --console %*
)
echo.
echo The app has stopped. If something went wrong, the message is above.
pause
