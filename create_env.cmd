@echo off
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\create_env.ps1" %*
if errorlevel 1 pause
