
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent  # ./Project
ADREAL = ROOT / "AdrealDownloadCreative"
ADSCLS = ROOT / "Ads_AI-Classification"

LINKS_TXT = ADREAL / "links.txt"
H_TXT = ADREAL / "h.txt"  # тут тримаємо видалені лінки
UPLOAD_RUNTIME_JSON = ADREAL / "runtime_upload.json" # файл для передачі метрик часу між скриптами
HISTORY = ADSCLS / "history.csv"


def _run_adreal_main() -> dict:
    """Запускає AdrealDownloadCreative/main.py як модуль і повертає runtime dict.

    Це прибирає потребу у тимчасових JSON-файлах між скриптами.
    """
    sys.path.insert(0, str(ADREAL))
    try:
        # main.py в директорії ADREAL
        import main as adreal_main  # type: ignore

        return adreal_main.run()
    finally:
        # прибираємо наш шлях, щоб не ламати імпорти далі в пайплайні
        try:
            sys.path.remove(str(ADREAL))
        except ValueError:
            pass


def _read_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8", errors="ignore")
    return [ln.strip() for ln in text.splitlines() if ln.strip()]


def _write_lines(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(lines)
    if lines:
        body += "\n"
    path.write_text(body, encoding="utf-8")


def _read_history_links(history_path: Path) -> set[str]:
    if not history_path.exists() or history_path.stat().st_size == 0:
        return set()

    try:
        df = pd.read_csv(history_path, sep=";", dtype=str)
    except Exception: # noqa
        df = pd.read_csv(history_path, sep=",", dtype=str)

    col = "Link" if "Link" in df.columns else df.columns[0]
    return set(df[col].astype(str).str.strip().tolist())


def _save_upload_runtime(
    *,
    total_sec: float,
    src_total: int,
    removed_count: int,
    kept_count: int,
    started_at: str,
    finished_at: str,
    downloaded_rows_total: int | None = None,
    downloaded_rows_200: int | None = None,
    downloaded_rows_non200: int | None = None,
    extra_metrics: dict | None = None,
) -> None:
    """Зберігає в JSON агреговані метрики вигрузки."""
    payload = {
        "upload_time_total_creatives_sec": float(total_sec),
        "started_at": started_at,
        "finished_at": finished_at,
        "links_total_before_filter": int(src_total),
        "links_removed_by_history": int(removed_count),
        "links_kept_for_download": int(kept_count),
    }

    if downloaded_rows_total is not None:
        payload["downloaded_rows_total"] = int(downloaded_rows_total)
    if downloaded_rows_200 is not None:
        payload["downloaded_rows_200"] = int(downloaded_rows_200)
    if downloaded_rows_non200 is not None:
        payload["downloaded_rows_non200"] = int(downloaded_rows_non200)

    if extra_metrics:
        # метрики з main.py (окремий час по відео/зображенням тощо)
        payload.update(extra_metrics)

    try:
        UPLOAD_RUNTIME_JSON.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as exc:
        print(f"[warn] Не вдалося записати runtime у {UPLOAD_RUNTIME_JSON}: {exc}", file=sys.stderr)



def main() -> None:
    start_wall = time.perf_counter()
    started_at = datetime.now().isoformat(timespec="seconds")

    src = _read_lines(LINKS_TXT)
    hist = _read_history_links(HISTORY)

    removed = [ln for ln in src if ln in hist]
    kept = [ln for ln in src if ln not in hist]

    _write_lines(H_TXT, removed)
    _write_lines(LINKS_TXT, kept)

    print(f"[filter] total={len(src)} removed={len(removed)} kept={len(kept)}")

    extra_metrics = _run_adreal_main()

    output_path = ADREAL / "output.txt"
    downloaded_rows_total = downloaded_rows_200 = downloaded_rows_non200 = None
    if output_path.exists():
        try:
            df_out = pd.read_csv(output_path, dtype=str)
            df_out["status"] = pd.to_numeric(df_out.get("status"), errors="coerce").fillna(0).astype(int)
            downloaded_rows_total = len(df_out)
            downloaded_rows_200 = int((df_out["status"] == 200).sum())
            downloaded_rows_non200 = int((df_out["status"] != 200).sum())
        except Exception as e:
            print(f"[warn] Не вдалося прочитати {output_path} для runtime-метрик: {e}", file=sys.stderr)

    finished_at = datetime.now().isoformat(timespec="seconds")
    total_sec = time.perf_counter() - start_wall

    _save_upload_runtime(
        total_sec=total_sec,
        src_total=len(src),
        removed_count=len(removed),
        kept_count=len(kept),
        started_at=started_at,
        finished_at=finished_at,
        downloaded_rows_total=downloaded_rows_total,
        downloaded_rows_200=downloaded_rows_200,
        downloaded_rows_non200=downloaded_rows_non200,
        extra_metrics=extra_metrics,
    )


if __name__ == "__main__":
    main()
