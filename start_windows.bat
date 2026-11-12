@echo off
rem Start CCU-Historian repair tool on Windows
rem Installs the Python packages (JPype1, numpy) on first run.
setlocal
cd /d "%~dp0"

where py >nul 2>nul
if errorlevel 1 (
  echo Python wurde nicht gefunden. Bitte Python 3 von https://www.python.org installieren
  echo ^(Haken bei "Add python.exe to PATH" und "py launcher" setzen^).
  pause
  exit /b 1
)

rem Java (11 or newer) is located by the tool itself, also without PATH/JAVA_HOME.

py -3 -c "import jpype, numpy" 2>nul
if errorlevel 1 (
  echo Installiere benoetigte Python-Pakete ...
  py -3 -m pip install --user -r requirements.txt
  if errorlevel 1 (
    echo Installation fehlgeschlagen.
    pause
    exit /b 1
  )
)

py -3 -m historian_repair %*
if errorlevel 1 pause
