@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "PY="
where py >nul 2>nul && set "PY=py"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY (
  echo Python wurde nicht gefunden.
  echo Bitte von https://www.python.org/downloads/ installieren
  echo und beim Installieren "Add python.exe to PATH" anhaken.
  pause
  exit /b 1
)
echo Pruefe benoetigte Bibliotheken ...
%PY% -m pip install --quiet --disable-pip-version-check --user pikepdf pillow reportlab tkinterdnd2
rem optional: iPhone-Fotos (.heic) - Fehler hier sind egal
%PY% -m pip install --quiet --disable-pip-version-check --user pillow-heif >nul 2>nul
if "%~1"=="" (
  %PY% pdf_werkstatt.py
) else (
  %PY% pdf_werkstatt.py "%~1"
)
if errorlevel 1 pause
