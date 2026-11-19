@echo off
cd /d "%~dp0"
python voice.py
if errorlevel 1 pause
