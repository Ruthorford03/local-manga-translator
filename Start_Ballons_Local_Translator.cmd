@echo off
setlocal
cd /d "%~dp0"
set "MANGA_PY=%~dp0tools\BallonsTranslator\.venv\Scripts\python.exe"
if not exist "%MANGA_PY%" set "MANGA_PY=%~dp0tools\BallonsTranslator\ballontrans_pylibs_win\python.exe"
if not exist "%MANGA_PY%" (
  echo Python environment is missing. Follow docs\SETUP.zh-TW.md first.
  pause
  exit /b 1
)
"%MANGA_PY%" -X utf8 "%~dp0tools\local-manga-translation\launch_ballons_sakura.py" %*
if errorlevel 1 pause
endlocal
