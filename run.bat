@echo off
cd /d "%~dp0"
py app.py 2>nul || python app.py
