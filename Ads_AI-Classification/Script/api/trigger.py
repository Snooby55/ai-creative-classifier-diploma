import os
import threading
from typing import Optional, Union

import pandas as pd

from Script.api.chatgpt_api_utils import extract_table_from_response, emulate_not_found_response, main_caller
from Script.core.console_bus import emit_log
from Script.core.csv_writer import IncrementalCSVWriter
from Script.utils.utils import extract_video_frames, image_to_base64


class ChatGPTCaller:
    def __init__(
        self,
        df_header,
        writer: Optional[IncrementalCSVWriter] = None,
        progress: Optional[dict] = None,
        progress_key: Optional[str] = None,
    ):
        self.header = list(df_header)
        self.df = pd.DataFrame(columns=self.header)
        self.writer = writer
        self.progress = progress
        self.progress_key = progress_key

        # Якщо все ж будете використовувати ОДИН trigger з кількох потоків.
        self._lock = threading.Lock()

    def get_dataframe(self) -> pd.DataFrame:
        return self.df

    def add_data_to_dataframe(self, new_data: Union[list, pd.DataFrame]) -> int:
        if isinstance(new_data, pd.DataFrame):
            new_df = new_data.reindex(columns=self.header)
        else:
            new_df = pd.DataFrame(new_data, columns=self.header)

        if new_df is None or new_df.empty:
            return 0

        with self._lock:
            self.df = pd.concat([self.df, new_df], ignore_index=True)
            if self.writer is not None:
                self.writer.append_rows(new_df)

            if self.progress is not None and self.progress_key is not None:
                key = f"{self.progress_key}_done"
                self.progress[key] = int(self.progress.get(key, 0)) + len(new_df)

        return len(new_df)

    def classify_group(
        self,
        file_group: list[str],
        admin_prompt: str,
        *,
        number_of_frames: int = 0,
        request_spacing_sec: float = 0.0,
        max_format_retries: int = 2,
    ) -> Optional[list]:
        """Класифікує групу файлів і ПОВЕРТАЄ таблицю як список рядків.

        Нічого не пише в self.df / csv — це важливо для паралельної обробки:
        worker-threads роблять лише API виклик і парсинг, а merge+write робимо
        в головному потоці.
        """
        if not file_group:
            return None

        format_guard = (
            "\n\nIMPORTANT: Return ONLY a markdown table with the required columns. "
            "Do not add explanations, code blocks, or extra text."
        )

        last_formatted: Optional[list] = None

        for attempt in range(max_format_retries + 1):
            prompt = admin_prompt + (format_guard if attempt > 0 else "")

            if number_of_frames <= 0:
                emit_log(f"Виклик ChatGPT API для групи зображень (images={len(file_group)}, try={attempt + 1})...")

                try:
                    descriptions, names = main_caller(
                        file_group,
                        prompt,
                        process_type="image",
                        progress=self.progress,
                        request_spacing_sec=request_spacing_sec,
                    )
                except Exception as exc:  # noqa: BLE001
                    emit_log(f"Помилка API (image batch): {exc}", level="WARNING")
                    return None

                expected_count = len(names)
                formatted = extract_table_from_response(
                    self.header,
                    descriptions,
                    [os.path.basename(n) for n in names],
                    expected_count=expected_count,
                )
            else:
                batch_data = []
                for vid_path in file_group:
                    frames = extract_video_frames(vid_path, number_of_frames=number_of_frames)
                    if not frames:
                        emit_log(f"Не вдалося отримати фрейми з {vid_path}. Пропускаємо.")
                        continue

                    frames = [f for f in frames if f is not None]
                    if not frames:
                        emit_log(f"У {vid_path} всі фрейми None. Пропускаємо.")
                        continue

                    batch_data.append(
                        {
                            "video_filename": vid_path,
                            "frame_base64": [image_to_base64(frame) for frame in frames],
                        }
                    )

                if not batch_data:
                    return None

                emit_log(
                    f"Виклик ChatGPT API для групи відео (videos={len(batch_data)}/{len(file_group)}, "
                    f"frames={number_of_frames * len(batch_data)}, try={attempt + 1})..."
                )

                try:
                    descriptions, names = main_caller(
                        batch_data,
                        prompt,
                        process_type="video",
                        progress=self.progress,
                        request_spacing_sec=request_spacing_sec,
                    )
                except Exception as exc:  # noqa: BLE001
                    emit_log(f"Помилка API (video batch): {exc}", level="WARNING")
                    return None

                expected_count = len(names)
                formatted = extract_table_from_response(
                    self.header,
                    descriptions,
                    [os.path.basename(n) for n in names],
                    expected_count=expected_count,
                )

            if formatted:
                return formatted

        # --- FALLBACK ---
        sent_names= [os.path.basename(p) for p in file_group]
        emit_log(
            f"Fallback: не вдалося отримати валідну таблицю після {max_format_retries + 1} спроб. "
            f"Записую 'Not found' для {len(sent_names)} рядків.",
            level="WARNING",
        )
        return emulate_not_found_response(self.header, sent_names, missing_value="Not found")

    def classification_caller(self, file_group, admin_prompt, number_of_frames=0) -> bool:
        formatted_data = self.classify_group(
            file_group,
            admin_prompt,
            number_of_frames=number_of_frames,
            request_spacing_sec=0.0,
        )

        if formatted_data:
            added = self.add_data_to_dataframe(formatted_data)
            emit_log(f"Результати записані: +{added}")
            return True

        emit_log("Таблиця не знайдена або помилка формату в відповіді.")
        return False
