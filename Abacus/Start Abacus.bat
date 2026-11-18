@echo off
title Abacus
cd /d "%~dp0"
python abacus.py
echo.
echo Abacus stopped. Press any key to close.
pause >nul
