@echo off
cd /d "%~dp0"
where pyw >nul 2>nul && (start "" pyw "%~dp0app.py" %* & exit /b)
where pythonw >nul 2>nul && (start "" pythonw "%~dp0app.py" %* & exit /b)
echo Python wasn't found. Install it from python.org, then try again.
pause
