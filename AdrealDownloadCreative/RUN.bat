@ECHO OFF

SET ROOT=%~dp0\..
SET PYTHONPATH=%ROOT%
CD /D %~dp0\AdrealDownloadCreative

REM Перевірка наявності лаунчера `py`
where py >nul 2>nul
IF %ERRORLEVEL% EQU 0 (
    REM Використовуємо `py` для встановлення пакетів
    py -3.12 -m pip install -r requirements.txt
    py -3.12 -m pip install -U yt-dlp
    CLS
    py -3.12 -m run_adreal_with_history
) ELSE (
    REM Якщо `py` не знайдений, використовуємо `python3.12`
    python3.12 -m pip install -r requirements.txt
    python3.12 -m pip install -U yt-dlp
    CLS
    python3.12 -m run_adreal_with_history
)

PAUSE