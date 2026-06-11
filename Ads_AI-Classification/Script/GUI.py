
import os
import re
import sys
import threading
from datetime import datetime, date
from calendar import monthrange
from pathlib import Path
from typing import Optional, List, Dict, Any, Callable
import traceback

import customtkinter as ctk
from tkinter import filedialog, messagebox

from Script.APP import main as app_main
from Script.config import APP_CONFIG
from Script.utils.metrics import RunMetrics

# ------------------ утиліти пошуку шляхів ------------------
DATE_RE = re.compile(r"^Creatives_(\d{4}-\d{2}-\d{2})$")


def find_latest_creatives_dir(root_dir: Path) -> Optional[Path]:
    if not root_dir or not root_dir.exists():
        return None
    candidates = []
    for child in root_dir.iterdir():
        if child.is_dir():
            m = DATE_RE.match(child.name)
            if m:
                try:
                    dt = datetime.strptime(m.group(1), "%Y-%m-%d")
                    candidates.append((dt, child))
                except ValueError:
                    pass
    if not candidates:
        return None
    candidates.sort(key=lambda t: t[0], reverse=True)
    return candidates[0][1]


def find_output_txt(root_dir: Path, prefer_dir: Optional[Path] = None) -> Optional[Path]:
    p = root_dir / "output.txt"
    if p.exists() and p.is_file():
        return p
    if prefer_dir:
        p = prefer_dir / "output.txt"
        if p.exists() and p.is_file():
            return p
    max_depth = len(root_dir.parts) + 3
    for current, _, files in os.walk(root_dir):
        if len(Path(current).parts) > max_depth:
            continue
        if "output.txt" in files:
            return Path(current) / "output.txt"
    return None


# ------------------ Collapsible ------------------
class CollapsibleFrame(ctk.CTkFrame):
    def __init__(self, master, title: str = "Деталі", *args, **kwargs):
        super().__init__(master, *args, **kwargs)
        self._title = title
        self._content_visible = False
        self._header = ctk.CTkButton(self, text=f"▸ {title}", command=self._toggle, anchor="w")
        self._header.pack(fill="x", padx=8, pady=(8, 4))
        self._container = ctk.CTkScrollableFrame(self, height=260)
        self._container.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self._container.pack_forget()

    def content(self):
        return self._container

    def set_title(self, title: str) -> None:
        self._title = title
        tri = "▾" if self._content_visible else "▸"
        self._header.configure(text=f"{tri} {title}")

    def _toggle(self) -> None:
        self._content_visible = not self._content_visible
        if self._content_visible:
            self._header.configure(text=f"▾ {self._title}")
            self._container.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        else:
            self._header.configure(text=f"▸ {self._title}")
            self._container.pack_forget()


# ------------------ МЕТРИКИ як вбудований фрейм ------------------
class MetricsView(ctk.CTkFrame):
    def __init__(self, master, on_next_stage: Callable[[], None], on_finish: Callable[[], None]):
        super().__init__(master)
        self._on_next_stage = on_next_stage
        self._on_finish = on_finish

        self.result: Dict[str, Any] = {}
        self.metrics: RunMetrics | None = None
        self.stage_idx: int = 0
        self.stages: List[Dict[str, Any]] = []
        self.run_history: List[Dict[str, Any]] = []
        self.report_path: Optional[str] = None

        # layout
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(4, weight=1)

        # header
        self._header_label = ctk.CTkLabel(self, text="Завершено", font=("Segoe UI", 20, "bold"))
        self._header_label.grid(row=0, column=0, sticky="w", padx=16, pady=(16, 6))

        self._dynamic_widgets: List[ctk.CTkBaseClass] = []

        # --- стан і контейнер для деталей прогону ---
        self._details_visible: bool = False

        # кнопка-тогл
        self._details_toggle_btn = ctk.CTkButton(self, text="Показати деталі прогону ▼", command=self._toggle_details, fg_color="transparent", border_width=1)
        self._details_toggle_btn.grid(row=2, column=0, sticky="w", padx=16, pady=(0, 4))

        # фрейм, в якому лежить скролюваний контент деталей
        self._details_outer = ctk.CTkFrame(self)
        # рядок з фіксованою висотою (scrollableFrame height)
        self._details_outer.grid(row=3, column=0, sticky="nsew", padx=0, pady=(0, 0))
        self._details_outer.grid_remove()  # спочатку прихований

        # скролюваний фрейм всередині, висота фіксована
        self._details_scroll = ctk.CTkScrollableFrame(self._details_outer, height=260, label_text="Деталі прогону", label_font=("Segoe UI", 16, "bold"))
        self._details_scroll.pack(fill="both", expand=True, padx=16, pady=(4, 8))

    def refresh(self, *, result: Dict[str, Any], stage_idx: int, stages: List[Dict[str, Any]], run_history: List[Dict[str, Any]]) -> None:
        self.result = result or {}

        rm = self.result.get("metrics")
        self.metrics = rm if isinstance(rm, RunMetrics) else None

        self.stage_idx = stage_idx
        self.stages = stages
        self.run_history = run_history or []
        self.report_path = self.result.get("report_path")

        stage_name = self.stages[self.stage_idx].get("name", f"Етап {self.stage_idx + 1}")
        self._header_label.configure(text=f"Завершено: {stage_name}")

        self._clear_dynamic()
        self._build_summary_section()

        if self._details_visible:
            self._rebuild_details_content()

        self._build_improvements_and_history_section()
        self._build_buttons_section()

    def _clear_dynamic(self) -> None:
        for w in self._dynamic_widgets:
            try: w.destroy()
            except Exception: # noqa
                pass
        self._dynamic_widgets.clear()

    def _build_summary_section(self) -> None:
        summary = ctk.CTkFrame(self)
        summary.grid(row=1, column=0, sticky="ew", padx=16, pady=8)
        self._dynamic_widgets.append(summary)

        metrics = self.metrics
        run_history = self.run_history

        if not isinstance(metrics, RunMetrics):
            msg = (
                "Метрики цього прогону недоступні.\n"
                "Швидше за все, класифікація завершилась з помилкою - дивись текст помилки в логах вище."
            )
            ctk.CTkLabel(summary, text=msg, font=("Segoe UI", 13), justify="left").grid(
                row=0, column=0, sticky="w", padx=10, pady=4
            )
            return

        def _fmt_pct(x: float) -> str:
            try:
                return f"{float(x):.2f}%"
            except Exception:  # noqa
                return "0.00%"

        def _safe_div(num: float, den: float) -> float:
            num = float(num or 0.0)
            den = float(den or 0.0)
            if den <= 0.0:
                return 0.0
            return (num / den) * 100.0

        def row(
                frame: ctk.CTkBaseClass,
                label: str,
                value: object,
                r: int,
                c: int,
                bold: bool = False,
        ) -> None:
            font = ("Segoe UI", 12, "bold") if bold else ("Segoe UI", 12)
            ctk.CTkLabel(frame, text=label, font=("Segoe UI", 12)).grid(
                row=r, column=c, sticky="w", padx=8, pady=2
            )
            ctk.CTkLabel(frame, text=str(value), font=font).grid(
                row=r, column=c + 1, sticky="w", padx=8, pady=2
            )

        # 1) total cost (по всіх прогонах)
        all_runs_usd = 0.0
        try:
            for item in run_history or []:
                mm = (item or {}).get("metrics")
                if isinstance(mm, RunMetrics):
                    all_runs_usd += float(mm.total_cost_usd or 0.0)
        except Exception:  # noqa
            pass
        row(summary, "Витрати (USD, всі етапи)", f"${all_runs_usd:.6f}", 0, 0)

        # 2) identified
        row(
            summary,
            "ідентифіковано креативів (IsDownload==200)",
            f"{metrics.identified_all} ({_fmt_pct(metrics.identified_all_pct_of_all)} від усіх)",
            0,
            2,
        )

        identified_all = int(metrics.identified_all or 0)
        hist_taken_all = int(metrics.hist_taken_all or 0)
        classified_api_all = int(metrics.classified_api_all or 0)

        # 3) correctly classified (під історію)
        row(
            summary,
            "правильно класифіковані",
            f"{int(metrics.classified_correct_all or 0)} ({_fmt_pct(metrics.classified_correct_all_pct_of_identified)} від ідентиф.)",
            1,
            0,
            bold=True,
        )

        # 4) taken from history (як було)
        row(
            summary,
            "взято з історії",
            f"{hist_taken_all} ({_fmt_pct(metrics.hist_taken_pct_of_pool)} від правильно класифікованих)",
            1,
            2,
            bold=False,
        )

        # 5) processed (фактично оброблялися = пройшли через API класифікації)
        row(
            summary,
            "фактично оброблялися (через API)",
            f"{classified_api_all} ({_fmt_pct(_safe_div(classified_api_all, identified_all))} від ідентиф.)",
            2,
            0,
            bold=True,
        )

        # 6) duplicates (від тих що оброблялися)
        dups_all = int(getattr(metrics, "img_dups", 0) or 0) + int(getattr(metrics, "vid_dups", 0) or 0)
        row(
            summary,
            "дублікатів",
            f"{dups_all} ({_fmt_pct(_safe_div(dups_all, classified_api_all))} від обробл.)",
            2,
            2,
            bold=False,
        )

        # 7) broken (від тих що оброблялися)
        broken_all = metrics.classify_broken_creatives_all
        row(
            summary,
            "пошкоджених",
            f"{broken_all} ({_fmt_pct(_safe_div(broken_all, identified_all))} від ідентиф.)",
            3,
            0,
            bold=False,
        )

        for i in range(4):
            summary.grid_columnconfigure(i, weight=1)

    def _build_details_section(self, parent: ctk.CTkBaseClass) -> None:
        """Деталі прогону: усі таблички з RunMetrics, рендеряться в заданий parent."""
        m = self.metrics

        if not isinstance(m, RunMetrics):
            ctk.CTkLabel(
                parent,
                text="Детальні метрики цього прогону недоступні.",
                font=("Segoe UI", 12),
            ).pack(anchor="w", padx=4, pady=(4, 8))
            return

        def _fmt_pct(x: float) -> str:
            try:
                return f"{float(x):.2f}%"
            except Exception: # noqa
                return "0.00%"

        def _cell(frame, r: int, c: int, label: str, value: object) -> None:
            ctk.CTkLabel(frame, text=label, font=("Segoe UI", 12)).grid(
                row=r, column=c, sticky="w", padx=8, pady=2
            )
            ctk.CTkLabel(frame, text=str(value), font=("Segoe UI", 12, "bold")).grid(
                row=r, column=c + 1, sticky="w", padx=8, pady=2
            )

        # =====================================================================
        # 0) DEBUG / TECH (тільки поля які реально існують в RunMetrics)
        # =====================================================================
        tech_frame = ctk.CTkFrame(parent)
        tech_frame.pack(fill="x", padx=0, pady=(4, 4))

        ctk.CTkLabel(
            tech_frame,
            text="Технічні змінні (debug)",
            font=("Segoe UI", 13, "bold"),
        ).grid(row=0, column=0, columnspan=4, sticky="w", padx=8, pady=(6, 2))

        row = 1
        _cell(tech_frame, row, 0, "API rows (img)", m.img_api_rows)
        _cell(tech_frame, row, 2, "API rows (video)", m.vid_api_rows)
        row += 1

        _cell(tech_frame, row, 0, "Дублікатів img", m.img_dups)
        _cell(tech_frame, row, 2, "Дублікатів video", m.vid_dups)
        row += 1

        _cell(tech_frame, row, 0, "Broken banner (pipeline)", m.classify_broken_banner_all)
        _cell(tech_frame, row, 2, "Broken video (pipeline)", m.classify_broken_video_all)
        row += 1

        _cell(tech_frame, row, 0, "Broken creatives (all)", m.classify_broken_creatives_all)
        _cell(tech_frame, row, 2, "Broken links (unique)", m.broken_links_unique)

        for cc in range(4):
            tech_frame.grid_columnconfigure(cc, weight=1)

        # =====================================================================
        # 1) TOTAL + IDENTIFIED
        # =====================================================================
        totals_frame = ctk.CTkFrame(parent)
        totals_frame.pack(fill="x", padx=0, pady=(4, 4))

        ctk.CTkLabel(
            totals_frame, text="Тотал змінні (лінки)", font=("Segoe UI", 13, "bold")
        ).grid(row=0, column=0, columnspan=4, sticky="w", padx=8, pady=(6, 2))

        row = 1
        _cell(totals_frame, row, 0, "Загальна кількість усіх креативів", m.total_links_all)
        _cell(totals_frame, row, 2, "Загальна кількість унікальних креативів", m.total_links_unique)
        row += 1

        _cell(
            totals_frame,
            row,
            0,
            "Ідентифіковані (усі)",
            f"{m.identified_all} ({_fmt_pct(m.identified_all_pct_of_all)} від усіх)",
        )
        _cell(
            totals_frame,
            row,
            2,
            "Ідентифіковані (унікальні)",
            f"{m.identified_unique} ({_fmt_pct(m.identified_unique_pct_of_unique)} від унікальних)",
        )
        row += 1

        _cell(
            totals_frame,
            row,
            0,
            "Ідентифіковані відео (усі)",
            f"{m.identified_video_all} ({_fmt_pct(m.identified_video_all_pct_of_identified)} від ідентиф.)",
        )
        _cell(
            totals_frame,
            row,
            2,
            "Ідентифіковані банери (усі)",
            f"{m.identified_banner_all} ({_fmt_pct(m.identified_banner_all_pct_of_identified)} від ідентиф.)",
        )
        row += 1

        _cell(
            totals_frame,
            row,
            0,
            "Ідентифіковані відео (унікальні)",
            f"{m.identified_video_unique} ({_fmt_pct(m.identified_video_unique_pct_of_identified_unique)} від ідентиф. унік.)",
        )
        _cell(
            totals_frame,
            row,
            2,
            "Ідентифіковані банери (унікальні)",
            f"{m.identified_banner_unique} ({_fmt_pct(m.identified_banner_unique_pct_of_identified_unique)} від ідентиф. унік.)",
        )
        row += 1

        _cell(totals_frame, row, 0, "Пошкоджені лінки (усі)", m.broken_links_all)
        _cell(totals_frame, row, 2, "Пошкоджені лінки (унікальні)", m.broken_links_unique)

        for cc in range(4):
            totals_frame.grid_columnconfigure(cc, weight=1)

        # =====================================================================
        # 2) CLASSIFIED VIA API
        # =====================================================================
        api_frame = ctk.CTkFrame(parent)
        api_frame.pack(fill="x", padx=0, pady=(4, 4))

        ctk.CTkLabel(
            api_frame, text="Класифіковані через API", font=("Segoe UI", 13, "bold")
        ).grid(row=0, column=0, columnspan=4, sticky="w", padx=8, pady=(6, 2))

        row = 1
        _cell(
            api_frame,
            row,
            0,
            "Класифіковані через API (усі)",
            f"{m.classified_api_all} ({_fmt_pct(m.classified_api_all_pct_of_identified)} від ідентиф.)",
        )
        _cell(
            api_frame,
            row,
            2,
            "Класифіковані через API (унікальні)",
            f"{m.classified_api_unique} ({_fmt_pct(m.classified_api_unique_pct_of_identified_unique)} від ідентиф. унік.)",
        )
        row += 1

        _cell(
            api_frame,
            row,
            0,
            "API відео (усі)",
            f"{m.classified_api_video_all} ({_fmt_pct(m.classified_api_video_all_pct_of_identified_video)} від ідентиф. відео)",
        )
        _cell(
            api_frame,
            row,
            2,
            "API відео (унікальні)",
            f"{m.classified_api_video_unique} ({_fmt_pct(m.classified_api_video_unique_pct_of_identified_video_unique)} від ідентиф. відео унік.)",
        )
        row += 1

        _cell(
            api_frame,
            row,
            0,
            "API банери (усі)",
            f"{m.classified_api_banner_all} ({_fmt_pct(m.classified_api_banner_all_pct_of_identified_banner)} від ідентиф. банерів)",
        )
        _cell(
            api_frame,
            row,
            2,
            "API банери (унікальні)",
            f"{m.classified_api_banner_unique} ({_fmt_pct(m.classified_api_banner_unique_pct_of_identified_banner_unique)} від ідентиф. банерів унік.)",
        )

        for cc in range(4):
            api_frame.grid_columnconfigure(cc, weight=1)

        # =====================================================================
        # 3) CORRECTLY CLASSIFIED (for history)
        # =====================================================================
        correct_frame = ctk.CTkFrame(parent)
        correct_frame.pack(fill="x", padx=0, pady=(4, 4))

        ctk.CTkLabel(
            correct_frame, text="Правильно класифіковані (під історію)", font=("Segoe UI", 13, "bold")
        ).grid(row=0, column=0, columnspan=4, sticky="w", padx=8, pady=(6, 2))

        row = 1
        _cell(
            correct_frame,
            row,
            0,
            "Правильно класифіковані (усі)",
            f"{m.classified_correct_all} ({_fmt_pct(m.classified_correct_all_pct_of_identified)} від ідентиф.)",
        )
        _cell(
            correct_frame,
            row,
            2,
            "Правильно класифіковані (унікальні)",
            f"{m.classified_correct_unique} ({_fmt_pct(m.classified_correct_unique_pct_of_identified_unique)} від ідентиф. унік.)",
        )
        row += 1

        _cell(
            correct_frame,
            row,
            0,
            "Правильно відео (усі)",
            f"{m.classified_correct_video_all} ({_fmt_pct(m.classified_correct_video_all_pct_of_identified_video)} від ідентиф. відео)",
        )
        _cell(
            correct_frame,
            row,
            2,
            "Правильно відео (унікальні)",
            f"{m.classified_correct_video_unique} ({_fmt_pct(m.classified_correct_video_unique_pct_of_identified_video_unique)} від ідентиф. відео унік.)",
        )
        row += 1

        _cell(
            correct_frame,
            row,
            0,
            "Правильно банери (усі)",
            f"{m.classified_correct_banner_all} ({_fmt_pct(m.classified_correct_banner_all_pct_of_identified_banner)} від ідентиф. банерів)",
        )
        _cell(
            correct_frame,
            row,
            2,
            "Правильно банери (унікальні)",
            f"{m.classified_correct_banner_unique} ({_fmt_pct(m.classified_correct_banner_unique_pct_of_identified_banner_unique)} від ідентиф. банерів унік.)",
        )

        for cc in range(4):
            correct_frame.grid_columnconfigure(cc, weight=1)

        # =====================================================================
        # 4) HISTORY TAKEN BEFORE API
        # =====================================================================
        hist_frame = ctk.CTkFrame(parent)
        hist_frame.pack(fill="x", padx=0, pady=(4, 4))

        ctk.CTkLabel(
            hist_frame, text="Історичні змінні (взято ДО API)", font=("Segoe UI", 13, "bold")
        ).grid(row=0, column=0, columnspan=4, sticky="w", padx=8, pady=(6, 2))

        row = 1
        _cell(hist_frame, row, 0, "Взято з історії (усі)", f"{m.hist_taken_all} ({_fmt_pct(m.hist_taken_pct_of_pool)} від правильно класифікованих)")
        row += 1

        _cell(hist_frame, row, 0, "Взято з історії відео (усі)", f"{m.hist_taken_video_all} ({_fmt_pct(m.hist_taken_video_pct_of_pool)} від правильно класифікованих)")
        _cell(hist_frame, row, 2, "Взято з історії банери (усі)", f"{m.hist_taken_banner_all} ({_fmt_pct(m.hist_taken_banner_pct_of_pool)} від правильно класифікованих)")

        for cc in range(4):
            hist_frame.grid_columnconfigure(cc, weight=1)

        # =====================================================================
        # 5) TIME (UPLOAD vs CLASSIFY) – з breakdown по video/banner
        # =====================================================================
        time_frame = ctk.CTkFrame(parent)
        time_frame.pack(fill="x", padx=0, pady=(4, 4))

        ctk.CTkLabel(
            time_frame, text="Часові змінні", font=("Segoe UI", 13, "bold")
        ).grid(row=0, column=0, columnspan=4, sticky="w", padx=8, pady=(6, 2))

        ctk.CTkLabel(time_frame, text="Вивантажка (сек)", font=("Segoe UI", 12, "bold")).grid(
            row=1, column=0, columnspan=2, sticky="w", padx=8, pady=(0, 2)
        )
        ctk.CTkLabel(time_frame, text="Класифікація (сек)", font=("Segoe UI", 12, "bold")).grid(
            row=1, column=2, columnspan=2, sticky="w", padx=8, pady=(0, 2)
        )

        def _cell_time(r: int, c: int, label: str, value: object) -> None:
            ctk.CTkLabel(time_frame, text=label, font=("Segoe UI", 12)).grid(
                row=r, column=c, sticky="w", padx=8, pady=2
            )
            ctk.CTkLabel(time_frame, text=str(value), font=("Segoe UI", 12, "bold")).grid(
                row=r, column=c + 1, sticky="w", padx=8, pady=2
            )

        row = 2
        _cell_time(row, 0, "Загальний час на вивантажку креативів", f"{m.upload_time_total_creatives_sec:.2f}s")
        _cell_time(row, 2, "Загальний час на класифікацію креативів", f"{m.classify_time_total_creatives_sec:.2f}s")
        row += 1

        _cell_time(row, 0, "Середній час на вивантажку креативів", f"{m.upload_time_avg_creative_sec:.3f}s")
        _cell_time(row, 2, "Середній час на класифікацію креативів", f"{m.classify_time_avg_creative_sec:.3f}s")
        row += 1

        _cell_time(row, 0, "Загальний час на вивантажку відео", f"{m.upload_time_total_video_sec:.2f}s")
        _cell_time(row, 2, "Загальний час на класифікацію відео", f"{m.classify_time_total_video_sec:.2f}s")
        row += 1

        _cell_time(row, 0, "Середній час на вивантажку відео", f"{m.upload_time_avg_video_sec:.3f}s")
        _cell_time(row, 2, "Середній час на класифікацію відео", f"{m.classify_time_avg_video_sec:.3f}s")
        row += 1

        _cell_time(row, 0, "Загальний час на вивантажку банерів", f"{m.upload_time_total_banner_sec:.2f}s")
        _cell_time(row, 2, "Загальний час на класифікацію банерів", f"{m.classify_time_total_banner_sec:.2f}s")
        row += 1

        _cell_time(row, 0, "Середній час на вивантажку банерів", f"{m.upload_time_avg_banner_sec:.3f}s")
        _cell_time(row, 2, "Середній час на класифікацію банерів", f"{m.classify_time_avg_banner_sec:.3f}s")

        for cc in range(4):
            time_frame.grid_columnconfigure(cc, weight=1)

        # =====================================================================
        # 6) PIPELINE TIME
        # =====================================================================
        pipeline_frame = ctk.CTkFrame(parent)
        pipeline_frame.pack(fill="x", padx=0, pady=(4, 4))

        ctk.CTkLabel(
            pipeline_frame,
            text="Час пайплайнів (додаткові часові змінні)",
            font=("Segoe UI", 13, "bold"),
        ).grid(row=0, column=0, columnspan=4, sticky="w", padx=8, pady=(6, 2))

        def _cell_pipe(r: int, c: int, label: str, value: object) -> None:
            ctk.CTkLabel(pipeline_frame, text=label, font=("Segoe UI", 12)).grid(
                row=r, column=c, sticky="w", padx=8, pady=2
            )
            ctk.CTkLabel(pipeline_frame, text=str(value), font=("Segoe UI", 12, "bold")).grid(
                row=r, column=c + 1, sticky="w", padx=8, pady=2
            )

        row = 1
        _cell_pipe(row, 0, "Pipeline (усі креативи, сумарно)", f"{m.pipeline_time_total_creatives_sec:.2f}s")
        _cell_pipe(row, 2, "Pipeline (на 1 креатив)", f"{m.pipeline_time_avg_creative_sec:.3f}s")
        row += 1

        _cell_pipe(row, 0, "Pipeline відео (сумарно)", f"{m.pipeline_time_total_video_sec:.2f}s")
        _cell_pipe(row, 2, "Pipeline відео (на 1 відео)", f"{m.pipeline_time_avg_video_sec:.3f}s")
        row += 1

        _cell_pipe(row, 0, "Pipeline банери (сумарно)", f"{m.pipeline_time_total_banner_sec:.2f}s")
        _cell_pipe(row, 2, "Pipeline банери (на 1 банер)", f"{m.pipeline_time_avg_banner_sec:.3f}s")

        for cc in range(4):
            pipeline_frame.grid_columnconfigure(cc, weight=1)

        # =====================================================================
        # 7) MONEY
        # =====================================================================
        cost_frame = ctk.CTkFrame(parent)
        cost_frame.pack(fill="x", padx=0, pady=(4, 12))

        ctk.CTkLabel(cost_frame, text="Грошові змінні", font=("Segoe UI", 13, "bold")).grid(
            row=0, column=0, columnspan=4, sticky="w", padx=8, pady=(6, 2)
        )

        row = 1
        _cell(cost_frame, row, 0, "Total creatives cost (USD)", f"${m.total_cost_usd:.6f}")
        row += 1
        _cell(cost_frame, row, 0, "Video cost (USD)", f"${m.vid_cost_usd:.6f}")
        row += 1
        _cell(cost_frame, row, 0, "Banner cost (USD)", f"${m.img_cost_usd:.6f}")

        for cc in range(4):
            cost_frame.grid_columnconfigure(cc, weight=1)

    def _toggle_details(self) -> None:
        """Показати / сховати блок деталей прогону."""
        self._details_visible = not self._details_visible

        if self._details_visible:
            # показати контейнер
            self._details_outer.grid()
            self._details_toggle_btn.configure(text="Сховати деталі прогону ▲")
            # перебудувати контент
            self._rebuild_details_content()
        else:
            # сховати контейнер
            self._details_outer.grid_remove()
            self._details_toggle_btn.configure(text="Показати деталі прогону ▼")

    def _rebuild_details_content(self) -> None:
        """Очистити скролюваний фрейм та побудувати туди деталі метрик."""
        # прибираємо старий вміст
        for child in self._details_scroll.winfo_children():
            child.destroy()

        # якщо метрик немає – показуємо повідомлення
        if not isinstance(self.metrics, RunMetrics):
            ctk.CTkLabel(
                self._details_scroll,
                text="Детальні метрики цього прогону недоступні.\nЙмовірно, класифікація завершилась з помилкою = дивись лог вище.",
                font=("Segoe UI", 12),
                justify="left",
            ).pack(anchor="w", padx=4, pady=(4, 8))
            return

        # будуємо деталі у скролюваний фрейм
        self._build_details_section(parent=self._details_scroll)

    def _build_improvements_and_history_section(self) -> None:
        area = ctk.CTkScrollableFrame(self)
        area.grid(row=4, column=0, sticky="nsew", padx=16, pady=8)
        self._dynamic_widgets.append(area)

        def _extract_metrics(item) -> RunMetrics | None:
            if not item:
                return None
            mm = item.get("metrics")
            return mm if isinstance(mm, RunMetrics) else None

        def _safe_div(num: float, den: float) -> float:
            num = float(num or 0.0)
            den = float(den or 0.0)
            if den <= 0.0:
                return 0.0
            return (num / den) * 100.0

        def _delta_int(prev: int, curr: int) -> str:
            prev = int(prev or 0)
            curr = int(curr or 0)
            diff = curr - prev

            if prev <= 0:
                sign = "+" if diff >= 0 else ""
                return f"{sign}{diff} (н/д)"

            pct = (diff / prev) * 100.0
            sign = "+" if diff >= 0 else ""
            return f"{sign}{diff} ({sign}{pct:.2f}%)"

        # --- Покращення ---
        ctk.CTkLabel(area, text="Покращення", font=("Segoe UI", 16, "bold")).pack(
            anchor="w", padx=12, pady=(4, 6)
        )

        if len(self.run_history) < 2:
            ctk.CTkLabel(
                area,
                text="Недостатньо історії, щоб порахувати приріст (потрібно ≥2 етапи).",
                font=("Segoe UI", 12),
            ).pack(anchor="w", padx=12, pady=(0, 8))
        else:
            impr_frame = ctk.CTkFrame(area)
            impr_frame.pack(fill="x", padx=12, pady=(0, 8))

            try:
                for i in range(1, len(self.run_history)):
                    prev_m = _extract_metrics(self.run_history[i - 1] or {})
                    curr_m = _extract_metrics(self.run_history[i] or {})

                    prev_correct_all = prev_m.classified_correct_all if prev_m else 0
                    curr_correct_all = curr_m.classified_correct_all if curr_m else 0

                    lbl = (
                        f"Етап {i} → Етап {i + 1}: "
                        f"Δ Correct(all) = {_delta_int(prev_correct_all, curr_correct_all)}; "
                    )
                    ctk.CTkLabel(impr_frame, text=lbl, font=("Segoe UI", 12)).pack(anchor="w", padx=8, pady=2)
            except Exception:  # noqa
                traceback.print_exc()

        # --- Історія ---
        ctk.CTkLabel(area, text="Історія", font=("Segoe UI", 16, "bold")).pack(
            anchor="w", padx=12, pady=(8, 6)
        )

        for orig_idx in range(1, len(self.run_history) + 1):
            item = self.run_history[orig_idx - 1] or {}
            m = _extract_metrics(item)
            if m is None:
                continue

            name = self.stages[orig_idx - 1]["name"] if orig_idx - 1 < len(self.stages) else f"Етап {orig_idx}"
            is_current = orig_idx == len(self.run_history)

            card = ctk.CTkFrame(area, fg_color=("gray90" if not is_current else None))
            card.pack(fill="x", padx=8, pady=6)

            ctk.CTkLabel(
                card,
                text=f"{name}{'  (current)' if is_current else ''}",
                font=("Segoe UI", 13, "bold"),
            ).grid(row=0, column=0, sticky="w", padx=10, pady=(8, 2))

            def cell(r: int, c: int, label: str, value: object) -> None:
                ctk.CTkLabel(card, text=label, font=("Segoe UI", 12)).grid(
                    row=r, column=c, sticky="w", padx=10, pady=3
                )
                ctk.CTkLabel(card, text=str(value), font=("Segoe UI", 12, "bold")).grid(
                    row=r, column=c + 1, sticky="w", padx=10, pady=3
                )

            r = 1

            identified_all = int(m.identified_all or 0)
            hist_taken_all = int(m.hist_taken_all or 0)
            classified_api_all = int(m.classified_api_all or 0)
            dups_all = int(m.img_dups or 0) + int(m.vid_dups or 0)
            broken_all = int(m.classify_broken_creatives_all or 0)

            cell(r, 0, "кост (USD)", f"${m.total_cost_usd:.6f}")
            cell(r, 2, "ідентифіковано", identified_all)
            r += 1

            cell(
                r,
                0,
                "правильно класифіковані",
                f"{int(m.classified_correct_all or 0)} ({m.classified_correct_all_pct_of_identified:.2f}% від ідентиф.)",
            )

            cell(r, 2, "взято з історії", f"{hist_taken_all} ({m.hist_taken_pct_of_pool:.2f}% від правильно класифікованих)")
            r += 1

            cell(r, 0, "фактично оброблялися", f"{classified_api_all} ({_safe_div(classified_api_all, identified_all):.2f}% від ідентиф.)")
            cell(r, 2, "дублікатів", f"{dups_all} ({_safe_div(dups_all, classified_api_all):.2f}% від обробл.)")
            r += 1

            cell(r, 0, "пошкоджених", f"{broken_all} ({_safe_div(broken_all, identified_all):.2f}% від ідентиф.)")

            for col in range(4):
                card.grid_columnconfigure(col, weight=1)

    def _build_buttons_section(self) -> None:
        btns = ctk.CTkFrame(self)
        btns.grid(row=5, column=0, sticky="ew", padx=16, pady=(8, 16))
        self._dynamic_widgets.append(btns)

        if self.report_path and os.path.exists(self.report_path):
            def open_report():
                try:
                    if os.name == "nt":
                        os.startfile(self.report_path)  # type: ignore[attr-defined]
                    elif sys.platform == "darwin":
                        os.system(f'open "{self.report_path}"')
                    else:
                        os.system(f'xdg-open "{self.report_path}"')
                except Exception as e:
                    messagebox.showerror("Помилка", f"Не вдалося відкрити звіт:\n{e}")
            ctk.CTkButton(btns, text="Відкрити звіт", command=open_report).pack(side="left", padx=(0, 12))

        if self.stage_idx + 1 < len(self.stages):
            next_stage = self.stages[self.stage_idx + 1]
            caption = f"Запустити {next_stage['name']} (frames={next_stage['number_of_frames']}, batch={next_stage['video_batch_size']})"
            ctk.CTkButton(btns, text=caption, command=self._on_next_stage).pack(side="left", padx=(0, 12))

        ctk.CTkButton(btns, text="Завершити", command=self._on_finish).pack(side="left")


# ------------------ головний застосунок ------------------
class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("Обробка креативів")
        self.geometry("1000x900")
        ctk.set_appearance_mode("light")
        ctk.set_default_color_theme("blue")

        # етапи пайплайна
        self.stages: List[Dict[str, Any]] = [
            {"name": "Етап 1 — легкий",   "number_of_frames": 2, "video_batch_size": 5},
            {"name": "Етап 2 — середній", "number_of_frames": 3, "video_batch_size": 3},
            {"name": "Етап 3 — важкий",   "number_of_frames": 5, "video_batch_size": 2},
        ]
        self.stage_idx: int = 0
        self.run_history: List[Dict[str, Any]] = []
        self.last_report_path: Optional[str] = None

        # дані вибору
        self.root_dir_var = ctk.StringVar()
        self.creatives_dir_var = ctk.StringVar()
        self.output_txt_var = ctk.StringVar()

        # тип/дата репорту
        self.report_type_var = ctk.StringVar(value="weekly")  # weekly | monthly | yearly
        today = date.today()
        self.month_var = ctk.StringVar(value=self._month_short(today.month))
        self.day_var = ctk.StringVar(value=str(today.day))
        self.year_var = ctk.StringVar(value=str(today.year))

        # вьюхи (frames)
        self.header = ctk.CTkLabel(self, text="Обробка креативів", font=("Segoe UI", 22, "bold"))
        self.header.pack(pady=(16, 8))
        self.page_area = ctk.CTkFrame(self)
        self.page_area.pack(fill="both", expand=True, padx=16, pady=(0, 16))
        self.page_area.grid_columnconfigure(0, weight=1)
        self.page_area.grid_rowconfigure(0, weight=1)

        self.view_select = self._build_select_view(self.page_area)
        self.view_logs = self._build_logs_view(self.page_area)
        self.view_metrics = self._build_metrics_view(self.page_area)

        self._show_view("select")

        default_root = APP_CONFIG.get("paths", {}).get("adreal_project_dir", "")
        if default_root:
            p = Path(default_root).resolve()
            if p.exists():
                self.root_dir_var.set(str(p))
                self.auto_detect()

    # ---------- Helpers ----------
    @staticmethod
    def _month_short(m: int) -> str:
        months = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
        return months[max(1, min(12, m)) - 1]

    @staticmethod
    def _month_to_num(ms: str) -> int:
        mapping = {n:i+1 for i,n in enumerate(["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"])}
        return mapping.get(ms, 1)

    def _report_date_iso(self) -> Optional[str]:
        """YYYY-MM-DD з валідацією."""
        try:
            y = int(self.year_var.get())
            m = self._month_to_num(self.month_var.get())
            d = int(self.day_var.get())
            max_d = monthrange(y, m)[1]
            if d < 1 or d > max_d:
                return None
            return f"{y:04d}-{m:02d}-{d:02d}"
        except Exception: # noqa
            return None

    # ---------- Views ----------
    def _build_select_view(self, parent) -> ctk.CTkFrame:
        f = ctk.CTkFrame(parent)
        f.grid(row=0, column=0, sticky="nsew")

        s = ctk.CTkFrame(f)
        s.pack(fill="x", padx=8, pady=8)

        # корінь
        ctk.CTkLabel(s, text="Коренева директорія:", font=("Segoe UI", 13, "bold")).grid(row=0, column=0, sticky="w", padx=12, pady=(12, 6))
        self.root_entry = ctk.CTkEntry(s, textvariable=self.root_dir_var)
        self.root_entry.grid(row=1, column=0, sticky="ew", padx=(12, 6), pady=(0, 12))
        ctk.CTkButton(s, text="Оберіть…", command=self.browse_root).grid(row=1, column=1, sticky="e", padx=(6, 12), pady=(0, 12))
        ctk.CTkButton(s, text="Автопошук", command=self.auto_detect).grid(row=1, column=2, sticky="e", padx=(0, 12), pady=(0, 12))

        # папка креативів
        ctk.CTkLabel(s, text="Папка Creatives_YYYY-MM-DD:").grid(row=2, column=0, sticky="w", padx=12, pady=(0, 6))
        self.creatives_entry = ctk.CTkEntry(s, textvariable=self.creatives_dir_var)
        self.creatives_entry.grid(row=3, column=0, sticky="ew", padx=(12, 6), pady=(0, 10))
        ctk.CTkButton(s, text="Оберіть папку…", command=self.browse_creatives_dir).grid(row=3, column=1, sticky="e", padx=(6, 12), pady=(0, 10))

        # output.txt
        ctk.CTkLabel(s, text="Файл output.txt:").grid(row=4, column=0, sticky="w", padx=12, pady=(0, 6))
        self.output_entry = ctk.CTkEntry(s, textvariable=self.output_txt_var)
        self.output_entry.grid(row=5, column=0, sticky="ew", padx=(12, 6), pady=(0, 12))
        ctk.CTkButton(s, text="Оберіть файл…", command=self.browse_output_file).grid(row=5, column=1, sticky="e", padx=(6, 12), pady=(0, 12))

        # --- Тип репорту + дата ---
        row = ctk.CTkFrame(f)
        row.pack(fill="x", padx=8, pady=(0, 12))

        ctk.CTkLabel(row, text="Тип репорту:").grid(row=0, column=0, sticky="w", padx=12, pady=(6, 4))
        ctk.CTkOptionMenu(row, values=["weekly", "monthly", "yearly"], variable=self.report_type_var, width=140).grid(row=1, column=0, sticky="w", padx=12, pady=(0, 10))

        ctk.CTkLabel(row, text="День:").grid(row=0, column=1, sticky="w", padx=12, pady=(6, 4))
        ctk.CTkEntry(row, textvariable=self.day_var, width=80).grid(row=1, column=1, sticky="w", padx=12, pady=(0, 10))

        ctk.CTkLabel(row, text="Місяць:").grid(row=0, column=2, sticky="w", padx=12, pady=(6, 4))
        ctk.CTkOptionMenu(row, values=["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], variable=self.month_var, width=110).grid(row=1, column=2, sticky="w", padx=12, pady=(0, 10))

        ctk.CTkLabel(row, text="Рік:").grid(row=0, column=3, sticky="w", padx=12, pady=(6, 4))
        ctk.CTkEntry(row, textvariable=self.year_var, width=100).grid(row=1, column=3, sticky="w", padx=12, pady=(0, 10))

        ctk.CTkFrame(row, height=6, fg_color="transparent").grid(row=2, column=0, columnspan=4)

        for c in range(4):
            row.grid_columnconfigure(c, weight=1)

        bottom = ctk.CTkFrame(f)
        bottom.pack(fill="x", padx=8, pady=(0, 8))
        self.start_btn = ctk.CTkButton(bottom, text="Старт", command=self.on_start, state="disabled")
        self.start_btn.pack(anchor="e", padx=12, pady=(12, 8))

        for var in (self.root_dir_var, self.creatives_dir_var, self.output_txt_var, self.day_var, self.year_var, self.month_var, self.report_type_var):
            var.trace_add("write", lambda *_: self.validate_ready()) # noqa

        s.grid_columnconfigure(0, weight=1)
        return f

    def _build_logs_view(self, parent) -> ctk.CTkFrame:
        f = ctk.CTkFrame(parent)
        f.grid(row=0, column=0, sticky="nsew")
        f.grid_columnconfigure(0, weight=1)
        f.grid_rowconfigure(0, weight=1)

        self.log_text = ctk.CTkTextbox(f)
        self.log_text.grid(row=0, column=0, sticky="nsew", padx=12, pady=12)

        self.log_status = ctk.CTkLabel(f, text="Йде обробка…")
        self.log_status.grid(row=1, column=0, sticky="e", padx=12, pady=(0, 12))

        return f

    def _build_metrics_view(self, parent) -> MetricsView:
        f = MetricsView(parent, on_next_stage=self._on_next_stage_clicked, on_finish=self._on_finish_clicked)
        f.grid(row=0, column=0, sticky="nsew")
        return f

    def _show_view(self, name: str) -> None:
        for child in self.page_area.winfo_children():
            child.grid_remove()
        if name == "select":
            self.view_select.grid()
        elif name == "logs":
            self.view_logs.grid()
        elif name == "metrics":
            self.view_metrics.grid()

    # ----- file pick -----
    def browse_root(self) -> None:
        d = filedialog.askdirectory(title="Оберіть кореневу директорію")
        if d:
            self.root_dir_var.set(d)
            self.auto_detect()

    def auto_detect(self) -> None:
        root_path = Path(self.root_dir_var.get().strip())
        if not root_path.exists():
            messagebox.showwarning("Увага", "Коренева директорія не існує.")
            return
        latest = find_latest_creatives_dir(root_path)
        if latest:
            self.creatives_dir_var.set(str(latest))
        out = find_output_txt(root_path, latest)
        if out:
            self.output_txt_var.set(str(out))
        self.validate_ready()

    def browse_creatives_dir(self) -> None:
        d = filedialog.askdirectory(title="Оберіть папку Creatives_YYYY-MM-DD")
        if d:
            self.creatives_dir_var.set(d)

    def browse_output_file(self) -> None:
        f = filedialog.askopenfilename(title="Оберіть файл output.txt", filetypes=[("Text", ".txt")])
        if f:
            self.output_txt_var.set(f)

    # ----- validation -----
    def validate_ready(self) -> None:
        root_ok = Path(self.root_dir_var.get().strip()).exists()
        creatives_ok = Path(self.creatives_dir_var.get().strip()).is_dir()
        output_ok = Path(self.output_txt_var.get().strip()).is_file()
        date_ok = self._report_date_iso() is not None
        self.start_btn.configure(state=("normal" if (root_ok and creatives_ok and output_ok and date_ok) else "disabled"))

    # ----- config -----
    def _apply_stage_to_config(self) -> None:
        st = self.stages[self.stage_idx]
        params = APP_CONFIG.get("params", {})
        params["number_of_frames"] = int(st["number_of_frames"])
        params["video_batch_size"] = int(st["video_batch_size"])
        # передаємо тип/дату репорту і номер стадії
        params["report_type"] = self.report_type_var.get().strip().lower()
        params["report_date"] = self._report_date_iso() or date.today().strftime("%Y-%m-%d")
        params["stage"] = int(self.stage_idx + 1)
        APP_CONFIG["params"] = params

    # ----- logs -----
    def _append_log(self, s: str) -> None:
        try:
            self.log_text.configure(state="normal")
            self.log_text.insert("end", s)
            self.log_text.see("end")
            self.log_text.configure(state="disabled")
        except Exception: # noqa
            pass

    # ----- flow -----
    def _on_next_stage_clicked(self):
        if self.stage_idx + 1 >= len(self.stages):
            return

        self._delete_prev_after_run = True
        self.stage_idx += 1
        self._run_once()

    def _on_finish_clicked(self):
        self._show_view("select")
        self.stage_idx = 0

    def _on_run_finished(self, result: Optional[Dict[str, Any]]) -> None:
        prev_report = getattr(self, "last_report_path", None)

        new_report = None
        if result:
            new_report = result.get("report_path")
            self.run_history.append(result)

        try:
            if (
                    getattr(self, "_delete_prev_after_run", False)
                    and new_report and os.path.exists(new_report)
                    and prev_report and os.path.exists(prev_report)
                    and os.path.abspath(prev_report) != os.path.abspath(new_report)
            ):
                os.remove(prev_report)
                self._append_log(f"[i] Видалено попередній звіт: {prev_report}\n")
        except Exception as e:
            self._append_log(f"[!] Не вдалося видалити попередній звіт: {e}\n")

        if new_report and os.path.exists(new_report):
            self.last_report_path = new_report

        self._delete_prev_after_run = False

        self._show_view("metrics")
        self.view_metrics.refresh(result=result or {}, stage_idx=self.stage_idx, stages=self.stages, run_history=self.run_history)

    def _run_once(self) -> None:
        creatives_dir = self.creatives_dir_var.get().strip()
        output_file = self.output_txt_var.get().strip()

        if not Path(creatives_dir).is_dir():
            messagebox.showerror("Помилка", "Папка креативів вказана некоректно.")
            return
        if not Path(output_file).is_file():
            messagebox.showerror("Помилка", "Файл output.txt вказано некоректно.")
            return

        self._apply_stage_to_config()

        self._show_view("logs")
        self.log_text.configure(state="normal"); self.log_text.delete("1.0", "end"); self.log_text.configure(state="disabled")
        self._append_log(f"[▶] Старт: {self.stages[self.stage_idx]['name']}\n")

        def runner():
            try:
                res = app_main(
                    _creatives_directory=str(creatives_dir),
                    _links_output_path=str(output_file),
                    paths=APP_CONFIG.get("paths", {}),
                    flags=APP_CONFIG.get("flags", {}),
                    params=APP_CONFIG.get("params", {}),
                )
            except Exception as e:
                tb = traceback.format_exc()
                print(f"\n\n[x] Помилка під час обробки (runner): {e.__class__.__name__}: {e}\n{tb}\n")
                res = None
            finally:
                self.after(0, lambda: self._on_run_finished(res))

        threading.Thread(target=runner, daemon=True).start()

    def on_start(self) -> None:
        self.stage_idx = 0
        self.run_history.clear()
        self._delete_prev_after_run = False
        self._run_once()


if __name__ == "__main__":
    app = App()
    app.mainloop()
