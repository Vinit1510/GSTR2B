"""Application logging — file + in-memory queue for the GUI to consume."""
from __future__ import annotations

import logging
import queue
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import config

_GUI_QUEUE: queue.Queue[str] = queue.Queue(maxsize=10_000)


class _GuiQueueHandler(logging.Handler):
    """Push every log record (formatted string) onto a queue the GUI polls."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.format(record)
            line = self.format(record)
            try:
                _GUI_QUEUE.put_nowait(line)
            except queue.Full:
                pass
        except Exception:
            pass


def get_gui_queue() -> queue.Queue[str]:
    return _GUI_QUEUE


def setup_logging(verbose: bool = False) -> logging.Logger:
    config.ensure_dirs()
    log_file = config.LOGS_DIR / f"app-{datetime.now():%Y%m%d}.log"

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)

    # Avoid duplicate handlers if called twice
    root.handlers.clear()

    fh = RotatingFileHandler(
        log_file, maxBytes=2_000_000, backupCount=5, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    root.addHandler(fh)

    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root.addHandler(sh)

    gh = _GuiQueueHandler()
    gh.setFormatter(fmt)
    root.addHandler(gh)

    # Silence noisy third-party loggers
    for noisy in ("PIL", "easyocr", "urllib3", "asyncio", "playwright"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    return logging.getLogger("gstr2b")
