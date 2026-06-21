@echo off
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\build_frontend.ps1" %*
if errorlevel 1 pause
