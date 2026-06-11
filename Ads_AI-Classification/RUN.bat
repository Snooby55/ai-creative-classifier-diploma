@ECHO OFF

SET ROOT=%~dp0\..
SET PYTHONPATH=%ROOT%
CD /D %~dp0\Ads_AI-Classification

REM Перевірка наявності лаунчера `py`
where py >nul 2>nul
IF %ERRORLEVEL% EQU 0 (
    REM Використовуємо `py` для встановлення пакетів
    py -3.12 -m pip install -r Script\requirements.txt
    CLS
    py -3.12 -m Script.GUI
) ELSE (
    REM Якщо `py` не знайдений, використовуємо `python3.12`
    python3.12 -m pip install -r Script\requirements.txt
    CLS
    python3.12 -m Script.GUI
)

PAUSE