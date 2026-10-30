@echo off
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0pull_ibkr.ps1" %*
pause
