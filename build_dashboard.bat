@echo off
setlocal
set PYTHONIOENCODING=utf-8
where py >nul 2>nul
if %errorlevel%==0 (
  py "%~dp0analysis\build_dashboard.py" "%~dp0."
) else (
  python "%~dp0analysis\build_dashboard.py" "%~dp0."
)
pause
