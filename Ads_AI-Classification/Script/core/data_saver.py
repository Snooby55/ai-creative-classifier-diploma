
from __future__ import annotations
import os, time, shutil
import pandas as pd
import multiprocessing as mp
from typing import Iterable, Sequence, List, Optional

from Script.core.console_bus import emit_log

def _read_partial_csvs(paths: Sequence[str]) -> List[pd.DataFrame]:
    frames: List[pd.DataFrame] = []
    for p in paths:
        if not p:
            continue
        try:
            if os.path.exists(p) and os.path.getsize(p) > 0:
                df = pd.read_csv(p, sep=';')
                frames.append(df)
                emit_log(f"Прочитано partial CSV: {p}")
        except Exception as e:
            emit_log(f"Не вдалось прочитати {p}: {e}", level="WARNING")
    return frames

def _from_shared_list(shared_list) -> List[pd.DataFrame]:
    frames: List[pd.DataFrame] = []
    try:
        data = list(shared_list) if shared_list is not None else []
        if not data:
            return frames

        if isinstance(data[0], dict):
            frames.append(pd.DataFrame(data))
        else:
            for it in data:
                if isinstance(it, pd.DataFrame):
                    frames.append(it)
        emit_log(f"Додано {len(data)} елементів із shared_list")
    except Exception as e:
        emit_log(f"Помилка читання shared_list: {e}", level="WARNING")
    return frames

def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.drop(columns=['№'], inplace=True, errors='ignore')
    if 'FileName' in df.columns:
        df['№'] = pd.to_numeric(
            df['FileName'].str.extract(r'^(\d+)_')[0],
            errors='coerce'
        ).astype('Int64')
    else:
        df['№'] = pd.NA
    return df

def _merge_links(df: pd.DataFrame, links_output_path: str) -> pd.DataFrame:
    if not (links_output_path and os.path.exists(links_output_path)):
        return df
    chunks = []
    try:
        for chunk in pd.read_csv(
            links_output_path,
            header=None,
            names=['Link', 'IsDownload', 'Company', 'Num'],
            chunksize=10000
        ):
            chunks.append(chunk[chunk['IsDownload'] == '200'])
    except Exception as e:
        emit_log(f"Не вдалось прочитати links файл: {e}", level="WARNING")
        return df

    if not chunks:
        return df

    links_df = pd.concat(chunks, ignore_index=True)
    links_df['Num'] = pd.to_numeric(links_df['Num'], errors='coerce')
    links_df.dropna(subset=['Num'], inplace=True)
    links_df['Num'] = links_df['Num'].astype(int)
    links_df = links_df.drop_duplicates(subset='Num')

    df = df.merge(links_df[['Num', 'Link']], left_on='№', right_on='Num', how='left')
    df.drop(columns=['Num'], inplace=True, errors='ignore')
    return df

def assemble_results(partial_paths: Sequence[str], shared_list) -> Optional[pd.DataFrame]:
    """Читає partial CSV (якщо є) або shared_list, конкатенує і нормалізує."""
    frames = _from_shared_list(shared_list)
    if not frames:
        frames = _read_partial_csvs(partial_paths)
    if not frames:
        return None
    df = pd.concat(frames, ignore_index=True)
    return _normalize(df)


def finalize_results(
    links_output_path: str,
    partial_image_csv: str,
    partial_video_csv: str,
    shared_list,
    debug_excel_path: str = None,
    save_debug_in_xlsx = False
) -> Optional[pd.DataFrame]:
    """Збирає результати, робить merge з links та записує у Excel. Повертає шлях до файлу або None."""
    df = assemble_results([partial_image_csv, partial_video_csv], shared_list)
    if df is None:
        emit_log("Немає результатів для вивантаження.", level="WARNING")
        return None

    df = _merge_links(df, links_output_path)
    all_cols = list(df.columns)
    cols = [c for c in ['№', 'FileName'] if c in all_cols] + [c for c in all_cols if c not in ('№', 'FileName')]

    if save_debug_in_xlsx and debug_excel_path is not None:
        try:
            df[cols].to_excel(debug_excel_path, index=False)
            emit_log(f"Результати записані у: {debug_excel_path}")
        except Exception as e:
            emit_log(f"Помилка запису Excel: {e}", level="ERROR")

    return df[cols]

def delete_temp_cvs(partial_image_csv: str, partial_video_csv: str):
    for pth in (partial_image_csv, partial_video_csv):
        try:
            if pth and os.path.exists(pth):
                os.remove(pth)
                emit_log(f"Видалено файл: {pth}")
        except Exception as e:
            emit_log(f"Не вдалось видалити {pth}: {e}", level="WARNING")

    out_dirs = set(filter(None, (os.path.dirname(partial_image_csv), os.path.dirname(partial_video_csv))))
    for d in out_dirs:
        if os.path.isdir(d):
            try:
                shutil.rmtree(d, ignore_errors=False)
                emit_log(f"Видалено директорію: {d}")
            except Exception as e:
                emit_log(f"Не вдалось видалити директорію {d}: {e}", level="WARNING")


def stop_and_cleanup(
    procs: Iterable[mp.Process],
    renderer,
    shared_list,
    progress,
    partial_image_csv: str,
    partial_video_csv: str,
    links_output_path: str,
    debug_excel_path: str,
    delete_out: bool = True
) -> Optional[str]:
    """
    Акуратно зупиняє підпроцеси, збирає часткові результати і зберігає в Excel.
    Після цього (опційно) видаляє partial-файли та директорію 'out'.
    Повертає шлях до збереженого Excel або None.
    """
    emit_log("Запущено аварійну зупинку…", level="WARNING")

    # 1) Зупинка процесів
    for p in procs:
        if p.is_alive():
            emit_log(f"terminate -> {p.name}", level="INFO")
            try:
                p.terminate()
            except Exception as e:
                emit_log(f"terminate({p.name}) помилка: {e}", level="WARNING")

    deadline = time.time() + 5.0
    for p in procs:
        try:
            p.join(timeout=max(0, deadline - time.time()))
        except Exception:
            pass

    # 2) Фіналізація результатів
    res_df = finalize_results(
        links_output_path=links_output_path,
        partial_image_csv=partial_image_csv,
        partial_video_csv=partial_video_csv,
        shared_list=shared_list,
        debug_excel_path=debug_excel_path,
        save_debug_in_xlsx=True
    )

    # 3) Прибирання тимчасових файлів/папок
    if delete_out:
        delete_temp_cvs(partial_image_csv, partial_video_csv)

    # 4) Зупиняємо рендерер
    try:
        renderer.stop()
    except Exception:
        pass

    emit_log("Аварійна зупинка завершена.", level="WARNING")
    return res_df
