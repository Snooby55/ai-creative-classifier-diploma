
import os
import time
import pandas as pd
from concurrent.futures import ThreadPoolExecutor, as_completed
from tqdm import tqdm
from PIL import Image

from Script.core.console_bus import emit_log
from Script.core.csv_writer import IncrementalCSVWriter
from Script.utils.utils import recursive_call, extract_campaign_name, compute_phash_from_path, extract_gif_frame, compare_images_hash,  merge_similar_pairs, compare_images_clip
from Script.api.trigger import ChatGPTCaller

class ImageBatchProcessor:
    def __init__(self, root_dir, callback, batch_group_size, frame_ratio=0.5, similarity_threshold=0.9, num_workers=8, classify_concurrency=2):
        self.root_dir = root_dir
        self.callback = callback  # Callback для обробки батчу (кампанії)
        self.batch_group_size = batch_group_size
        self.frame_ratio = frame_ratio
        self.similarity_threshold = similarity_threshold
        self.num_workers = num_workers
        self.classify_concurrency = classify_concurrency

        self.image_files = []       # Повні шляхи до зображень
        self.images = []            # PIL.Image об’єкти (відмасштабовані)
        self.image_phashes = {}     # {file_path: phash}
        self.campaigns = {}         # {campaign_name: [file_path, ...]}
        self.embeddings = []        # Ембеддінги отримані від CLIP

        self.failed_files_count: int = 0

        self.file_to_index = {}

        self.df = pd.DataFrame()

        emit_log("Завантаження CLIP моделі...")
        from sentence_transformers import SentenceTransformer
        self.model = SentenceTransformer('clip-ViT-B-32')

        # Методи порівняння зображень:
        self.image_compare_methods = [
            ("hash", compare_images_hash, 1.0),  # tolerance 0 для абсолютного співпадіння
            ("clip", compare_images_clip, self.similarity_threshold)
        ]

    def traverse_directories(self, files_list: list[str] | None = None):
        if files_list is not None:
            all_files = [p for p in files_list if str(p).lower().endswith(('.jpg', '.jpeg', '.png', '.gif'))]
        else:
            all_files = []
            for dirpath, _, filenames in os.walk(self.root_dir):
                for file in filenames:
                    if file.lower().endswith(('.jpg', '.jpeg', '.png', '.gif')):
                        all_files.append(os.path.join(dirpath, file))

        failed_names: list[str] = []

        with ThreadPoolExecutor(max_workers=self.num_workers) as executor:
            futures = {executor.submit(self.process_image_file, path, self.frame_ratio): path for path in all_files}
            for future in tqdm(as_completed(futures), total=len(futures), desc="Обробка зображень", disable=os.environ.get("TQDM_DISABLE", "0") == "1"):
                path = futures[future]
                try:
                    path, phash, campaign = future.result()
                except Exception: # noqa
                    failed_names.append(os.path.basename(path))
                    continue

                try:
                    if path.lower().endswith('.gif'):
                        img = extract_gif_frame(path, self.frame_ratio)
                    else:
                        with Image.open(path) as im:
                            img = im.convert("RGB")
                    img = img.resize((300, 300), Image.LANCZOS) # noqa
                except Exception: # noqa
                    failed_names.append(os.path.basename(path))
                    continue

                self.image_files.append(path)
                self.images.append(img)
                self.image_phashes[path] = phash
                self.file_to_index[path] = len(self.images) - 1
                if campaign:
                    self.campaigns.setdefault(campaign, []).append(path)

        if failed_names:
            self.failed_files_count = len(failed_names)
            max_names = 200
            head = ", ".join(failed_names[:max_names])
            tail = " ..." if len(failed_names) > max_names else ""
            emit_log(f"Збої попередньої обробки у {len(failed_names)} файлів: {head}{tail}", level="WARNING")

        emit_log("Обчислення embeddings.")
        self.embeddings = self.model.encode(self.images, batch_size=128, convert_to_tensor=True, show_progress_bar=True)

        # Очистка пам'яті
        import gc
        self.images.clear()
        try:
            del self.model
        except Exception: # noqa
            pass
        gc.collect()

    @staticmethod
    def process_image_file(path, frame_ratio):
        try:
            phash = compute_phash_from_path(path, frame_ratio)
            campaign = extract_campaign_name(os.path.basename(path))
            return path, phash, campaign
        except Exception as e:
            emit_log(f"Помилка у process_image_file для {path}: {e}")
            return path, None, None

    def compare_campaign_images_clip(self, files, excluded_files):
        import torch
        from sentence_transformers import util

        subset = []
        for f in files:
            if f not in excluded_files and f in self.file_to_index:
                idx = self.file_to_index[f]
                subset.append((f, self.embeddings[idx]))

        if not subset:
            return []

        embeddings_subset = torch.stack([emb for (f, emb) in subset])
        results = util.paraphrase_mining_embeddings(embeddings_subset)
        output = []
        for score, i, j in results:
            f_i, _ = subset[i]
            f_j, _ = subset[j]
            if score >= self.similarity_threshold:
                output.append((f_i, f_j, float(score), 'clip'))
        return output

    def prefilter_hash(self, files):
        hash_groups = {}
        for f in files:
            phash = self.image_phashes.get(f)
            if phash is not None:
                hash_groups.setdefault(str(phash), []).append(f)

        identical_groups = [group for group in hash_groups.values() if len(group) > 1]
        return identical_groups

    def process_batches(self, trigger, admin_prompt, delay=0):
        batch_results = {}
        processed_files = []
        total_files_upper_bound = len(self.image_files)
        pbar = tqdm(total=total_files_upper_bound, desc="Класифікація (репрезентативи)", unit="img")

        classify_workers = self.classify_concurrency
        max_in_flight = max(2, classify_workers * 2)

        def _submit(_executor, group):
            return _executor.submit(trigger.classify_group, group, admin_prompt, number_of_frames=0, request_spacing_sec=delay)

        futures = set()
        with ThreadPoolExecutor(max_workers=classify_workers) as executor:
            for campaign, files in self.campaigns.items():
                identical_groups = self.prefilter_hash(files)
                try:
                    result = self.callback(campaign, files, self, identical_groups)
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
            for key_img, dup_imgs in result_dict.items():
                if dup_imgs:
                    key_row = self.df[self.df['FileName'] == os.path.basename(key_img)]
                    if not key_row.empty:
                        for dup_img in dup_imgs:
                            dup_row = key_row.copy()
                            dup_row.loc[:, 'FileName'] = os.path.basename(dup_img)
                            self.df = pd.concat([self.df, dup_row], ignore_index=True)

        pbar.close()

        # Очистка пам'яті
        self.embeddings = None
        self.image_phashes.clear()
        self.file_to_index.clear()
        self.campaigns.clear()

        return batch_results

def process_image_batch(campaign, files, processor, identical_groups):
    emit_log(f'Обробка кампанії з зображень: {campaign} ({len(files)} файлів)')
    similar_pairs = []

    for group in identical_groups:
        campaign_group = [f for f in group if f in files]
        if len(campaign_group) > 1:
            for i in range(len(campaign_group)):
                for j in range(i + 1, len(campaign_group)):
                    score = compare_images_hash(campaign_group[i], campaign_group[j], processor, tolerance=0)
                    if score >= 1.0:
                        similar_pairs.append((campaign_group[i], campaign_group[j], score, 'hash'))

    excluded_files = set()
    for group in identical_groups:
        for f in group[1:]:
            excluded_files.add(f)

    clip_pairs = processor.compare_campaign_images_clip(files, excluded_files)
    similar_pairs.extend(clip_pairs)

    result_dict = merge_similar_pairs(similar_pairs, files)
    return result_dict

def run_image_processing(shared_list, root_directory, similarity_threshold, frame_ratio, batch_group_size, delay_between_calls, df_header, admin_prompt, partial_csv_path, progress, files_list: list[str] | None = None, dry_run: bool = False):
    t_pipeline_start = time.perf_counter()

    try:
        writer = IncrementalCSVWriter(partial_csv_path, df_header)
        trigger = ChatGPTCaller(df_header=df_header, writer=writer, progress=progress, progress_key='img')

        processor = ImageBatchProcessor(
            root_dir=root_directory,
            callback=process_image_batch,
            batch_group_size=batch_group_size,
            frame_ratio=frame_ratio,
            similarity_threshold=similarity_threshold,
            num_workers=8,
            classify_concurrency=8
        )

        processor.traverse_directories(files_list=files_list)
        progress['img_total'] = len(processor.image_files)
        emit_log(f'Знайдено {len(processor.image_files)} зображень.')

        if dry_run:
            import os, pandas as pd  # noqa
            processor.df = pd.DataFrame({"FileName": [os.path.basename(p) for p in processor.image_files]})
            progress['img_done'] = int(progress.get('img_done', 0) or 0) + len(processor.image_files)
            shared_list.append(processor.df)
            return {}

        batch_results = processor.process_batches(trigger=trigger, admin_prompt=admin_prompt, delay=delay_between_calls)

        # Дублікати
        try:
            img_dup_total = 0
            for _campaign, result_dict in batch_results.items():
                for _rep, dups in result_dict.items():
                    if dups:
                        img_dup_total += len(dups)

            progress['img_dups'] = int(progress.get('img_dups', 0) or 0) + int(img_dup_total)
        except Exception as e:
            emit_log(f"Не вдалося порахувати image dup-и: {e}", level="WARNING")

        total_reps = sum(len(result_dict) for result_dict in batch_results.values())
        total_dups = int(progress.get('img_dups', 0) or 0)

        # Пошкоджені
        progress["classify_broken_banner_all"] = int(processor.failed_files_count)

        # Сумаризація
        emit_log(f'Images summary: campaigns={len(batch_results)}, representatives={total_reps}, duplicates={total_dups}')
        shared_list.append(processor.df)

    finally:
        # Пайплайновий час
        if progress is not None:
            try:
                elapsed = max(time.perf_counter() - t_pipeline_start, 0.0)
                progress["pipeline_time_total_banner_sec"] = float(progress.get("pipeline_time_total_banner_sec", 0.0) or 0.0) + elapsed
            except Exception: # noqa
                pass