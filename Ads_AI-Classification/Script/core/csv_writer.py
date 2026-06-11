import os
import threading
from typing import List

import pandas as pd


class IncrementalCSVWriter:
    """Thread-safe incremental CSV writer (safe for concurrent threads)."""

    def __init__(self, path: str, header: List[str]):
        self.path = path
        self.header = list(header)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

        self._lock = threading.Lock()
        self._initialized = os.path.exists(path) and os.path.getsize(path) > 0

    def _refresh_initialized(self) -> None:
        """Re-check file state (must be called under lock)."""
        self._initialized = os.path.exists(self.path) and os.path.getsize(self.path) > 0

    def append_rows(self, df: pd.DataFrame) -> None:
        if df is None or df.empty:
            return

        safe_df = df.reindex(columns=self.header)

        with self._lock:
            self._refresh_initialized()

            mode = "a" if self._initialized else "w"
            write_header = not self._initialized

            safe_df.to_csv(
                self.path,
                mode=mode,
                header=write_header,
                index=False,
                encoding="utf-8-sig",
                sep=";",
            )
            self._initialized = True
