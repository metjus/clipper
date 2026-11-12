@echo off
REM AI clipper - one-click start for Windows.
REM Finds Python (py launcher, PATH or the default install folders), creates a
REM virtual environment on first run, installs dependencies, checks ffmpeg and
REM starts the web UI.
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
chcp 65001 >nul

REM ---------------------------------------------------------------------------
REM 1. Existing virtual environment wins (no need to find Python again)
REM ---------------------------------------------------------------------------
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -c "import sys" >nul 2>nul && goto :have_venv
  echo Existujuce .venv je poskodene, vytvaram nove...
  rmdir /s /q ".venv"
)

REM ---------------------------------------------------------------------------
REM 2. Find a working Python 3.10 - 3.13
REM    Note: "python" in PATH is often the Microsoft Store stub that only opens
REM    the Store, so every candidate is actually executed and version-checked.
REM ---------------------------------------------------------------------------
set "PY="
for %%V in (3.12 3.13 3.11 3.10) do (
  if not defined PY (
    py -%%V -c "import sys" >nul 2>nul && set "PY=py -%%V"
  )
)
if not defined PY (
  for %%C in (python python3) do (
    if not defined PY (
      %%C -c "import sys; sys.exit(0 if (3,10)<=sys.version_info[:2]<=(3,13) else 1)" >nul 2>nul && set "PY=%%C"
    )
  )
)
if not defined PY (
  for %%D in ("%LOCALAPPDATA%\Programs\Python" "%ProgramFiles%" "%ProgramFiles(x86)%" "C:\") do (
    for %%V in (312 313 311 310) do (
      if not defined PY if exist "%%~D\Python%%V\python.exe" set "PY="%%~D\Python%%V\python.exe""
    )
  )
)
if not defined PY (
  for %%V in (3.14 3) do (
    if not defined PY (
      py -%%V -c "import sys" >nul 2>nul && set "PY=py -%%V"
    )
  )
  if defined PY (
    echo.
    echo Nasiel som len Python, ktory je novsi ako 3.13. Niektore kniznice ^(Whisper^) na nom zatial nemusia ist.
    echo Ak instalacia zlyha, nainstaluj Python 3.12: winget install -e --id Python.Python.3.12
    echo.
  )
)
if not defined PY goto :no_python

echo Pouzivam Python: !PY!
!PY! --version

echo [1/3] Vytvaram virtualne prostredie...
!PY! -m venv .venv
if errorlevel 1 (
  echo Nepodarilo sa vytvorit .venv. Skus preinstalovat Python a zaskrtnut "pip" a "py launcher".
  pause
  exit /b 1
)

:have_venv
set "VPY=.venv\Scripts\python.exe"

echo [2/3] Instalujem / aktualizujem zavislosti (prvykrat to chvilu trva)...
"%VPY%" -m pip install --upgrade pip >nul
"%VPY%" -m pip install -e . -U yt-dlp
if errorlevel 1 (
  echo.
  echo Instalacia zavislosti zlyhala. Posli mi text chyby vyssie.
  pause
  exit /b 1
)

where ffmpeg >nul 2>nul
if errorlevel 1 (
  if exist "%LOCALAPPDATA%\Microsoft\WinGet\Links\ffmpeg.exe" (
    set "PATH=%LOCALAPPDATA%\Microsoft\WinGet\Links;%PATH%"
  ) else (
    echo.
    echo ffmpeg nie je nainstalovany. Instalujem cez winget...
    winget install -e --id Gyan.FFmpeg --accept-package-agreements --accept-source-agreements
    set "PATH=%LOCALAPPDATA%\Microsoft\WinGet\Links;%PATH%"
  )
)
where ffmpeg >nul 2>nul
if errorlevel 1 (
  echo.
  echo ffmpeg sa stale neda najst. Zatvor toto okno a spusti run.bat znova,
  echo alebo ho nainstaluj rucne: winget install -e --id Gyan.FFmpeg
  pause
  exit /b 1
)

echo [3/3] Spustam AI clipper na http://127.0.0.1:8787
"%VPY%" -m clipper web
pause
exit /b 0

:no_python
echo.
echo ============================================================
echo  Nenasiel som funkcny Python 3.10 - 3.13.
echo ============================================================
echo  Najjednoduchsie riesenie (v PowerShelli alebo cmd):
echo.
echo     winget install -e --id Python.Python.3.12
echo.
echo  Potom zatvor toto okno a spusti run.bat znova.
echo.
echo  Ak instalujes z python.org, zaskrtni "Add python.exe to PATH"
echo  a nechaj zapnuty "py launcher".
echo.
echo  Ak Windows pri prikaze "python" otvara Microsoft Store:
echo  Nastavenia ^> Aplikacie ^> Rozsirene nastavenia aplikacii ^>
echo  Aliasy spustenia aplikacii ^> vypni python.exe a python3.exe
echo ============================================================
pause
exit /b 1
