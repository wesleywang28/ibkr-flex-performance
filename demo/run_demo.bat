@echo off
setlocal
set PYTHONIOENCODING=utf-8
set PY=python
where py >nul 2>nul
if %errorlevel%==0 set PY=py
%PY% "%~dp0generate_demo.py" || goto :err
%PY% "%~dp0..\analysis\build_dashboard.py" "%~dp0." --demo --no-snapshot || goto :err
for %%f in ("%~dp0*.html") do start "" "%%f"
goto :eof
:err
echo.
echo Failed. Please run: pip install -r requirements.txt
pause
