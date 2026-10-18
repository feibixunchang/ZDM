@echo off
REM ZDM installer launcher for Windows - double click this file.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
pause
