
# --- Дозволи на типи вигрузок ---
ALLOW_VIDEO_DIRECT = True        # прямі .mp4 з <video> або з iframe-відео
ALLOW_YOUTUBE = True             # YouTube через Selenium -> yt_dlp у процес-пулі
ALLOW_SCREENSHOT_FALLBACK = True # якщо нічого не зберегли - робити скрін банеру
ALLOW_HTML_DOWNLOAD = False

# --- Поведінка, коли HTML вимкнений і сторінка може дати лише .html ---
# "mark_failed"  -> записати в output.txt як ніби нічого не вивантажили (number/type порожні)
# "screenshot"   -> не зберігати HTML, але зробити скрін (як fallback)
HTML_DISABLED_BEHAVIOR = "mark_failed"

# Внутрішній спец-токен (main.py його перевіряє).
HTML_DISABLED_RESULT = "html_skipped"
