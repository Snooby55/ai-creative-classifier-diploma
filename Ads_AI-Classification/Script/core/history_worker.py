# ---------------------- NEW HISTORY / LINKS UTILITIES ----------------------
from __future__ import annotations

from pathlib import Path
import os, re, warnings
import time, glob
import shutil
from typing import Set, Tuple, Dict, List
from datetime import datetime

import numpy as np
import pandas as pd
import csv

from Script.utils.metrics import RunMetrics, IMG_EXTS, VID_EXTS, ext_from_filename

# ---------------------- GENERIC HELPERS ----------------------

def _now_date_str() -> str:
    """Return YYYY-MM-DD (local)."""
    return datetime.now().strftime("%Y-%m-%d")


def _is_excel_path(path: str) -> bool:
    p = str(path or "").lower()
    return p.endswith(".xlsx") or p.endswith(".xlsm")


def _ensure_history_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Ensure columns for the 'History' sheet in Excel mode."""
    need = ["Link", "DateAdded", "Status", "Ext", "Product"]
    out = df.copy() if df is not None else pd.DataFrame(columns=need)
    for c in need:
        if c not in out.columns:
            out[c] = "" if c in ("Product", "Ext") else pd.NA
    # dtypes
    out["Status"] = pd.to_numeric(out["Status"], errors="coerce").astype("Int64")
    out["Link"] = out["Link"].astype(str)
    out["Product"] = out["Product"].astype(str)
    out["DateAdded"] = out["DateAdded"].astype(str)
    out["Ext"] = out["Ext"].astype(str)
    return out[need]


def _read_excel_book(path: str) -> Dict[str, pd.DataFrame]:
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return {}
    try:
        return pd.read_excel(path, sheet_name=None)
    except Exception:
        return {}


def _write_excel_book_atomic(path: str, sheets: dict[str, pd.DataFrame]) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    root, ext = os.path.splitext(path)
    if not ext:
        ext = ".xlsx"  # безпечний дефолт

    tmp = f"{root}.tmp.{os.getpid()}.{int(time.time()*1000)}{ext}"
    with pd.ExcelWriter(tmp, engine="openpyxl") as xl:
        for name, df in sheets.items():
            (df if isinstance(df, pd.DataFrame) else pd.DataFrame(df)).to_excel(xl, sheet_name=name, index=False)

    LinksHistoryManager._robust_replace(tmp, path) # noqa


def _read_history_df_excel(path: str) -> pd.DataFrame:
    book = _read_excel_book(path)
    hist = book.get("History", pd.DataFrame(columns=["Link", "Status", "Product", "DateAdded", "Ext"]))
    return _ensure_history_columns(hist)


def _write_history_df_excel(path: str, history_df: pd.DataFrame) -> None:
    book = _read_excel_book(path)
    book["History"] = _ensure_history_columns(history_df)
    _write_excel_book_atomic(path, book)

def _append_metrics_excel(path: str, row_dict: dict) -> None:
    book = _read_excel_book(path)
    m = book.get("Metrics", pd.DataFrame()).copy()

    if m.empty:
        m = pd.DataFrame(columns=list(row_dict.keys()))

    for c in row_dict.keys():
        if c not in m.columns:
            m[c] = pd.NA

    row = {k: row_dict.get(k, pd.NA) for k in m.columns}

    m.loc[len(m)] = [row[c] for c in m.columns]
    book["Metrics"] = m[m.columns]
    _write_excel_book_atomic(path, book)


def _append_metrics_csv(base_path: str, row_dict: dict, sep: str = ";") -> None:
    import os, csv
    d = os.path.dirname(base_path) or "."
    stem, _ = os.path.splitext(os.path.basename(base_path))
    metrics_path = os.path.join(d, f"{stem}_metrics.csv")

    if not os.path.exists(metrics_path) or os.path.getsize(metrics_path) == 0:
        cols = list(row_dict.keys())
        write_header = True
    else:
        with open(metrics_path, "r", encoding="utf-8-sig", newline="") as f:
            r = csv.reader(f, delimiter=sep)
            first = next(r, None)
        cols = first or list(row_dict.keys())
        write_header = False
        for k in row_dict.keys():
            if k not in cols:
                cols.append(k)
                write_header = True

    def _norm(v):
        if v is None or (isinstance(v, float) and pd.isna(v)) or (isinstance(v, str) and v.strip() == ""):
            return ""
        return v

    normalized = {c: _norm(row_dict.get(c)) for c in cols}

    os.makedirs(os.path.dirname(metrics_path) or ".", exist_ok=True)
    file_exists = os.path.exists(metrics_path)

    with open(metrics_path, "a", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, delimiter=sep)
        if not file_exists or write_header:
            w.writeheader()
        w.writerow(normalized)




# ---------------------- PUBLIC UTILS ----------------------

def combine_columns_to_two(
    dfs: pd.DataFrame | List[pd.DataFrame],
    cols_to_merge: List[str],
    combined_col_name: str,
    link_col: str,
    sep: str = " || ",
    replacements: Dict[str, str] | None = None,
    empty_fill: str | None = None,
) -> pd.DataFrame:
    if isinstance(dfs, list):
        df = pd.concat(dfs, ignore_index=True)
    else:
        df = dfs.copy()

    missing = [c for c in cols_to_merge + [link_col] if c not in df.columns]
    if missing:
        raise ValueError(f"Відсутні колонки у датафреймі: {missing}")

    rep: Dict[str, str] = {}
    for k, v in (replacements or {}).items():
        key = str(k).lower()
        if key not in rep:
            rep[key] = v

    def _format_cell(value):
        if pd.isna(value):
            is_empty = True
            s = ""
        else:
            s = str(value).strip()
            is_empty = (s == "")
        sl = s.lower()
        if sl in rep:
            new = rep[sl]
            return "" if new is None else str(new)
        if is_empty:
            return "" if empty_fill is None else str(empty_fill)
        return s

    merged_series = df.apply(lambda row: sep.join([_format_cell(row[c]) for c in cols_to_merge]), axis=1)
    result = pd.DataFrame({combined_col_name: merged_series, link_col: df[link_col].values})
    return result[[combined_col_name, link_col]]


# ---------------------- MAIN CLASS ----------------------
class LinksHistoryManager:
    def __init__(self, history_path: str, csv_sep: str = ";"):
        self.history_path = history_path
        self.csv_sep = csv_sep

    # -------- storage helpers ----
    def _read_df_auto(self, path: str) -> pd.DataFrame:
        p = Path(path)
        if not p.exists() or p.stat().st_size == 0:
            return pd.DataFrame(columns=["Link", "Product", "Status"])

        ext = p.suffix.lower()
        try:
            if ext == ".parquet":
                return pd.read_parquet(path)
            if ext in (".feather", ".ft"):
                return pd.read_feather(path)
            if ext == ".csv":
                return pd.read_csv(path, sep=self.csv_sep, dtype={"Link": str, "Product": str, "Status": "Int16"})
            if _is_excel_path(path):
                return _read_history_df_excel(path)[["Link", "Status", "Product"]]
        except Exception as e:
            warnings.warn(f"Failed to read '{path}' as {ext}: {e}. Falling back to CSV.")
        return pd.read_csv(path)

    def _write_df_auto(self, df: pd.DataFrame, path: str) -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        ext = Path(path).suffix.lower()
        try:
            if ext == ".parquet":
                df.to_parquet(path, index=False); return
            if ext in (".feather", ".ft"):
                df.to_feather(path); return
            if ext == ".csv":
                df.to_csv(path, index=False, encoding="utf-8-sig", sep=self.csv_sep); return
            if _is_excel_path(path):
                _write_history_df_excel(path, _ensure_history_columns(df))
                return
        except Exception as e:
            warnings.warn(f"Failed to write '{path}' as {ext}: {e}. Falling back to CSV.")
        df.to_csv(path, index=False, encoding="utf-8-sig")

    @staticmethod
    def _robust_replace(src: str, dst: str, attempts: int = 15, base_sleep: float = 0.1) -> bool:
        """Надійна заміна файла з експоненціальною затримкою."""
        last_err = None
        for i in range(attempts):
            try:
                os.replace(src, dst)
                return True
            except PermissionError as e:
                last_err = e
                time.sleep(base_sleep * (i + 1))  # бекоф
            except Exception as e:
                last_err = e
                time.sleep(base_sleep)
        try:
            shutil.copy2(src, dst)
            os.remove(src)
            return True
        except Exception:
            warnings.warn(f"Failed to finalize '{dst}' from '{src}': {last_err}")
            return False

    def _recover_orphan_tmps(self, base_path: str) -> None:
        """Якщо залишились сирітські .tmp -> намагаємося їх дограти."""
        for tmp in glob.glob(base_path + ".tmp*"):
            try:
                self._robust_replace(tmp, base_path)
            except Exception:
                pass

    # -------- IO for links_output --------
    @staticmethod
    def read_links_output(links_output_path: str) -> pd.DataFrame:
        if not links_output_path or not os.path.exists(links_output_path):
            return pd.DataFrame(columns=["Link", "IsDownload", "Company", "Num", "Type"])

        chunks = []
        for chunk in pd.read_csv(links_output_path, header=0, dtype=str, chunksize=10000):
            chunk = chunk.rename(
                columns={
                    "link": "Link",
                    "status": "IsDownload",
                    "brand": "Company",
                    "number": "Num",
                    "type": "Type",
                }
            )
            chunks.append(chunk)

        if not chunks:
            return pd.DataFrame(columns=["Link", "IsDownload", "Company", "Num", "Type"])

        df = pd.concat(chunks, ignore_index=True)
        df["IsDownload"] = pd.to_numeric(df["IsDownload"], errors="coerce").fillna(0).astype(int)
        df["Num"] = pd.to_numeric(df["Num"], errors="coerce").astype("Int64")
        df = df[df["Link"].astype(str).str.startswith(("http://", "https://"), na=False)]

        if "Type" not in df.columns:
            df["Type"] = ""

        return df

    # -------- history upserts --------
    def upsert_history(self, updates: pd.DataFrame) -> pd.DataFrame:
        """
        Підтримка .xlsx файлу з листом 'History').
        Для .csv/.parquet/.feather - тепер теж зберігаємо DateAdded і Ext.
        Пріоритет статусів: 1 > 2 > 0.
        """
        if updates is None or updates.empty:
            return self._read_df_auto(self.history_path) if not _is_excel_path(
                self.history_path) else _read_history_df_excel(self.history_path)

        up = updates.drop_duplicates(subset=["Link"], keep="last").copy()
        up["Product"] = up["Product"].fillna("").astype(str)
        up["Status"] = pd.to_numeric(up["Status"], errors="coerce").astype("Int64")
        if "DateAdded" not in up.columns:
            up["DateAdded"] = ""
        if "Ext" not in up.columns:
            up["Ext"] = ""
        up = up[["Link", "DateAdded", "Status", "Ext", "Product"]]

        # Excel branch
        if _is_excel_path(self.history_path):
            hist = _read_history_df_excel(self.history_path)
            prio = {0: 0, 2: 1, 1: 2}
            hindex = {lk: i for i, lk in enumerate(hist["Link"].tolist())}

            for _, r in up.iterrows():
                lk = str(r["Link"])
                st_new = int(r["Status"]) if pd.notna(r["Status"]) else None
                prod_new = (r["Product"] or "").strip()
                dt_new = str(r["DateAdded"] or "").strip()
                ext_new = (r["Ext"] or "").strip()

                if lk in hindex:
                    i = hindex[lk]
                    try:
                        st_old = int(hist.at[i, "Status"]) if pd.notna(hist.at[i, "Status"]) else None
                    except Exception:
                        st_old = None
                    keep = st_old
                    if st_new is not None:
                        if keep is None or prio.get(st_new, 0) > prio.get(keep, 0):
                            keep = st_new
                    hist.at[i, "Status"] = keep

                    if prod_new:
                        hist.at[i, "Product"] = prod_new

                    if not str(hist.at[i, "DateAdded"] or "").strip():
                        hist.at[i, "DateAdded"] = dt_new or _now_date_str()

                    if not str(hist.at[i, "Ext"] or "").strip() and ext_new:
                        hist.at[i, "Ext"] = ext_new
                else:
                    hist = pd.concat([hist, pd.DataFrame([{
                        "Link": lk,
                        "Status": st_new,
                        "Product": prod_new,
                        "DateAdded": dt_new or _now_date_str(),
                        "Ext": ext_new
                    }])], ignore_index=True)

            _write_history_df_excel(self.history_path, hist)
            return hist

        # --- CSV/other branch
        base_path = self.history_path
        tmp_path = f"{base_path}.tmp.{os.getpid()}.{int(time.time() * 1000)}"
        self._recover_orphan_tmps(base_path)

        header_cols = ["Link", "DateAdded", "Status", "Ext", "Product"]

        def _norm_int(x):
            try:
                return int(x)
            except Exception:
                return None

        upd_map = {
            k: (_norm_int(v["Status"]), (v["Product"] or "").strip(), (v["DateAdded"] or "").strip(), (v["Ext"] or "").strip())
            for k, v in up.set_index("Link")[["DateAdded", "Status", "Ext", "Product"]].to_dict(orient="index").items()
        }
        seen = set()
        sep = self.csv_sep
        priority = {0: 0, 2: 1, 1: 2}

        with open(tmp_path, "w", newline="", encoding="utf-8-sig") as fout:
            w = csv.writer(fout, delimiter=sep)
            w.writerow(header_cols)

            if not os.path.exists(base_path) or os.path.getsize(base_path) == 0:
                add_df = up[header_cols].copy()
                add_df.loc[add_df["DateAdded"].astype(str).str.strip() == "", "DateAdded"] = _now_date_str()
                add_df.to_csv(fout, header=False, index=False, sep=sep, quoting=csv.QUOTE_MINIMAL)
            else:
                for chunk in pd.read_csv(base_path, sep=sep, dtype=str, chunksize=200_000):
                    out = chunk.copy()
                    for c in header_cols:
                        if c not in out.columns:
                            out[c] = ""

                    for idx, row in out.iterrows():
                        link = str(row["Link"])
                        if link in upd_map:
                            a = _norm_int(row.get("Status"))
                            b = upd_map[link][0]  # new status
                            keep = a if (a is not None and priority.get(a, 0) >= priority.get(b, 0)) else b
                            out.at[idx, "Status"] = keep

                            prod_new, dt_new, ext_new = upd_map[link][1], upd_map[link][2], upd_map[link][3]
                            if prod_new:
                                out.at[idx, "Product"] = prod_new

                            if not str(out.at[idx, "DateAdded"] or "").strip():
                                out.at[idx, "DateAdded"] = dt_new or _now_date_str()

                            if not str(out.at[idx, "Ext"] or "").strip() and ext_new:
                                out.at[idx, "Ext"] = ext_new

                            seen.add(link)

                    out[header_cols].to_csv(fout, header=False, index=False, sep=sep, quoting=csv.QUOTE_MINIMAL)

                missing_links = [lk for lk in upd_map.keys() if lk not in seen]
                if missing_links:
                    add_df = up[up["Link"].isin(missing_links)][header_cols].copy()
                    add_df.loc[add_df["DateAdded"].astype(str).str.strip() == "", "DateAdded"] = _now_date_str()
                    add_df.to_csv(fout, header=False, index=False, sep=sep, quoting=csv.QUOTE_MINIMAL)
            fout.flush()
            os.fsync(fout.fileno())

        self._robust_replace(tmp_path, base_path)
        return self._read_df_auto(base_path)

    def add_non200_to_history(self, links_df: pd.DataFrame) -> pd.DataFrame:
        if links_df is None or links_df.empty:
            return self._read_df_auto(self.history_path) if not _is_excel_path(self.history_path) else _read_history_df_excel(self.history_path)

        not200 = links_df[links_df["IsDownload"] != 200]
        if not200.empty:
            return self._read_df_auto(self.history_path) if not _is_excel_path(self.history_path) else _read_history_df_excel(self.history_path)

        updates = not200[["Link"]].drop_duplicates().copy()
        updates["Product"] = ""
        updates["Status"] = 0
        return self.upsert_history(updates)

    def find_known_products(self, links_df_200: pd.DataFrame) -> pd.DataFrame:
        if links_df_200 is None or links_df_200.empty:
            return pd.DataFrame(columns=["Link", "Product", "Ext"])

        path = self.history_path
        if not (path and os.path.exists(path)):
            return pd.DataFrame(columns=["Link", "Product", "Ext"])

        want = set(links_df_200["Link"].astype(str).tolist())

        if _is_excel_path(path):
            hist = _read_history_df_excel(path)
            part = hist[hist["Link"].isin(want)][["Link", "Product", "Ext"]]
            part = part[~part["Product"].isna() & (part["Product"].astype(str).str.strip() != "")]
            return part.drop_duplicates(subset=["Link"], keep="last")[["Product", "Link", "Ext"]]

        # CSV/other streaming read
        rows = []
        for chunk in pd.read_csv(path, sep=self.csv_sep, usecols=["Link", "Product", "Ext"], chunksize=200_000, dtype=str):
            part = chunk[chunk["Link"].isin(want)]
            if not part.empty:
                rows.append(part)

        if not rows:
            return pd.DataFrame(columns=["Link", "Product", "Ext"])

        df = pd.concat(rows, ignore_index=True)
        df = df[~df["Product"].isna() & (df["Product"].str.strip() != "")]
        df = df.drop_duplicates(subset=["Link"], keep="last")
        return df[["Product", "Link", "Ext"]]

    # -------- filesystem --------
    @staticmethod
    def iter_paths_by_ids(directory: str, allowed_ids: Set[int], recursive: bool = True, prefix_regex: str = r'^(\d+)_') -> Tuple[list[str], list[str]]:
        if not directory or not os.path.isdir(directory):
            return [], []

        pat = re.compile(prefix_regex)
        img_paths, vid_paths = [], []

        for root, _, files in os.walk(directory):
            for name in files:
                m = pat.match(name)
                if not (m and m.group(1).isdigit() and int(m.group(1)) in allowed_ids):
                    continue
                ext = os.path.splitext(name)[1].lower()
                full = os.path.join(root, name)
                if ext in IMG_EXTS:
                    img_paths.append(full)
                elif ext in VID_EXTS:
                    vid_paths.append(full)
            if not recursive:
                break

        return img_paths, vid_paths

    # -------- post-classification --------
    def upsert_classified_to_history(self, classified_products: pd.DataFrame, sep="||", delete_with: Tuple[str, ...] | None = ("-", "not found")) -> pd.DataFrame:
        if classified_products is None or classified_products.empty:
            return self._read_df_auto(self.history_path) if not _is_excel_path(self.history_path) else _read_history_df_excel(self.history_path)

        updates = classified_products.dropna(subset=["Link"]).drop_duplicates(subset=["Link"]).copy()
        updates["Product"] = updates["Product"].astype(str)

        _missing = {str(x).strip().lower() for x in (delete_with or [])} | {""}

        def _is_missing(cell: str) -> bool:
            return cell is None or cell.strip().lower() in _missing

        def _should_write(prod: str) -> bool:
            parts = [p.strip() for p in re.split(r"\s*" + re.escape(sep.strip()) + r"\s*", str(prod))]
            while len(parts) < 3:
                parts.append("")
            brand, product, category = parts[0], parts[1], parts[2]
            return not (_is_missing(brand) and _is_missing(product))

        filtered = updates[updates["Product"].apply(_should_write)].copy()
        if filtered.empty:
            return self._read_df_auto(self.history_path) if not _is_excel_path(self.history_path) else _read_history_df_excel(self.history_path)

        def _row_ext(row):
            fn = row.get("FileName", "")
            return ext_from_filename(fn)

        filtered["Ext"] = filtered.apply(_row_ext, axis=1)
        filtered["DateAdded"] = _now_date_str()
        filtered["Status"] = 1
        cols = ["Link", "DateAdded", "Status", "Ext", "Product"]
        filtered = filtered[[c for c in cols if c in filtered.columns]]

        return self.upsert_history(filtered)

    @staticmethod
    def build_run_report(links_df_full: pd.DataFrame, classified_products: pd.DataFrame) -> pd.DataFrame:
        if links_df_full is None or links_df_full.empty:
            return pd.DataFrame(columns=["Link", "Status", "Product"])

        df = links_df_full[["Link", "IsDownload"]].copy()
        df["IsDownload"] = pd.to_numeric(df["IsDownload"], errors="coerce").fillna(0).astype(int)
        df["Status"] = np.where(df["IsDownload"] == 200, 2, 0)
        df["Product"] = ""

        if classified_products is not None and not classified_products.empty:
            cp = classified_products.drop_duplicates(subset=["Link"], keep="last").copy()
            df = df.merge(cp[["Link", "Product"]], on="Link", how="left", suffixes=("", "_cp"))
            mask_classified = df["Product_cp"].notna() & (df["Product_cp"].astype(str).str.strip() != "")
            df.loc[mask_classified & (df["IsDownload"] == 200), "Status"] = 1
            df.loc[mask_classified, "Product"] = df.loc[mask_classified, "Product_cp"]
            df = df.drop(columns=["Product_cp"])

        return df[["Link", "Status", "Product"]].drop_duplicates(subset=["Link"], keep="last")

    @staticmethod
    def compute_quality_from_report(report_df: pd.DataFrame, sep: str = "||", delete_with: Tuple[str, ...] | None = ("-", "not found")) -> Dict[str, float]:
        """
        Рахує % правильно/неправильно класифікованих САМЕ ПО РЕПОРТУ:
        - беремо лише рядки зі Status == 1 як знаменник (denom);
        - 'правильно' = та ж умова, що й _should_write: Brand або Product не пусті/-/not found.
        """
        out = {"correct_count": 0, "incorrect_count": 0, "denom": 0, "correct_pct": 0.0, "incorrect_pct": 0.0}
        if report_df is None or report_df.empty:
            return out

        df = report_df.copy()
        s = pd.to_numeric(df.get("Status"), errors="coerce").fillna(-1).astype(int)
        df = df[s == 1]
        denom = int(len(df))
        out["denom"] = denom
        if denom == 0:
            return out

        prod_col = "Product" if "Product" in df.columns else None
        if not prod_col:
            return out

        missing = {str(x).strip().lower() for x in (delete_with or [])} | {""}

        def _is_missing(cell: str) -> bool:
            return cell is None or str(cell).strip().lower() in missing

        def _should_write_product(prod: str) -> bool:
            parts = [p.strip() for p in re.split(r"\s*" + re.escape(sep.strip()) + r"\s*", str(prod))]
            while len(parts) < 3:
                parts.append("")
            brand, product = parts[0], parts[1]
            return not (_is_missing(brand) and _is_missing(product))

        good = int(df[prod_col].apply(_should_write_product).sum())
        bad = denom - good
        out["correct_count"] = good
        out["incorrect_count"] = bad
        out["correct_pct"] = round(100.0 * good / denom, 2)
        out["incorrect_pct"] = round(100.0 * bad / denom, 2)
        return out

    def save_report(self, report_df: pd.DataFrame, path: str) -> None:
        self._write_df_auto(report_df, path)

    @staticmethod
    def _fmt_pct(val):
        if val is None:
            return ""
        if pd.isna(val):
            return ""
        if isinstance(val, str) and val.strip() == "":
            return ""
        try:
            v = float(val)
        except Exception:
            return str(val)
        return f"{v}".replace(".", ",")

    # -------- metrics sheet append --------
    def append_metrics_row(self, *, metrics: RunMetrics) -> None:
        row = metrics.to_dict()

        if _is_excel_path(self.history_path):
            _append_metrics_excel(self.history_path, row)
        else:
            _append_metrics_csv(self.history_path, row, sep=self.csv_sep)
