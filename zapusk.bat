@echo off
cd /d "%~dp0"
if not exist venv\Scripts\pythonw.exe (
    chcp 65001 >nul
    echo Программа ещё не установлена. Сначала дважды щёлкните по ustanovka.bat
    pause
    exit /b 1
)
start "" venv\Scripts\pythonw.exe okno.py
