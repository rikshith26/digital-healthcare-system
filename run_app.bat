@echo off
title HealthLab AI - Auto-Restart Server
cd web_app
:loop
echo ========================================
echo Starting HealthLab AI Server...
echo ========================================
python app.py
echo.
echo ⚠️ Server stopped or crashed. Restarting in 5 seconds...
timeout /t 5
goto loop
