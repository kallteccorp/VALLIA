@echo off
setlocal
python -B "%~dp0start.py" %*
if errorlevel 1 pause
