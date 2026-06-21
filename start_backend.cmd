@echo off
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start_backend.ps1" %*
if errorlevel 1 pause
