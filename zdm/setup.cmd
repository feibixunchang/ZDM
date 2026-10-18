@echo off
REM Launched by the ZDM self-extracting installer (7zS.sfx) after extraction.
REM Runs the per-user Windows installer. No administrator rights required.
powershell -NoProfile -ExecutionPolicy Bypass -File install.ps1
