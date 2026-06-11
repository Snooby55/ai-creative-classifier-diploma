from pathlib import Path
PROJECT_DIR = Path(__file__).resolve().parent.parent  # Project/Script
def full_path(*parts) -> str:
    p = Path(*parts)
    if not p.is_absolute():
        p = PROJECT_DIR.joinpath(p)
    return str(p.expanduser().resolve())

# Publicis
api_key = 'PASTE_YOUR_API_KEY_HERE'


# У різних моделей виклик може відрізнятися (тож при зміні тестування обов'язкове)
CHATGPT_MODEL = 'gpt-5' # Використовувана модель
TOKEN_PRICES = {
    'gpt-4o': {
        "input": 2.5 / 1_000_000,
        "cached_input": 1.25 / 1_000_000,
        "output": 10 / 1_000_000
    },

    # Завишена кількість токенів на обробці зображень, тому ціна стає +- еквівалентною до 'gpt-5'
    'gpt-5-mini': {
        "input": 0.25 / 1_000_000,
        "cached_input": 0.03 / 1_000_000,
        "output": 2 / 1_000_000
    },

    'gpt-5': {
        "input": 1.25 / 1_000_000,
        "cached_input": 0.13 / 1_000_000,
        "output": 10 / 1_000_000
    }
}


# --- Структура для генерації адмін промптів (не для юзерів) ---
ADMIN_PROMPT_SCHEMA = {
    "image": {
        "task": (
            "You’ll receive an image (with a built-in caption) and must classify it strictly into the table below. "
            "The entire output MUST be written in English only, regardless of the input language."
        ),
        "rules": [
            "Use caption and visual content to extract all fields.",
            "If something is not visible or unknown, write 'Not found'.",
            "Output ONLY the table described below."
        ]
    },
    "video": {
        "task": (
            "You'll receive frames from several videos (each frame labeled by its source video). "
            "The entire output MUST be written in English only, regardless of the input language."
            "For each video, use all its frames to determine a single classification."
        ),
        "rules": [
            "If ANY frame has information, use it to classify the WHOLE video.",
            "Do NOT classify frames individually - one row per video only.",
            "If something is not visible or unknown across frames, write 'Not found'.",
            "Output ONLY the table described below."
        ]
    }
}


APP_CONFIG = {
    "paths": {
        "debug_output_excel_results": full_path("_forced_stopped_last_chance.xlsx"), # Дебаг. Результат класифікації (Для випадків при швидкій зупинці зберігатиме)
        "partial_image_csv": full_path("_temp/partial_image_results.csv"), # Тимчасові проміжні результати для захисту від екстрених зупинок (темп директорія та файли, створиться та видалиться автоматично)
        "partial_video_csv": full_path("_temp/partial_video_results.csv"), # Тимчасові проміжні результати для захисту від екстрених зупинок (темп директорія та файли, створиться та видалиться автоматично)
        "classification_excel": full_path("options.xlsx"), # На випадок присутності списку класифікації, стовпець налаштовується у виклику в main()
        "admin_prompt_schema":  full_path("Script/admin_prompt_schema.json"), # Схема для генерації промпту, тех файл
        "history_store_path": full_path("history.csv"), # Основний файл історії (накопичувальний)
        "reports_dir": full_path("Reports"), # Тека для всіх звітів

        # https://github.com/Data-Science-Publicis-Groupe-Ukraine/AdrealDownloadCreative + "run_adreal_with_history.py"
        "adreal_project_dir":     full_path("../AdrealDownloadCreative"), # Шлях до вивантажувача креативів для авто вибору
        "extra_known_links_path": full_path("../AdrealDownloadCreative/h.txt"), # Де лежать посилання які співпадають з тими що в історії
        "runtime_upload": full_path("../AdrealDownloadCreative/runtime_upload.json"), # Де лежать додаткові метрики по вигрузці креативів
        "creatives_directory": None, # Де лежать файли креативів
        "links_output_path": full_path("../AdrealDownloadCreative/output.txt"), # Згенерований output
    },
    "flags": {
        "process_images": True,             # Класифікувати зображення?
        "process_videos": True,             # Класифікувати відео?
        "save_debug_excel_always": False,   # Зберігатиме "debug_output_excel_results" завжди, а не тільки під час різких зупинок
        "dry_run": False,                   # Режим для ТЕСТУВАННЯ стрес-стійкості системи на великі навантаження
    },
    "params": {
        "similarity_threshold": 0.98,       # 0<x<1 -> Наскільки зображення повинні бути схожі, щоб вважатися дублікатами
        "frame_ratio": 0.5,                 # 0<x<1 з якої частини гіфки братиметься кадр для конвертації у зображення
        "image_batch_size": 10,             # Кількість зображень на один запит (бажано НЕ більше 10)
        "number_of_frames": 2,              # Кількість фреймів на відео для його класифікації
        "video_batch_size": 5,              # Кількість відео в запиті (бажано number_of_frames * video_batch_size = 10 чи менше)
        "delay": 1,                         # Мінімальна затримка між запитами в пайплайнах
        "chatgpt_max_tokens": 500,          # Обмеження кількості токенів у відповіді чата гпт (зазвичай відповідь матиме ~200 токенів)
        "stress_img_items": 9000,           # скільки зображень симулювати (при dry_run == True)
        "stress_vid_items": 2000,           # скільки відео симулювати (при dry_run == True)
    },
}

