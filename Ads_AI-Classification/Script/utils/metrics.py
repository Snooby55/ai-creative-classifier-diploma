from __future__ import annotations

import os
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Iterable, Optional

import pandas as pd

Number = float | int


# ---------------------- FILETYPE SETS ----------------------
IMG_EXTS = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff",
}

VID_EXTS = {
    ".mp4", ".avi", ".mov", ".mkv", ".webm", ".m4v", ".mpeg", ".mpg",
}


def ext_from_filename(name: str) -> str:
    """
    Акуратно дістає розширення з імені файла/URL.
    (не тягнемо history_worker імпортом, щоб не створювати цикли)
    """
    s = (str(name) if name is not None else "").strip()
    if not s:
        return ""
    base = os.path.basename(s)
    base = base.split("?")[0].split("#")[0]
    if "." not in base:
        return ""
    ext = base.rsplit(".", 1)[-1].lower()
    return f".{ext}" if ext else ""


def _safe_div(num: Number, den: Number) -> float:
    den_f = float(den) if den is not None else 0.0
    if den_f <= 0:
        return 0.0
    return float(num) * 100.0 / den_f


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _norm_link(value: Any) -> str:
    if value is None:
        return ""
    s = str(value).strip()
    return s


def _normalize_type(value: Any) -> str:
    """
    Приводимо будь-які варіації до: video | banner | other
    """
    s = str(value or "").strip().lower()
    if not s:
        return "other"

    if s in {"video", "vid", "mp4"}:
        return "video"

    if s in {"banner", "image", "img", "png", "jpg", "jpeg", "screenshot"}:
        return "banner"

    # "video_youtube", "video_direct", etc.
    if "video" in s:
        return "video"
    if "banner" in s or "image" in s or "img" in s:
        return "banner"

    return "other"


_MISSING = {"", "-", "not found", "not classified", "none", "nan"}


def _is_missing(cell: Any) -> bool:
    return cell is None or str(cell).strip().lower() in _MISSING


def should_write_product(product: Any) -> bool:
    """
    "Правильно класифіковано" = підлягає запису в історію.
    Логіка: brand і product не можуть бути одночасно пустими/"-"/"not found".
    Працюємо з форматом: "Brand || Product || Category" (із пробілами або без).
    """
    raw = "" if product is None else str(product)
    parts = [p.strip() for p in re.split(r"\s*\|\|\s*", raw)]
    while len(parts) < 3:
        parts.append("")

    brand, prod = parts[0], parts[1]
    return not (_is_missing(brand) and _is_missing(prod))


def _build_type_map(
    *,
    links_df_full: pd.DataFrame,
    res_df: Optional[pd.DataFrame],
    known_df: Optional[pd.DataFrame],
) -> pd.DataFrame:
    """
    Будуємо мапу Link -> Type з пріоритетом:
      1) links_df_full["Type"] (найбільш авторитетне, бо з download-скрипта)
      2) res_df["FileName"] (ext)
      3) known_df["Ext"] (ext)

    Повертає df з колонками: Link, Type
    """
    parts: list[pd.DataFrame] = []

    # 3) known_df (найнижчий пріоритет)
    if isinstance(known_df, pd.DataFrame) and not known_df.empty and "Link" in known_df.columns:
        df = known_df[["Link"]].copy()
        df["Link"] = df["Link"].map(_norm_link)
        if "Ext" in known_df.columns:
            df["Ext"] = known_df["Ext"].astype(str).str.lower()
            df["Type"] = df["Ext"].map(
                lambda e: "banner" if e in IMG_EXTS else ("video" if e in VID_EXTS else "other")
            )
        else:
            df["Type"] = "other"
        parts.append(df[["Link", "Type"]])

    # 2) res_df by FileName
    if isinstance(res_df, pd.DataFrame) and not res_df.empty and "Link" in res_df.columns:
        cols = [c for c in ["Link", "FileName"] if c in res_df.columns]
        df = res_df[cols].copy()
        df["Link"] = df["Link"].map(_norm_link)
        if "FileName" not in df.columns:
            df["FileName"] = ""
        df["Ext"] = df["FileName"].map(ext_from_filename).str.lower()
        df["Type"] = df["Ext"].map(
            lambda e: "banner" if e in IMG_EXTS else ("video" if e in VID_EXTS else "other")
        )
        parts.append(df[["Link", "Type"]])

    # 1) links_df_full["Type"] (найвищий пріоритет)
    if isinstance(links_df_full, pd.DataFrame) and not links_df_full.empty and "Link" in links_df_full.columns:
        if "Type" in links_df_full.columns:
            df = links_df_full[["Link", "Type"]].copy()
            df["Link"] = df["Link"].map(_norm_link)
            df["Type"] = df["Type"].map(_normalize_type)
            parts.append(df[["Link", "Type"]])

    if not parts:
        return pd.DataFrame(columns=["Link", "Type"])

    out = pd.concat(parts, ignore_index=True)
    out = out[out["Link"].astype(str).str.len() > 0]
    out = out.drop_duplicates(subset=["Link"], keep="last")
    return out


def _links_set(df: Optional[pd.DataFrame], col: str = "Link") -> set[str]:
    if not isinstance(df, pd.DataFrame) or df.empty or col not in df.columns:
        return set()
    return {x for x in df[col].map(_norm_link).tolist() if x}


def _prepare_links_df(links_df_full: Optional[pd.DataFrame]) -> pd.DataFrame:
    if not isinstance(links_df_full, pd.DataFrame) or links_df_full.empty:
        return pd.DataFrame(columns=["Link", "IsDownload"])

    df = links_df_full.copy()
    if "Link" not in df.columns:
        df["Link"] = ""
    df["Link"] = df["Link"].map(_norm_link)

    if "IsDownload" not in df.columns:
        df["IsDownload"] = 0

    df["IsDownload"] = pd.to_numeric(df["IsDownload"], errors="coerce").fillna(0).astype(int)
    return df


def _count_all_unique(
    df_identified: pd.DataFrame,
    *,
    link_set: set[str],
    type_col: str = "Type",
) -> dict[str, int]:
    """
    Рахуємо all/unique загалом і по типах на базі identified rows (IsDownload==200).
    """
    if df_identified.empty:
        return {
            "all": 0,
            "unique": 0,
            "video_all": 0,
            "video_unique": 0,
            "banner_all": 0,
            "banner_unique": 0,
        }

    sub = df_identified[df_identified["Link"].isin(link_set)].copy()
    if sub.empty:
        return {
            "all": 0,
            "unique": 0,
            "video_all": 0,
            "video_unique": 0,
            "banner_all": 0,
            "banner_unique": 0,
        }

    out: dict[str, int] = {}
    out["all"] = int(len(sub))
    out["unique"] = int(sub["Link"].nunique())

    sub_video = sub[sub[type_col] == "video"]
    sub_banner = sub[sub[type_col] == "banner"]

    out["video_all"] = int(len(sub_video))
    out["video_unique"] = int(sub_video["Link"].nunique()) if not sub_video.empty else 0

    out["banner_all"] = int(len(sub_banner))
    out["banner_unique"] = int(sub_banner["Link"].nunique()) if not sub_banner.empty else 0

    return out


@dataclass
class RunMetrics:
    # ------------------ ОПИСОВІ ------------------
    report_date: str
    generated_at: str
    report_type: str
    stage: int

    # ------------------ TOTAL (лінки) ------------------
    total_links_all: int = 0
    total_links_unique: int = 0

    # ------------------ IDENTIFIED (IsDownload==200) ------------------
    identified_all: int = 0
    identified_unique: int = 0
    identified_all_pct_of_all: float = 0.0
    identified_unique_pct_of_unique: float = 0.0

    identified_video_all: int = 0
    identified_video_unique: int = 0
    identified_banner_all: int = 0
    identified_banner_unique: int = 0

    identified_video_all_pct_of_identified: float = 0.0
    identified_banner_all_pct_of_identified: float = 0.0
    identified_video_unique_pct_of_identified_unique: float = 0.0
    identified_banner_unique_pct_of_identified_unique: float = 0.0

    # ------------------ CLASSIFIED VIA API ------------------
    classified_api_all: int = 0
    classified_api_unique: int = 0
    classified_api_all_pct_of_identified: float = 0.0
    classified_api_unique_pct_of_identified_unique: float = 0.0

    classified_api_video_all: int = 0
    classified_api_video_unique: int = 0
    classified_api_banner_all: int = 0
    classified_api_banner_unique: int = 0

    classified_api_video_all_pct_of_identified_video: float = 0.0
    classified_api_banner_all_pct_of_identified_banner: float = 0.0
    classified_api_video_unique_pct_of_identified_video_unique: float = 0.0
    classified_api_banner_unique_pct_of_identified_banner_unique: float = 0.0

    # ------------------ CORRECTLY CLASSIFIED (for history) ------------------
    classified_correct_all: int = 0
    classified_correct_unique: int = 0
    classified_correct_all_pct_of_identified: float = 0.0
    classified_correct_unique_pct_of_identified_unique: float = 0.0

    classified_correct_video_all: int = 0
    classified_correct_video_unique: int = 0
    classified_correct_banner_all: int = 0
    classified_correct_banner_unique: int = 0

    classified_correct_video_all_pct_of_identified_video: float = 0.0
    classified_correct_banner_all_pct_of_identified_banner: float = 0.0
    classified_correct_video_unique_pct_of_identified_video_unique: float = 0.0
    classified_correct_banner_unique_pct_of_identified_banner_unique: float = 0.0

    # ------------------ HISTORY (taken before API) ------------------
    hist_taken_all: int = 0
    hist_taken_unique: int = 0
    hist_taken_video_all: int = 0
    hist_taken_banner_all: int = 0

    # ratio: history / (history + correctly_classified + duplicates_of_correctly_classified)
    # (дублікати вже входять у classified_correct_all)
    hist_taken_pct_of_pool: float = 0.0
    hist_taken_video_pct_of_pool: float = 0.0
    hist_taken_banner_pct_of_pool: float = 0.0

    # ------------------ BROKEN DOWNLOAD ------------------
    broken_links_all: int = 0
    broken_links_unique: int = 0

    # ------------------ TIME: UPLOAD ------------------
    upload_time_total_creatives_sec: float = 0.0
    upload_time_avg_creative_sec: float = 0.0
    upload_time_total_video_sec: float = 0.0
    upload_time_avg_video_sec: float = 0.0
    upload_time_total_banner_sec: float = 0.0
    upload_time_avg_banner_sec: float = 0.0

    # ------------------ TIME: CLASSIFICATION ------------------
    classify_time_total_creatives_sec: float = 0.0
    classify_time_avg_creative_sec: float = 0.0
    classify_time_total_video_sec: float = 0.0
    classify_time_avg_video_sec: float = 0.0
    classify_time_total_banner_sec: float = 0.0
    classify_time_avg_banner_sec: float = 0.0

    # ------------------ TIME: PIPELINE ------------------
    pipeline_time_total_creatives_sec: float = 0.0
    pipeline_time_avg_creative_sec: float = 0.0
    pipeline_time_total_video_sec: float = 0.0
    pipeline_time_avg_video_sec: float = 0.0
    pipeline_time_total_banner_sec: float = 0.0
    pipeline_time_avg_banner_sec: float = 0.0

    # ------------------ TOKENS / MONEY ------------------
    img_tok_in: int = 0
    img_tok_out: int = 0
    vid_tok_in: int = 0
    vid_tok_out: int = 0

    img_cost_usd: float = 0.0
    vid_cost_usd: float = 0.0
    total_cost_usd: float = 0.0

    # ------------------ DEBUG / TECH ------------------
    img_api_rows: int = 0
    vid_api_rows: int = 0
    img_dups: int = 0
    vid_dups: int = 0

    classify_broken_banner_all: int = 0
    classify_broken_video_all: int = 0
    classify_broken_creatives_all: int = 0

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @property
    def api_accuracy_pct(self) -> float:
        return _safe_div(max(self.classified_correct_all - self.hist_taken_all, 0), self.classified_api_all)

    @property
    def api_accuracy_unique_pct(self) -> float:
        return _safe_div(max(self.classified_correct_unique - self.hist_taken_unique, 0), self.classified_api_unique)

    @classmethod
    def build_from_runtime(
        cls,
        *,
        progress: dict,
        links_df_full: pd.DataFrame,
        report_df: pd.DataFrame,
        res_df: pd.DataFrame,
        known_df: Optional[pd.DataFrame] = None,
        when: Optional[str] = None,
    ) -> "RunMetrics":
        when = when or datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        report_date = str(progress.get("report_date") or datetime.now().strftime("%Y-%m-%d"))
        report_type = str(progress.get("report_type") or "weekly")
        stage = _to_int(progress.get("stage", 1), 1)

        # -------- base dfs --------
        links_df = _prepare_links_df(links_df_full)

        total_links_all = int(len(links_df))
        total_links_unique = int(links_df["Link"].nunique()) if not links_df.empty else 0

        identified_mask = (links_df["IsDownload"] == 200) & (links_df["Link"].astype(str).str.len() > 0)
        df_identified = links_df[identified_mask].copy()

        identified_all = int(len(df_identified))
        identified_unique = int(df_identified["Link"].nunique()) if not df_identified.empty else 0

        identified_all_pct_of_all = _safe_div(identified_all, total_links_all)
        identified_unique_pct_of_unique = _safe_div(identified_unique, total_links_unique)

        # -------- type map --------
        type_map = _build_type_map(
            links_df_full=links_df,
            res_df=res_df,
            known_df=known_df,
        )

        if "Type" in df_identified.columns:
            df_identified = df_identified.drop(columns=["Type"])

        df_identified = df_identified.merge(type_map, on="Link", how="left")

        if "Type" not in df_identified.columns:
            df_identified["Type"] = "other"

        df_identified["Type"] = df_identified["Type"].fillna("other").map(_normalize_type)

        identified_video_all = int((df_identified["Type"] == "video").sum())
        identified_banner_all = int((df_identified["Type"] == "banner").sum())

        identified_video_unique = int(df_identified[df_identified["Type"] == "video"]["Link"].nunique()) if identified_video_all > 0 else 0
        identified_banner_unique = int(df_identified[df_identified["Type"] == "banner"]["Link"].nunique()) if identified_banner_all > 0 else 0

        identified_video_all_pct_of_identified = _safe_div(identified_video_all, identified_all)
        identified_banner_all_pct_of_identified = _safe_div(identified_banner_all, identified_all)

        identified_video_unique_pct_of_identified_unique = _safe_div(identified_video_unique, identified_unique)
        identified_banner_unique_pct_of_identified_unique = _safe_div(identified_banner_unique, identified_unique)

        # -------- API classified set (факт виклику API) --------
        api_links = _links_set(res_df, "Link")

        # -------- history set (взято ДО класифікації) --------
        hist_links = _links_set(known_df, "Link")

        # -------- product map (для визначення "правильності") --------
        correct_links: set[str] = set()
        if isinstance(report_df, pd.DataFrame) and not report_df.empty and "Link" in report_df.columns:
            rep = report_df.copy()
            if "Product" not in rep.columns:
                rep["Product"] = ""
            rep["Link"] = rep["Link"].map(_norm_link)

            rep = rep[rep["Link"].astype(str).str.len() > 0]
            rep = rep.drop_duplicates(subset=["Link"], keep="last")

            prod_map = dict(zip(rep["Link"].tolist(), rep["Product"].tolist()))
            for ln in api_links:
                if should_write_product(prod_map.get(ln, "")):
                    correct_links.add(ln)

        # -------- counts: API classified / correct --------
        api_counts = _count_all_unique(df_identified, link_set=api_links, type_col="Type")
        correct_counts = _count_all_unique(df_identified, link_set=correct_links, type_col="Type")
        hist_counts = _count_all_unique(df_identified, link_set=hist_links, type_col="Type")

        classified_api_all = api_counts["all"]
        classified_api_unique = api_counts["unique"]

        classified_api_all_pct_of_identified = _safe_div(classified_api_all, identified_all)
        classified_api_unique_pct_of_identified_unique = _safe_div(classified_api_unique, identified_unique)

        classified_api_video_all = api_counts["video_all"]
        classified_api_video_unique = api_counts["video_unique"]
        classified_api_banner_all = api_counts["banner_all"]
        classified_api_banner_unique = api_counts["banner_unique"]

        classified_api_video_all_pct_of_identified_video = _safe_div(classified_api_video_all, identified_video_all)
        classified_api_banner_all_pct_of_identified_banner = _safe_div(classified_api_banner_all, identified_banner_all)
        classified_api_video_unique_pct_of_identified_video_unique = _safe_div(classified_api_video_unique, identified_video_unique)
        classified_api_banner_unique_pct_of_identified_banner_unique = _safe_div(classified_api_banner_unique, identified_banner_unique)

        # --- history ---
        hist_taken_all = hist_counts["all"]
        hist_taken_unique = hist_counts["unique"]
        hist_taken_video_all = hist_counts["video_all"]
        hist_taken_banner_all = hist_counts["banner_all"]

        # --- NEW meaning: "correct" = (API-correct + taken from history in this run) ---
        classified_correct_all = correct_counts["all"] + hist_taken_all
        classified_correct_unique = correct_counts["unique"] + hist_taken_unique
        classified_correct_video_all = correct_counts["video_all"] + hist_taken_video_all
        classified_correct_video_unique = correct_counts["video_unique"] + hist_counts["video_unique"]
        classified_correct_banner_all = correct_counts["banner_all"] + hist_taken_banner_all
        classified_correct_banner_unique = correct_counts["banner_unique"] + hist_counts["banner_unique"]

        # --- percentages must be based on UPDATED correct ---
        classified_correct_all_pct_of_identified = _safe_div(classified_correct_all, identified_all)
        classified_correct_unique_pct_of_identified_unique = _safe_div(classified_correct_unique, identified_unique)
        classified_correct_video_all_pct_of_identified_video = _safe_div(classified_correct_video_all, identified_video_all)
        classified_correct_banner_all_pct_of_identified_banner = _safe_div(classified_correct_banner_all, identified_banner_all)
        classified_correct_video_unique_pct_of_identified_video_unique = _safe_div(classified_correct_video_unique, identified_video_unique)
        classified_correct_banner_unique_pct_of_identified_banner_unique = _safe_div(classified_correct_banner_unique, identified_banner_unique)

        # -------- history pct --------
        hist_taken_pct_of_pool = _safe_div(hist_taken_all, classified_correct_all)
        hist_taken_video_pct_of_pool = _safe_div(hist_taken_video_all, classified_correct_video_all)
        hist_taken_banner_pct_of_pool = _safe_div(hist_taken_banner_all, classified_correct_banner_all)

        # -------- broken download --------
        broken_df = links_df[links_df["IsDownload"] != 200].copy()
        broken_links_all = int(len(broken_df))
        broken_links_unique = int(broken_df["Link"].nunique()) if not broken_df.empty else 0

        # -------- progress: tech, tokens, money --------
        img_api_rows = _to_int(progress.get("img_done", 0))
        vid_api_rows = _to_int(progress.get("vid_done", 0))

        img_dups = _to_int(progress.get("img_dups", 0))
        vid_dups = _to_int(progress.get("vid_dups", 0))

        classify_broken_banner_all = _to_int(progress.get("classify_broken_banner_all", 0))
        classify_broken_video_all = _to_int(progress.get("classify_broken_video_all", 0))
        classify_broken_creatives_all = classify_broken_banner_all + classify_broken_video_all

        img_tok_in = _to_int(progress.get("img_tok_in_uncached", 0)) + _to_int(progress.get("img_tok_in_cached", 0))
        img_tok_out = _to_int(progress.get("img_tok_out", 0))
        vid_tok_in = _to_int(progress.get("vid_tok_in_uncached", 0)) + _to_int(progress.get("vid_tok_in_cached", 0))
        vid_tok_out = _to_int(progress.get("vid_tok_out", 0))

        img_cost_usd = _to_float(progress.get("img_usd_total", 0.0))
        vid_cost_usd = _to_float(progress.get("vid_usd_total", 0.0))
        total_cost_usd = _to_float(progress.get("usd_total", 0.0))

        # -------- time: upload --------
        upload_time_total_creatives_sec = _to_float(progress.get("upload_time_total_creatives_sec", 0.0))
        upload_time_total_video_sec = _to_float(progress.get("upload_time_total_video_sec", 0.0))
        upload_time_total_banner_sec = _to_float(progress.get("upload_time_total_banner_sec", 0.0))

        upload_items_creatives = max(_to_int(progress.get("upload_items_creatives", 0)), 0)
        upload_items_video = max(_to_int(progress.get("upload_items_video", 0)), 0)
        upload_items_banner = max(_to_int(progress.get("upload_items_banner", 0)), 0)

        upload_time_avg_creative_sec = (
            upload_time_total_creatives_sec / upload_items_creatives
            if upload_items_creatives > 0 else 0.0
        )
        upload_time_avg_video_sec = (
            upload_time_total_video_sec / upload_items_video
            if upload_items_video > 0 else 0.0
        )
        upload_time_avg_banner_sec = (
            upload_time_total_banner_sec / upload_items_banner
            if upload_items_banner > 0 else 0.0
        )

        # -------- time: classify --------
        classify_time_total_creatives_sec = _to_float(progress.get("classify_time_total_creatives_sec", 0.0))
        classify_time_total_video_sec = _to_float(progress.get("classify_time_total_video_sec", 0.0))
        classify_time_total_banner_sec = _to_float(progress.get("classify_time_total_banner_sec", 0.0))

        api_items_total = max(img_api_rows + vid_api_rows, 0)
        classify_time_avg_creative_sec = (
            classify_time_total_creatives_sec / api_items_total
            if api_items_total > 0 else 0.0
        )
        classify_time_avg_video_sec = (
            classify_time_total_video_sec / vid_api_rows
            if vid_api_rows > 0 else 0.0
        )
        classify_time_avg_banner_sec = (
            classify_time_total_banner_sec / img_api_rows
            if img_api_rows > 0 else 0.0
        )

        # -------- time: pipeline --------
        pipeline_time_total_creatives_sec = _to_float(progress.get("pipeline_time_total_creatives_sec", 0.0))
        pipeline_time_total_video_sec = _to_float(progress.get("pipeline_time_total_video_sec", 0.0))
        pipeline_time_total_banner_sec = _to_float(progress.get("pipeline_time_total_banner_sec", 0.0))

        # pipeline avg — логічніше ділити на identified_all (масштаб прогону)
        pipeline_time_avg_creative_sec = (
            pipeline_time_total_creatives_sec / identified_all
            if identified_all > 0 else 0.0
        )
        pipeline_time_avg_video_sec = (
            pipeline_time_total_video_sec / max(identified_video_all, 0)
            if identified_video_all > 0 else 0.0
        )
        pipeline_time_avg_banner_sec = (
            pipeline_time_total_banner_sec / max(identified_banner_all, 0)
            if identified_banner_all > 0 else 0.0
        )

        return cls(
            report_date=report_date,
            generated_at=when,
            report_type=report_type,
            stage=stage,
            total_links_all=total_links_all,
            total_links_unique=total_links_unique,
            identified_all=identified_all,
            identified_unique=identified_unique,
            identified_all_pct_of_all=identified_all_pct_of_all,
            identified_unique_pct_of_unique=identified_unique_pct_of_unique,
            identified_video_all=identified_video_all,
            identified_video_unique=identified_video_unique,
            identified_banner_all=identified_banner_all,
            identified_banner_unique=identified_banner_unique,
            identified_video_all_pct_of_identified=identified_video_all_pct_of_identified,
            identified_banner_all_pct_of_identified=identified_banner_all_pct_of_identified,
            identified_video_unique_pct_of_identified_unique=identified_video_unique_pct_of_identified_unique,
            identified_banner_unique_pct_of_identified_unique=identified_banner_unique_pct_of_identified_unique,
            classified_api_all=classified_api_all,
            classified_api_unique=classified_api_unique,
            classified_api_all_pct_of_identified=classified_api_all_pct_of_identified,
            classified_api_unique_pct_of_identified_unique=classified_api_unique_pct_of_identified_unique,
            classified_api_video_all=classified_api_video_all,
            classified_api_video_unique=classified_api_video_unique,
            classified_api_banner_all=classified_api_banner_all,
            classified_api_banner_unique=classified_api_banner_unique,
            classified_api_video_all_pct_of_identified_video=classified_api_video_all_pct_of_identified_video,
            classified_api_banner_all_pct_of_identified_banner=classified_api_banner_all_pct_of_identified_banner,
            classified_api_video_unique_pct_of_identified_video_unique=classified_api_video_unique_pct_of_identified_video_unique,
            classified_api_banner_unique_pct_of_identified_banner_unique=classified_api_banner_unique_pct_of_identified_banner_unique,
            classified_correct_all=classified_correct_all,
            classified_correct_unique=classified_correct_unique,
            classified_correct_all_pct_of_identified=classified_correct_all_pct_of_identified,
            classified_correct_unique_pct_of_identified_unique=classified_correct_unique_pct_of_identified_unique,
            classified_correct_video_all=classified_correct_video_all,
            classified_correct_video_unique=classified_correct_video_unique,
            classified_correct_banner_all=classified_correct_banner_all,
            classified_correct_banner_unique=classified_correct_banner_unique,
            classified_correct_video_all_pct_of_identified_video=classified_correct_video_all_pct_of_identified_video,
            classified_correct_banner_all_pct_of_identified_banner=classified_correct_banner_all_pct_of_identified_banner,
            classified_correct_video_unique_pct_of_identified_video_unique=classified_correct_video_unique_pct_of_identified_video_unique,
            classified_correct_banner_unique_pct_of_identified_banner_unique=classified_correct_banner_unique_pct_of_identified_banner_unique,
            hist_taken_all=hist_taken_all,
            hist_taken_unique=hist_taken_unique,
            hist_taken_video_all=hist_taken_video_all,
            hist_taken_banner_all=hist_taken_banner_all,
            hist_taken_pct_of_pool=hist_taken_pct_of_pool,
            hist_taken_video_pct_of_pool=hist_taken_video_pct_of_pool,
            hist_taken_banner_pct_of_pool=hist_taken_banner_pct_of_pool,
            broken_links_all=broken_links_all,
            broken_links_unique=broken_links_unique,
            upload_time_total_creatives_sec=upload_time_total_creatives_sec,
            upload_time_avg_creative_sec=upload_time_avg_creative_sec,
            upload_time_total_video_sec=upload_time_total_video_sec,
            upload_time_avg_video_sec=upload_time_avg_video_sec,
            upload_time_total_banner_sec=upload_time_total_banner_sec,
            upload_time_avg_banner_sec=upload_time_avg_banner_sec,
            classify_time_total_creatives_sec=classify_time_total_creatives_sec,
            classify_time_avg_creative_sec=classify_time_avg_creative_sec,
            classify_time_total_video_sec=classify_time_total_video_sec,
            classify_time_avg_video_sec=classify_time_avg_video_sec,
            classify_time_total_banner_sec=classify_time_total_banner_sec,
            classify_time_avg_banner_sec=classify_time_avg_banner_sec,
            pipeline_time_total_creatives_sec=pipeline_time_total_creatives_sec,
            pipeline_time_avg_creative_sec=pipeline_time_avg_creative_sec,
            pipeline_time_total_video_sec=pipeline_time_total_video_sec,
            pipeline_time_avg_video_sec=pipeline_time_avg_video_sec,
            pipeline_time_total_banner_sec=pipeline_time_total_banner_sec,
            pipeline_time_avg_banner_sec=pipeline_time_avg_banner_sec,
            img_tok_in=img_tok_in,
            img_tok_out=img_tok_out,
            vid_tok_in=vid_tok_in,
            vid_tok_out=vid_tok_out,
            img_cost_usd=img_cost_usd,
            vid_cost_usd=vid_cost_usd,
            total_cost_usd=total_cost_usd,
            img_api_rows=img_api_rows,
            vid_api_rows=vid_api_rows,
            img_dups=img_dups,
            vid_dups=vid_dups,
            classify_broken_banner_all=classify_broken_banner_all,
            classify_broken_video_all=classify_broken_video_all,
            classify_broken_creatives_all=classify_broken_creatives_all,
        )
