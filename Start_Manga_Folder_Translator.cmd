@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set "MANGA_PY=%~dp0tools\BallonsTranslator\.venv\Scripts\pythonw.exe"
if not exist "%MANGA_PY%" set "MANGA_PY=%~dp0tools\BallonsTranslator\ballontrans_pylibs_win\pythonw.exe"
if not exist "%MANGA_PY%" (
  echo Python environment is missing. Follow docs\SETUP.zh-TW.md first.
  pause
  exit /b 1
)
start "" "%MANGA_PY%" -B -X utf8 "%~dp0tools\local-manga-translation\folder_translator.py" %*
endlocal
exit
