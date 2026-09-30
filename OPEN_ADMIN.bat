@echo off
cd /d "%~dp0"
set PY=python
python --version >nul 2>nul
if errorlevel 1 set PY=py -3
if exist ADMIN_ACCESS.txt (
  echo [Maltsev] Opening the password file. Copy ONLY the password line.
  start "" "ADMIN_ACCESS.txt"
) else (
  echo [Maltsev] Run START_WINDOWS.bat first: no access file yet.
)
set ADMIN_URL=
%PY% -c "import json;print(json.load(open('data/server_address.json',encoding='utf-8'))['url']+'/admin.html')" > "%TEMP%\maltsev_admin_url.txt" 2>nul
if exist "%TEMP%\maltsev_admin_url.txt" set /p ADMIN_URL=<"%TEMP%\maltsev_admin_url.txt"
del "%TEMP%\maltsev_admin_url.txt" >nul 2>nul
if defined ADMIN_URL (
  start "" "%ADMIN_URL%"
) else (
  start "" "http://localhost:8080/admin.html"
)
