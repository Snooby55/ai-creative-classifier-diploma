from __future__ import annotations

import os
import re
import time
import json
import traceback
import importlib
import inspect
import pandas as pd
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import multiprocessing as mp

from Script.config import APP_CONFIG
from Script.utils.metrics import RunMetrics
from Script.core.console_bus import ConsoleRenderer, init_console_client, emit_progress, emit_log
from Script.core.data_saver import finalize_results, delete_temp_cvs, stop_and_cleanup
from Script.core.history_worker import LinksHistoryManager, combine_columns_to_two
from Script.api.admin_prompt_builder import PromptBuilder

# ---------------------- TYPES ----------------------
@dataclass
class JobSpec:
    """Опис однієї джоби/процесу. entrypoint: 'module.sub:callable'"""
    name: str
    entrypoint: str
    kwargs: dict[str, Any]

# ---------------------- HELPERS ----------------------
def _call(func, kwargs: dict[str, Any]):
    """Передаємо у func лише ті kwargs, які вона справді приймає (або всі, якщо має **kwargs)."""
    sig = inspect.signature(func)
    params = sig.parameters
    if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return func(**kwargs)

    allowed = {}
    for name, p in params.items():
        if p.kind == inspect.Parameter.POSITIONAL_ONLY:
            continue
        if name in kwargs:
            allowed[name] = kwargs[name]

    return func(**allowed)

def generic_worker(entrypoint: str, job_kwargs: dict, console_q):
    """Top-level для Process: підключаємо шину, вимикаємо локальні tqdm, викликаємо цільову функцію."""
    init_console_client(console_q, disable_local_tqdm=True, redirect_streams=True, attach_logging=True, capture_native_fds=True)
    os.environ["TQDM_DISABLE"] = "1"

    module_name, func_name = entrypoint.split(":", 1)
    try:
        func = getattr(importlib.import_module(module_name), func_name)
        return _call(func, job_kwargs)
    except Exception: # noqa
        tb = traceback.format_exc()
        emit_log(f"Worker {mp.current_process().name} crashed:\n{tb}", level="ERROR")
        return None

def build_common_kwargs(shared_list, root_directory, similarity_threshold, delay, df_header, progress):
    return {
        'shared_list': shared_list,
        'root_directory': root_directory,
        'similarity_threshold': similarity_threshold,
        'delay_between_calls': delay,
        'df_header': df_header,
        'progress': progress,
    }

def make_process(job: JobSpec, console_q) -> mp.Process:
    return mp.Process(
        target=generic_worker,
        args=(job.entrypoint, dict(job.kwargs), console_q),
        name=job.name
    )

def make_report_path(reports_dir: str, report_type: str, report_date_iso: str) -> str:
    try:
        dt_report = datetime.strptime(str(report_date_iso).strip(), "%Y-%m-%d")
    except Exception: # noqa
        dt_report = datetime.now()

    dd = dt_report.strftime("%d")
    mon_short = dt_report.strftime("%b")
    yyyy = dt_report.strftime("%Y")

    created_at = datetime.now().strftime("%Y-%m-%d %H-%M")
    base_name = f"{str(report_type).strip().lower()} report {dd} {mon_short} {yyyy} (created at {created_at})"

    # Заборонені у файлових іменах символи -> підкреслення
    safe_name = re.sub(r'[<>:"/\\|?*]+', "_", base_name)
    return os.path.join(reports_dir, f"{safe_name}.csv")


# ---------------------- MAIN ----------------------
def main(
    _creatives_directory: str | None = None,
    _links_output_path: str | None = None,
    *,
    paths: dict[str, Any] | None = None,
    flags: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
):
    """
    :param _creatives_directory: Директорія з креативами (можна не передавати, якщо є у paths)
    :param _links_output_path: Шлях до output.txt (можна не передавати, якщо є у paths)
    :param paths: явні конфіги (fallback на APP_CONFIG)
    :param flags: явні конфіги (fallback на APP_CONFIG)
    :param params: явні конфіги (fallback на APP_CONFIG)
    """
    mp.freeze_support()

    console_q = mp.Queue()
    init_console_client(console_q, disable_local_tqdm=False, redirect_streams=False, attach_logging=True)
    renderer = ConsoleRenderer(console_q)
    renderer.start()

    # -------- Конфіг --------
    paths  = paths  or APP_CONFIG.get("paths", {})
    flags  = flags  or APP_CONFIG.get("flags", {})
    params = params or APP_CONFIG.get("params", {})

    root_directory   = _creatives_directory or paths.get("creatives_directory")
    links_output_path = _links_output_path or paths.get("links_output_path")
    if not root_directory:
        raise ValueError("Не заданий шлях: creatives_directory. Передайте або встановіть у config.py")
    if not links_output_path:
        raise ValueError("Не заданий шлях: links_output_path. Передайте або встановіть у config.py")

    classification_xls          = paths.get("classification_excel")  # (може бути None - ок)
    admin_prompt_schema         = paths.get("admin_prompt_schema")
    partial_image_csv           = paths.get("partial_image_csv")
    partial_video_csv           = paths.get("partial_video_csv")
    debug_output_excel_results  = paths.get("debug_output_excel_results")
    history_store_path          = paths.get("history_store_path")
    reports_dir                 = paths.get("reports_dir")

    upload_runtime: dict[str, Any] = {}
    runtime_upload_path = paths.get("runtime_upload")
    if runtime_upload_path:
        try:
            with open(runtime_upload_path, "r", encoding="utf-8") as f:
                raw = f.read()

            if raw.strip():
                data = json.loads(raw)
                if isinstance(data, dict):
                    upload_runtime = data
        except Exception as e:
            emit_log(f"Не вдалося прочитати runtime_upload.json: {e}", level="WARNING")

    process_images        = bool(flags.get("process_images", True))
    process_videos        = bool(flags.get("process_videos", True))
    save_debug_excel_always = bool(flags.get("save_debug_excel_always", False))

    similarity_threshold = float(params.get("similarity_threshold", 0.98))
    frame_ratio          = float(params.get("frame_ratio", 0.5))
    image_batch_size     = int(params.get("image_batch_size", 10))
    number_of_frames     = int(params.get("number_of_frames", 2))
    video_batch_size     = int(params.get("video_batch_size", 5))
    delay                = int(params.get("delay", 1))

    # --------- Report file ---------
    os.makedirs(reports_dir, exist_ok=True)

    report_type = str(params.get("report_type", "weekly")).strip().lower()
    report_date_raw = str(params.get("report_date", "")).strip()  # очікуємо YYYY-MM-DD
    try:
        dt = datetime.strptime(report_date_raw, "%Y-%m-%d")
    except Exception: # noqa
        dt = datetime.now()

    run_report_path = make_report_path(reports_dir, report_type, dt.strftime("%Y-%m-%d"))

    # --------- PRE-PROCESS LINKS ---------
    hm = LinksHistoryManager(history_store_path)
    links_df_full = hm.read_links_output(links_output_path)

    extra_links_path = paths.get("extra_known_links_path")
    extra_links = []
    if extra_links_path:
        try:
            with open(extra_links_path, "r", encoding="utf-8", errors="ignore") as f:
                extra_links = [ln.strip() for ln in f if ln.strip()]
        except Exception: # noqa
            extra_links = []

    if extra_links:
        _extra_df = pd.DataFrame({"Link": extra_links, "IsDownload": 200})
        links_df_full = pd.concat([links_df_full, _extra_df], ignore_index=True).drop_duplicates(subset=["Link"], keep="last")

    links_200 = links_df_full[links_df_full["IsDownload"] == 200].copy()
    known_df = hm.find_known_products(links_200)

    known_links = set(known_df["Link"].tolist()) if not known_df.empty else set()
    unknown_200 = links_200[~links_200["Link"].isin(known_links)].copy()
    allowed_ids = set(unknown_200["Num"].dropna().astype(int).tolist())

    img_paths, vid_paths = hm.iter_paths_by_ids(directory=root_directory, allowed_ids=allowed_ids, recursive=True)
    emit_log(f"Пре-процесинг: зібрано файлів — images={len(img_paths)}, videos={len(vid_paths)}, унікальних ID={len(allowed_ids)}")

    # ---------- (TEST) Dry Run ----------
    dry_run = bool(flags.get("dry_run", False))
    stress_img_items = int(params.get("stress_img_items", 0))
    stress_vid_items = int(params.get("stress_vid_items", 0))
    if dry_run and stress_img_items > 0 and stress_vid_items > 0:
        from math import ceil
        if process_images and img_paths:
            k = ceil(stress_img_items / max(1, len(img_paths)))
            img_paths = (img_paths * k)[:stress_img_items]
        if process_videos and vid_paths:
            k = ceil(stress_vid_items / max(1, len(vid_paths)))
            vid_paths = (vid_paths * k)[:stress_vid_items]

    # ---------- PROMPT BUILDER ----------
    # Для зміни видаліть файл admin_prompt_schema з папки, якщо він там є
    pb = PromptBuilder(admin_prompt_schema, create_if_missing=True)

    if len(pb.columns) == 1:
        pb.add_column(
            "Brand",
            image_rule="company or trademark (e.g. Samsung, L'Oreal), or 'Not found'", image_example="Samsung",
            video_rule="company or trademark (e.g. Samsung, L'Oreal), or 'Not found'", video_example="Samsung",
        )
        pb.add_column(
            "Product",
            image_rule="model name/number (e.g. S24, A15), or 'Not found'", image_example="S24",
            video_rule="model name/number (e.g. S24, A15), or 'Not found'", video_example="S24",
        )
        pb.add_column(
            "Category",
            image_rule="general type (e.g. kitchen grill, gaming laptop, throat lozenges), or 'Not found'", image_example="Smartphone",
            video_rule="general type (e.g. kitchen grill, gaming laptop, throat lozenges), or 'Not found'", video_example="Smartphone",
        )
        # pb.add_column(
        #     "Description",
        #     image_rule="A very brief description of what is in the image", image_example="Front view of Galaxy S24 display",
        #     video_rule="A very brief description of what is in the video", video_example="Front view of Galaxy S24 display",
        # )
        # pb.save()

    # pb.add_classification_sheet(
    #     excel_path=classification_xls,
    #     column_index=2,  # 3-я колонка в Excel
    #     max_items=300,
    #     image_rule="Pick the best matching label from the classification list; else 'Not classified'.",
    #     video_rule="Pick the best matching label from the classification list using all frames; else 'Not classified'.",
    #     position=1,  # після '№'
    #     overwrite=True
    # )

    image_admin_prompt = pb.build_image_prompt().prompt_text
    video_admin_prompt = pb.build_video_prompt().prompt_text
    df_header = pb.df_header

    # ---------- Час старту роботи класифікації ----------
    classify_started_at = time.perf_counter()

    # ---------- Manager ----------
    manager = mp.Manager()

    run_metrics: RunMetrics | None = None
    try:
        progress = manager.dict({
            'img_total': 0,
            'img_done': 0,
            'vid_total': 0,
            'vid_done': 0,

            # токени (загальні)
            'tok_in_uncached': 0,
            'tok_in_cached': 0,
            'tok_out': 0,
            'tok_total': 0,

            # гроші (загальні)
            'usd_in_uncached': 0.0,
            'usd_in_cached': 0.0,
            'usd_out': 0.0,
            'usd_total': 0.0,

            # гроші по типах
            'img_usd_total': 0.0,
            'vid_usd_total': 0.0,

            # час (upload + classify)
            'upload_time_total_creatives_sec': 0.0,

            # NEW: upload breakdown
            'upload_time_total_video_sec': 0.0,
            'upload_time_total_banner_sec': 0.0,

            # NEW: upload counters (for avg time)
            'upload_items_creatives': 0,
            'upload_items_video': 0,
            'upload_items_banner': 0,

            'classify_time_total_creatives_sec': 0.0,
            'classify_time_total_banner_sec': 0.0,
            'classify_time_total_video_sec': 0.0,

            # пошкоджені під час класифікації
            'classify_broken_banner_all': 0,
            'classify_broken_video_all': 0,
        })

        # Якщо завантажувач записав runtime_upload.json - прокидуємо час у прогрес
        # Якщо завантажувач записав runtime_upload.json - прокидуємо час/лічильники у progress
        if upload_runtime:
            try:
                # total wall time (як було)
                progress["upload_time_total_creatives_sec"] = float(upload_runtime.get("upload_time_total_creatives_sec", 0.0) or 0.0)

                # per type time (сек)
                v_direct = float(upload_runtime.get("download_time_video_direct_sec", 0.0) or 0.0)
                v_yt = float(upload_runtime.get("download_time_video_youtube_sec", 0.0) or 0.0)
                img = float(upload_runtime.get("download_time_images_sec", 0.0) or 0.0)

                progress["upload_time_total_video_sec"] = v_direct + v_yt
                progress["upload_time_total_banner_sec"] = img

                # items (для середнього часу)
                items_video = int(upload_runtime.get("downloaded_video_direct_items", 0) or 0) + int(upload_runtime.get("downloaded_video_youtube_items", 0) or 0)
                items_banner = int(upload_runtime.get("downloaded_image_items", 0) or 0)
                items_creatives = int(upload_runtime.get("downloaded_rows_200", 0) or 0)

                progress["upload_items_video"] = items_video
                progress["upload_items_banner"] = items_banner
                progress["upload_items_creatives"] = items_creatives

            except Exception as e:
                emit_log(f"Не вдалося застосувати runtime_upload у progress: {e}", level="WARNING")

        # додаємо метадані репорту, щоб history записала поля Report Type / Report Date / Stage
        progress["report_type"] = report_type # noqa
        progress["report_date"] = dt.strftime("%Y-%m-%d") # noqa
        progress["stage"] = int(params.get("stage", 1))

        shared_list = manager.list()

        # Job specs
        common_kwargs = build_common_kwargs(shared_list, root_directory, similarity_threshold, delay, df_header, progress)

        image_kwargs = {
            **common_kwargs,
            'frame_ratio': frame_ratio,
            'batch_group_size': image_batch_size,
            'admin_prompt': image_admin_prompt,
            'partial_csv_path': partial_image_csv,
            'files_list': img_paths,
            'dry_run': dry_run,
        }

        video_kwargs = {
            **common_kwargs,
            'number_of_frames': number_of_frames,
            'batch_group_size': video_batch_size,
            'admin_prompt': video_admin_prompt,
            'partial_csv_path': partial_video_csv,
            'files_list': vid_paths,
            'dry_run': dry_run,
        }

        jobs: list[JobSpec] = []
        if process_images:
            jobs.append(JobSpec("ImageProc", "Script.pipelines.image_pipeline:run_image_processing", image_kwargs))
        if process_videos:
            jobs.append(JobSpec("VideoProc", "Script.pipelines.video_pipeline:run_video_processing", video_kwargs))

        procs = [make_process(j, console_q) for j in jobs]

        def _sig_handler(signum, frame): # noqa
            emit_log(f"Отримано сигнал {signum}: зупиняємо…", level="WARNING")
            stop_and_cleanup(
                procs=procs, renderer=renderer, shared_list=shared_list, progress=progress,
                partial_image_csv=partial_image_csv, partial_video_csv=partial_video_csv,
                links_output_path=links_output_path, debug_excel_path=debug_output_excel_results, delete_out=True
            )

            os._exit(0)

        try:
            import signal
            signal.signal(signal.SIGINT, _sig_handler)
            try:
                signal.signal(signal.SIGTERM, _sig_handler)
            except Exception: # noqa
                pass
        except Exception: # noqa
            pass

        # Запуск воркерів
        for p in procs:
            p.start()

        def get_desc(_progress):
            return f"Загальний прогрес | in={int(_progress.get('tok_in_uncached', 0))} " \
                   f"cached={int(_progress.get('tok_in_cached', 0))} out={int(_progress.get('tok_out', 0))} " \
                   f"| $total={_progress.get('usd_total', 0.0):.6f}"

        while any(p.is_alive() for p in procs):
            img_dups = int(progress.get('img_dups', 0) or 0)
            vid_dups = int(progress.get('vid_dups', 0) or 0)
            emit_progress(
                img_total=int(progress.get('img_total', 0)),
                img_done=int(progress.get('img_done', 0)) + img_dups,
                vid_total=int(progress.get('vid_total', 0)),
                vid_done=int(progress.get('vid_done', 0)) + vid_dups,
                desc=get_desc(progress)
            )
            time.sleep(0.2)

        img_dups = int(progress.get('img_dups', 0) or 0)
        vid_dups = int(progress.get('vid_dups', 0) or 0)
        emit_progress(
            img_total=int(progress.get('img_total', 0)),
            img_done=int(progress.get('img_done', 0)) + img_dups,
            vid_total=int(progress.get('vid_total', 0)),
            vid_done=int(progress.get('vid_done', 0)) + vid_dups,
            desc=get_desc(progress)
        )

        for p in procs:
            try:
                p.join(timeout=5)
            except Exception: # noqa
                pass

        res_df = finalize_results(
            links_output_path=links_output_path,
            partial_image_csv=partial_image_csv,
            partial_video_csv=partial_video_csv,
            shared_list=shared_list,
            debug_excel_path=debug_output_excel_results,
            save_debug_in_xlsx=save_debug_excel_always
        )

        
        # --- SAFETY NET: якщо з data_saver прийшли рядки без Link, добудуємо Link із FileName (Num_*) через мапу Num->Link з output.txt ---
        try:
            if res_df is not None and not res_df.empty:
                if "Link" not in res_df.columns:
                    res_df["Link"] = ""

                link_series = res_df["Link"]
                missing_mask = link_series.isna() | (link_series.astype(str).str.strip() == "")
                if bool(missing_mask.any()):
                    if links_df_full is None or links_df_full.empty:
                        links_df_full = LinksHistoryManager.read_links_output(links_output_path)

                    if links_df_full is not None and not links_df_full.empty and "Num" in links_df_full.columns:
                        tmp_links = links_df_full.copy()
                        tmp_links["Num"] = pd.to_numeric(tmp_links["Num"], errors="coerce").astype("Int64")
                        tmp_links["Link"] = tmp_links["Link"].fillna("").astype(str).str.strip()
                        tmp_links = tmp_links[(tmp_links["Num"].notna()) & (tmp_links["Link"] != "")]
                        # останній запис для Num має пріоритет
                        id_to_link = dict(zip(tmp_links["Num"].astype(int).tolist(), tmp_links["Link"].tolist()))

                        if "FileName" in res_df.columns:
                            extracted = res_df["FileName"].fillna("").astype(str).str.extract(r"^(\d+)_", expand=False)
                            extracted = pd.to_numeric(extracted, errors="coerce").astype("Int64")
                            filled = extracted[missing_mask].map(id_to_link)
                            res_df.loc[missing_mask, "Link"] = filled

                            after_missing = res_df["Link"].isna() | (res_df["Link"].astype(str).str.strip() == "")
                            emit_log(
                                f"Link auto-fill: було порожніх={int(missing_mask.sum())}, "
                                f"стало порожніх={int(after_missing.sum())}",
                                level="INFO"
                            )

                res_df["Link"] = res_df["Link"].fillna("").astype(str).str.strip()
        except Exception as e:
            emit_log(f"Link auto-fill failed: {e}", level="WARNING")

        delete_temp_cvs(partial_image_csv, partial_video_csv)

        # --------- POST-PROCESS: History updates & Run report ---------
        report_saved_ok = False
        try:
            if res_df is not None:
                merge_cols = [col for col in pb.columns if col != "№"]
                try:
                    merged_products_df = combine_columns_to_two(
                        dfs=res_df,
                        cols_to_merge=merge_cols,
                        combined_col_name="Product",
                        link_col="Link",
                        sep=" || ",
                        replacements={"not found": "-", "not classified": "-", "не found": "-"},
                        empty_fill="-"
                    )[["Link", "Product"]]
                    ln_fn = res_df[["Link", "FileName"]].dropna(subset=["Link"]).drop_duplicates(subset=["Link"], keep="last")
                    merged_products_df = merged_products_df.merge(ln_fn, on="Link", how="left")
                except Exception as e:
                    emit_log(f"Помилка побудови Product: {e}", level="CRITICAL")
                    merged_products_df = pd.DataFrame(columns=["Link", "Product"])

                try:
                    hm.upsert_classified_to_history(merged_products_df, sep="||", delete_with=("-", "not found"))
                except Exception as e:
                    emit_log(f"Не вдалося оновити історію: {e}", level="CRITICAL")

                try:
                    if links_df_full is None:
                        links_df_full = LinksHistoryManager.read_links_output(links_output_path)

                    classified_all = merged_products_df
                    try:
                        if known_df is not None and not known_df.empty:
                            known_for_run = known_df[["Link", "Product"]].copy()
                            classified_all = pd.concat([known_for_run, merged_products_df], ignore_index=True).drop_duplicates(subset=["Link"], keep="last")
                    except Exception as e:
                        emit_log(f"Не вдалося об'єднати з known_df: {e}", level="ERROR")

                    report_df = hm.build_run_report(links_df_full, classified_all)

                    try:
                        q = hm.compute_quality_from_report(report_df, sep="||", delete_with=("-", "not found"))
                        progress["correct_count"] = int(q.get("correct_count", 0))
                        progress["incorrect_count"] = int(q.get("incorrect_count", 0))
                        progress["correct_pct"] = float(q.get("correct_pct", 0.0))
                        progress["incorrect_pct"] = float(q.get("incorrect_pct", 0.0))
                        progress["denom"] = int(q.get("denom", 0))
                    except Exception as e:
                        emit_log(f"Не вдалося порахувати якість по репорту: {e}", level="ERROR")

                    try:
                        hm.save_report(report_df, run_report_path)
                        report_saved_ok = True
                    except Exception as e:
                        emit_log(f"Не вдалося зберегти репорт: {e}", level="ERROR")

                    # Зафіксувати загальний час роботи класифікатора
                    try:
                        classify_elapsed = max(time.perf_counter() - classify_started_at, 0.0)
                        progress["pipeline_time_total_creatives_sec"] = float(progress.get("pipeline_time_total_creatives_sec", 0.0) or 0.0) + classify_elapsed
                    except Exception as e:
                        emit_log(f"Не вдалося зафіксувати час класифікації: {e}", level="ERROR")

                    # Вписуємо метрики (+ Report Type/Date/Stage беруться з progress)
                    try:
                        run_metrics = RunMetrics.build_from_runtime(
                            progress=dict(progress),
                            links_df_full=links_df_full,
                            report_df=report_df,
                            res_df=res_df,
                            known_df=known_df,
                        )
                        hm.append_metrics_row(metrics=run_metrics)
                    except Exception as e:
                        emit_log(f"Не вдалося записати метрики в історію: {e}", level="ERROR")

                    emit_log(f"Звіт класифікації збережено: {run_report_path}")
                except Exception as e:
                    emit_log(f"Помилка обробки метрик: {e}", level="ERROR")

        finally:
            if run_metrics is None:
                try:
                    run_metrics = RunMetrics.build_from_runtime(
                        progress=dict(progress),
                        links_df_full=links_df_full,
                        report_df=locals().get("report_df"),
                        res_df=locals().get("res_df"),
                        known_df=known_df,
                    )
                except Exception: # noqa
                    run_metrics = RunMetrics(
                        report_date=dt.strftime("%Y-%m-%d"),
                        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        report_type=report_type,
                        stage=int(params.get("stage", 1)),
                    )

            return {
                "metrics": run_metrics,
                "report_path": run_report_path if report_saved_ok and os.path.exists(run_report_path) else None,
                "stage": {
                    "number_of_frames": number_of_frames,
                    "video_batch_size": video_batch_size,
                },
            }

    except Exception as e:
        class ClassificatorError(Exception):
            """Classification error"""

        raise ClassificatorError(f"Classification was unsuccessful: {e}")

    finally:
        try:
            renderer.stop() # noqa
        except Exception: # noqa
            pass

        try:
            if manager is not None: # noqa
                manager.shutdown() # noqa
        except Exception: # noqa
            pass

        try:
            console_q.close() # noqa
            console_q.join_thread()
        except Exception: # noqa
            pass

if __name__ == "__main__":
    main()
