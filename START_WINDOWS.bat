@echo off
cd /d "%~dp0"
echo [Maltsev] Folder: %CD%
if not exist server.py (
  echo [Maltsev] ERROR: server.py not found next to this file.
  echo Unpack the ENTIRE zip archive into a folder first.
  echo Running from inside the zip does not work.
  pause
  exit /b 1
)
if not exist catalog.json (
  echo [Maltsev] ERROR: catalog.json not found. Unpack the whole archive.
  pause
  exit /b 1
)
set PY=
where python >nul 2>nul
if errorlevel 1 goto TryPy
python --version >nul 2>nul
if errorlevel 1 goto TryPy
set PY=python
goto HavePy
:TryPy
where py >nul 2>nul
if errorlevel 1 goto NoPy
py -3 --version >nul 2>nul
if errorlevel 1 goto NoPy
set PY=py -3
goto HavePy
:NoPy
echo.
echo [Maltsev] Python not found, or it is a Microsoft Store stub.
echo 1. Install Python 3.10+ from https://www.python.org/downloads/
echo 2. On the FIRST installer screen check "Add python.exe to PATH".
echo 3. If Python came from Microsoft Store - remove it in Settings / Apps
echo    and install from python.org instead.
pause
exit /b 1
:HavePy
echo [Maltsev] Found: %PY%
%PY% --version
echo [Maltsev] Starting the shop... Keep this window open.
echo.
%PY% server.py --open
echo.
if errorlevel 1 (
  echo [Maltsev] Server stopped with error, code %errorlevel%.
  echo If you see "Address already in use" above, the port is busy:
  echo close the old black server window and start again.
) else (
  echo [Maltsev] Server stopped. Orders are saved in the data folder.
)
pause
