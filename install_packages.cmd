@echo off
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\install_packages.ps1" %*
if errorlevel 1 pause
