@echo off
REM AI clipper - one-click start for Windows.
REM Creates a virtual environment on first run, installs dependencies, checks ffmpeg, starts the web UI.
setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo Python nie je nainstalovany. Stiahni ho z https://www.python.org/downloads/ a pri instalacii zaskrtni "Add python.exe to PATH".
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo [1/3] Vytvaram virtualne prostredie...
  python -m venv .venv || (echo Nepodarilo sa vytvorit .venv & pause & exit /b 1)
)

echo [2/3] Instalujem / aktualizujem zavislosti (prvykrat to chvilu trva)...
".venv\Scripts\python.exe" -m pip install --upgrade pip >nul
".venv\Scripts\python.exe" -m pip install -e . || (echo Instalacia zlyhala & pause & exit /b 1)

where ffmpeg >nul 2>nul
if errorlevel 1 (
  echo.
  echo ffmpeg nie je v PATH. Skusam nainstalovat cez winget...
  winget install -e --id Gyan.FFmpeg --accept-package-agreements --accept-source-agreements
  echo.
  echo Ak sa ffmpeg prave nainstaloval, ZATVOR toto okno a spusti run.bat znova (PATH sa obnovi).
  echo.
)

echo [3/3] Spustam AI clipper na http://127.0.0.1:8787
".venv\Scripts\python.exe" -m clipper web
pause
