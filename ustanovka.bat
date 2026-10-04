@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Устанавливаю PDF-чек-мейкер. Это нужно сделать один раз.
echo.

if exist venv\Scripts\python.exe goto libs
echo [1/2] Создаю отдельную папку для программы (venv)...
py -3 -m venv venv 2>nul
if not exist venv\Scripts\python.exe python -m venv venv
if not exist venv\Scripts\python.exe (
    echo.
    echo Не нашла Python. Скачайте его с https://www.python.org/downloads/
    echo При установке поставьте галочку "Add python.exe to PATH", потом запустите этот файл снова.
    pause
    exit /b 1
)

:libs
echo [2/2] Скачиваю библиотеки, это займёт минуту...
venv\Scripts\python.exe -m pip install --disable-pip-version-check -q -r requirements.txt
if errorlevel 1 (
    echo.
    echo Не получилось скачать библиотеки. Проверьте интернет и запустите этот файл снова.
    pause
    exit /b 1
)

echo.
if not exist "%ProgramFiles%\GTK3-Runtime Win64\bin" (
    echo Осталось одно: установить GTK3 Runtime, без него PDF не соберётся.
    echo Скачайте файл gtk3-runtime-...-win64.exe отсюда и везде нажимайте Next:
    echo https://github.com/tschoonj/GTK-for-Windows-Runtime-Environment-Installer/releases
    echo.
)
echo Готово. Запускайте программу двойным щелчком по zapusk.bat
pause
