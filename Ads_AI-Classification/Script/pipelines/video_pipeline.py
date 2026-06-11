
import os
import time
import pandas as pd
from concurrent.futures import ThreadPoolExecutor, as_completed
from tqdm import tqdm

from Script.core.console_bus import emit_log
from Script.core.csv_writer import IncrementalCSVWriter
from Script.utils.utils import recursive_call, extract_campaign_name, extract_video_frames, compute_phash_from_image, compare_video_hash,  merge_similar_pairs
from Script.api.trigger import ChatGPTCaller

class VideoBatchProcessor:
    def __init__(self, root_dir, callback, batch_group_size, number_of_frames=3, similarity_threshold=0.9, num_workers=8, classify_concurrency=2):
        self.root_dir = root_dir
        self.callback = callback  # Callback для обробки груп відео (кампанії)
        self.batch_group_size = batch_group_size
        self.number_of_frames = number_of_frames
        self.similarity_threshold = similarity_threshold
        self.num_workers = num_workers
        self.classify_concurrency = classify_concurrency

        self.video_files = []       # Шляхи до відео
        self.video_phashes = {}     # {video_path: phash (для репрезентативного фрейму)}
        self.campaigns = {}         # {campaign_name: [video_path, ...]}

        self.failed_files_count: int = 0

        self.df = pd.DataFrame()

    def traverse_directories(self, files_list: list[str] | None = None):
        if files_list is not None:
            all_files = [p for p in files_list if str(p).lower().endswith(('.mp4', '.avi', '.mov', '.mkv', '.webm', '.m4v', '.mpeg', '.mpg'))]
        else:
            all_files = []
            for dirpath, _, filenames in os.walk(self.root_dir):
                for file in filenames:
                    if file.lower().endswith(('.mp4', '.avi', '.mov', '.mkv', '.webm', '.m4v', '.mpeg', '.mpg')):
                        all_files.append(os.path.join(dirpath, file))

        failed_names: list[str] = []

        with ThreadPoolExecutor(max_workers=self.num_workers) as executor:
            futures = {executor.submit(self.process_video_file, path, self.number_of_frames): path for path in all_files}
            for future in tqdm(as_completed(futures), total=len(futures), desc="Обробка відео", disable=os.environ.get("TQDM_DISABLE","0")=="1"):
                path = futures[future]
                try:
                    path, phash, campaign = future.result()
                except Exception: # noqa
                    failed_names.append(os.path.basename(path))
                    continue

                try:
                    if campaign:
                        self.video_files.append(path)
                        self.video_phashes[path] = phash
                        self.campaigns.setdefault(campaign, []).append(path)
                except Exception: # noqa
                    failed_names.append(os.path.basename(path))
                    continue

        if failed_names:
            self.failed_files_count = len(failed_names)
            max_names = 200
            head = ", ".join(failed_names[:max_names])
            tail = " ..." if len(failed_names) > max_names else ""
            emit_log(f"Збої попередньої обробки у {len(failed_names)} файлів: {head}{tail}", level="WARNING")

    @staticmethod
    def process_video_file(path, number_of_frames=3) -> tuple[str, list[int | None], str]:
        campaign = extract_campaign_name(os.path.basename(path))
        frames = extract_video_frames(path, number_of_frames=number_of_frames)

        if frames is None:
            raise ValueError(f"Failed to extract frames from video: {path}")

        phash = []
        for frame in frames:
            if frame is None:
                raise ValueError(f"Extracted None frame from video: {path}")
            phash.append(compute_phash_from_image(frame))

        return path, phash, campaign

    def compare_campaign_videos(self, files):
        if not files:
            raise ValueError("Received empty list of files for comparison.")

        similar_pairs = []
        n = len(files)

        for i in range(n):
            for j in range(i + 1, n):
                sim = compare_video_hash(files[i], files[j], self)
                if sim >= 1.0:
                    similar_pairs.append((files[i], files[j], sim, 'hash'))

        return similar_pairs

    def process_batches(self, trigger, admin_prompt, delay=0):
        batch_results = {}
        processed_files = []

        total_files_upper_bound = len(self.video_files)
        pbar = tqdm(total=total_files_upper_bound, desc="Класифікація (репрезентативи)", unit="vid")

        classify_workers = self.classify_concurrency
        max_in_flight = max(2, classify_workers * 2)

        def _submit(_executor, group):
            return _executor.submit(trigger.classify_group, group, admin_prompt, number_of_frames=self.number_of_frames, request_spacing_sec=delay)

        futures = set()
        with ThreadPoolExecutor(max_workers=classify_workers) as executor:
            for campaign, files in self.campaigns.items():
                try:
                    result = self.callback(campaign, files, self)
                    batch_results[campaign] = result

                    reps_in_campaign = len(result.keys())
                    for rep in result.keys():
                        processed_files.append(rep)

                    pbar.update(reps_in_campaign)

                    while len(processed_files) >= self.batch_group_size:
                        current_group = processed_files[: self.batch_group_size]
                        del processed_files[: self.batch_group_size]
                        futures.add(_submit(executor, current_group))

                        if len(futures) >= max_in_flight:
                            done = next(as_completed(futures))
                            futures.remove(done)
                            formatted = done.result()
                            if formatted:
                                trigger.add_data_to_dataframe(formatted)

                except Exception as exc:
                    emit_log(f"Помилка при обробці кампанії {campaign}: {exc}")

            if processed_files:
                futures.add(_submit(executor, processed_files))

            for fut in as_completed(futures):
                formatted = fut.result()
                if formatted:
                    trigger.add_data_to_dataframe(formatted)

        self.df = trigger.get_dataframe()

        for campaign, result_dict in batch_results.items():
            for key_video, dup_videos in result_dict.items():
                if dup_videos:
                    key_row = self.df[self.df["FileName"] == os.path.basename(key_video)]
                    if not key_row.empty:
                        for dup_video in dup_videos:
                            dup_row = key_row.copy()
                            dup_row.loc[:, "FileName"] = os.path.basename(dup_video)
                            self.df = pd.concat([self.df, dup_row], ignore_index=True)

        pbar.close()

        # Очистка пам'яті
        self.video_phashes.clear()
        self.campaigns.clear()

        return batch_results


def process_video_batch(campaign, files, processor):
    emit_log(f'Обробка відео кампанії: {campaign} ({len(files)} відео)')

    similar_pairs = processor.compare_campaign_videos(files)

    result_dict = merge_similar_pairs(similar_pairs, files)
    return result_dict

def run_video_processing(shared_list, root_directory, similarity_threshold, number_of_frames, batch_group_size, delay_between_calls, df_header, admin_prompt, partial_csv_path, progress, files_list: list[str] | None = None, dry_run: bool = False):
    t_pipeline_start = time.perf_counter()

    try:
        writer = IncrementalCSVWriter(partial_csv_path, df_header)
        trigger = ChatGPTCaller(df_header=df_header, writer=writer, progress=progress, progress_key='vid')

        processor = VideoBatchProcessor(
            root_dir=root_directory,
            callback=process_video_batch,
            batch_group_size=batch_group_size,
            number_of_frames=number_of_frames,
            similarity_threshold=similarity_threshold,
            num_workers=8,
            classify_concurrency=8
        )

        processor.traverse_directories(files_list=files_list)
        progress['vid_total'] = len(processor.video_files)
        emit_log(f'Знайдено {len(processor.video_files)} відео.')

        if dry_run:
            import os, pandas as pd  # noqa
            processor.df = pd.DataFrame({"FileName": [os.path.basename(p) for p in processor.video_files]})
            progress['vid_done'] = int(progress.get('vid_done', 0) or 0) + len(processor.video_files)
            shared_list.append(processor.df)
            return {}

        batch_results = processor.process_batches(trigger=trigger, admin_prompt=admin_prompt, delay=delay_between_calls)

        # Дублікати
        try:
            vid_dup_total = 0
            for _campaign, result_dict in batch_results.items():
                for _rep, dups in result_dict.items():
                    if dups:
                        vid_dup_total += len(dups)

            progress['vid_dups'] = int(progress.get('vid_dups', 0) or 0) + int(vid_dup_total)
        except Exception as e:
            emit_log(f"Не вдалося порахувати video dup-и: {e}", level="WARNING")

        total_reps = sum(len(result_dict) for result_dict in batch_results.values())
        vid_dup_total = int(progress.get('vid_dups', 0) or 0)

        # Пошкоджені
        progress["classify_broken_video_all"] = int(processor.failed_files_count)

        # Сумаризація
        emit_log(f'Videos summary: campaigns={len(batch_results)}, representatives={total_reps}, duplicates={vid_dup_total}')
        shared_list.append(processor.df)

    finally:
        if progress is not None:
            try:
                elapsed = max(time.perf_counter() - t_pipeline_start, 0.0)
                progress["pipeline_time_total_video_sec"] = float(progress.get("pipeline_time_total_video_sec", 0.0) or 0.0) + elapsed
            except Exception: # noqa
                pass