
from __future__ import annotations
import io
import os
import sys
import time
import logging
import threading
import queue
import re
import shutil
from dataclasses import dataclass
from typing import Any, Dict, Optional
from tqdm import tqdm

EV_LOG        = "log"
EV_PROGRESS   = "progress"
EV_STOP       = "stop"

ANSI_RESET = "\033[0m"
ANSI_PROGRESS = "\033[32m"
ANSI_COLORS = {
    "DEBUG":   "\033[90m",   # gray
    "INFO":    "\033[36m",   # cyan
    "WARNING": "\033[33m",   # yellow
    "ERROR":   "\033[31m",   # red
    "CRITICAL":"\033[41m",   # red background
}

# Фільтр прогресоподібних рядків/ANSI, щоб не плодити сміття в логах
_PROGRESS_LIKE = re.compile(r'(\r|\x1b\[[0-9;]*[A-Za-z]|[▏▎▍▌▋▊▉█]+|\d+%|#\s*#|^\s*\|\s*.+\s*\|\s*\d+%$)')

@dataclass
class ProgressState:
    img_total: int = 0
    img_done:  int = 0
    vid_total: int = 0
    vid_done: int = 0
    desc: str = "Загальний прогрес"

class ConsoleRenderer:
    """ЄДИНИЙ рендерер у main-процесі. Охайно друкує логи та малює один прогрес-бар"""
    def __init__(self, q: "queue.Queue[Dict[str, Any]]", poll_interval: float = 0.05):
        self.q = q
        self.poll_interval = poll_interval
        self._stop = threading.Event()
        self._thr: Optional[threading.Thread] = None
        self.state = ProgressState()
        self.lock = threading.Lock()

        self.pbar = tqdm(total=0, desc=self._desc(), unit="file", dynamic_ncols=True, position=0, leave=True, file=sys.stdout)

    def _desc(self) -> str:
        text = (
            f"{self.state.desc} | "
            f"img {self.state.img_done}/{self.state.img_total} | "
            f"vid {self.state.vid_done}/{self.state.vid_total}"
        )

        if not sys.stdout.isatty() and not os.getenv("ATTR_DEBUG_ENV"):
            return text

        return f"{ANSI_PROGRESS}{text}{ANSI_RESET}"

    def _clear_current_line(self):
        """Жорстко чистить поточний рядок у тому ж файлі, де бар."""
        cols = shutil.get_terminal_size((120, 20)).columns
        try:
            self.pbar.fp.write('\r' + ' ' * (max(0, cols - 1)) + '\r')
            self.pbar.fp.flush()
        except Exception:
            pass

    def _apply_progress(self, ev: Dict[str, Any]):
        for key in ("img_total", "img_done", "vid_total", "vid_done"):
            if key in ev and ev[key] is not None:
                setattr(self.state, key, int(ev[key]))
        if "desc" in ev and ev["desc"]:
            self.state.desc = str(ev["desc"])

        total = max(0, (self.state.img_total or 0) + (self.state.vid_total or 0))
        done  = max(0, (self.state.img_done  or 0) + (self.state.vid_done  or 0))

        with self.lock:
            if total != self.pbar.total:
                self.pbar.total = total
                self.pbar.refresh()
            if done > self.pbar.n:
                self.pbar.update(done - self.pbar.n)
            self.pbar.set_description(self._desc())

    def _write_log(self, text: str):
        if not text:
            return

        with self.lock:
            try:
                self._clear_current_line()
                tqdm.write(text, file=self.pbar.fp)
                time.sleep(0.02)
                self.pbar.refresh()
            except Exception as e:
                try:
                    tqdm.write(f"Logger error: {e}", file=self.pbar.fp)
                except Exception:
                    print(f"Logger error: {e}", file=sys.stdout, flush=True)

    def _handle_event(self, ev: Dict[str, Any]):
        t = ev.get("type")
        if t == EV_LOG:
            text = ev.get("text", "")
            if text and not _PROGRESS_LIKE.search(text):
                level = ev.get("level", "INFO")
                formatted = self._format_log(level, text)
                self._write_log(formatted)
        elif t == EV_PROGRESS:
            self._apply_progress(ev)
        elif t == EV_STOP:
            self._stop.set()

    @staticmethod
    def _format_log(level: str, text: str) -> str:
        level = (level or "INFO").upper()

        if not sys.stdout.isatty() and not os.getenv("ATTR_DEBUG_ENV", False):
            return f"{level}: {text}"

        color = ANSI_COLORS.get(level, "")
        reset = ANSI_RESET if color else ""

        return f"{color}{level}:{reset} {text}"

    def run_forever(self):
        while not self._stop.is_set():
            try:
                ev = self.q.get(timeout=self.poll_interval)
                last_prog = None

                if ev.get("type") == EV_PROGRESS:
                    last_prog = ev
                else:
                    self._handle_event(ev)

                while True:
                    try:
                        ev = self.q.get_nowait()
                    except queue.Empty:
                        break
                    if ev.get("type") == EV_PROGRESS:
                        last_prog = ev
                    else:
                        self._handle_event(ev)

                if last_prog:
                    self._apply_progress(last_prog)

            except queue.Empty:
                continue
            except Exception as e:
                with self.lock:
                    self._clear_current_line()
                    tqdm.write(f"Renderer error: {e}", file=self.pbar.fp)
                    self.pbar.refresh()

    def start(self):
        self._thr = threading.Thread(target=self.run_forever, name="ConsoleRenderer", daemon=True)
        self._thr.start()

    def stop(self):
        self._stop.set()
        try:
            self.q.put_nowait({"type": EV_STOP})
        except Exception:
            pass
        try:
            if self._thr:
                self._thr.join(timeout=1.0)
        finally:
            with self.lock:
                self.pbar.set_description(self._desc())
                self.pbar.close()


# ------------------ Клиєнт API (main + воркери) ------------------
_console_q: Optional["queue.Queue[Dict[str, Any]]"] = None

def init_console_client(q: "queue.Queue[Dict[str, Any]]",
                        disable_local_tqdm: bool = True,
                        redirect_streams: bool = True,
                        attach_logging: bool = True,
                        capture_native_fds: bool = False):
    """
    Підключає процес до консольної шини:
      - за потреби вимикає локальний tqdm,
      - перекидає Python stdout/stderr у чергу,
      - рівень logging -> у чергу,
      - (опц.) перехоплює нативний STDERR (fd=2) - для ffmpeg та ін. бінарей.
    """
    global _console_q
    _console_q = q

    if disable_local_tqdm:
        os.environ["TQDM_DISABLE"] = "1"

    if redirect_streams:
        sys.stdout = _StreamToQueue(level="INFO")
        sys.stderr = _StreamToQueue(level="WARNING")

    if attach_logging:
        root = logging.getLogger()
        for h in list(root.handlers):
            root.removeHandler(h)
        root.addHandler(_LoggingToQueueHandler())
        root.setLevel(logging.INFO)
        for name in ("transformers", "huggingface_hub", "urllib3", "openai", "matplotlib"):
            lg = logging.getLogger(name)
            lg.handlers = []
            lg.propagate = False
            lg.setLevel(logging.WARNING)

    if capture_native_fds:
        _start_native_stderr_forwarder(level="WARNING")
        # при бажанні можна також stdout бінарей:
        # _start_native_stdout_forwarder(level="INFO")

def emit_log(text: str, level: str = "INFO"):
    if _console_q is None:
        return

    _console_q.put_nowait({"type": EV_LOG, "level": level, "text": text})

def emit_progress(img_total: int | None = None, img_done: int | None = None,
                  vid_total: int | None = None, vid_done: int | None = None,
                  desc: Optional[str] = None):
    if _console_q is None:
        return
    try:
        _console_q.put_nowait({
            "type": EV_PROGRESS,
            "img_total": img_total, "img_done": img_done,
            "vid_total": vid_total, "vid_done": vid_done,
            "desc": desc
        })
    except Exception:
        pass

# ------------------ Перенаправлення Python streams ------------------

class _StreamToQueue(io.TextIOBase):
    def __init__(self, level: str = "INFO"):
        self.level = level
        self._buf = ""

    def write(self, buf: str):
        if not buf:
            return 0
        buf = buf.replace('\r', '\n')
        self._buf += buf
        while '\n' in self._buf:
            line, self._buf = self._buf.split('\n', 1)
            line = line.strip()
            if line and not _PROGRESS_LIKE.search(line):
                emit_log(line, level=self.level)
        return len(buf)

    def flush(self):
        if self._buf:
            line = self._buf.strip()
            self._buf = ""
            if line and not _PROGRESS_LIKE.search(line):
                emit_log(line, level=self.level)

class _LoggingToQueueHandler(logging.Handler):
    def emit(self, record: logging.LogRecord):
        try:
            msg = self.format(record)
        except Exception:
            msg = record.getMessage()
        emit_log(msg, level=record.levelname)

# ------------------ Перенаправлення нативних FD ------------------

def _spawn_fd_forwarder(src_fd: int, level: str):
    """
    Прив'язує pipe до fd (1 або 2) поточного процесу та перекидає в чергу.
    !НЕ використовувати для процесу, який малює pbar (main), бо тоді pbar не буде видно.
    """
    r_fd, w_fd = os.pipe()
    # перенаправляємо вихід src_fd у кінець запису пайпа
    os.dup2(w_fd, src_fd)
    os.close(w_fd)

    def _reader():
        buf = b""
        while True:
            try:
                chunk = os.read(r_fd, 4096)
                if not chunk:
                    break
                buf += chunk
                while b'\n' in buf:
                    line, buf = buf.split(b'\n', 1)
                    try:
                        text = line.decode('utf-8', errors='replace').strip()
                    except Exception:
                        text = str(line).strip()
                    if text and not _PROGRESS_LIKE.search(text):
                        # emit_log(text, level=level)
                        pass
            except Exception:
                break

    t = threading.Thread(target=_reader, name=f"fd{src_fd}-forwarder", daemon=True)
    t.start()

def _start_native_stderr_forwarder(level: str = "WARNING"):
    _spawn_fd_forwarder(2, level)

def _start_native_stdout_forwarder(level: str = "INFO"):
    _spawn_fd_forwarder(1, level)
