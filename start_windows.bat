@echo off
rem Start CCU-Historian repair tool (installs JPype1 on first run)
cd /d "%~dp0"
py -3 -c "import jpype" 2>nul || py -3 -m pip install -r requirements.txt
py -3 -m historian_repair %*
if errorlevel 1 pause
