"""
logger.py - Production-Level Structured Logging for OmniExtract

Features:
  - Dual output: colored console + structured JSON file
  - Thread-safe (safe to call from ThreadPoolExecutor workers)
  - Timing helpers (log_duration)
  - Log file rotates daily, stored in logs/ directory
  - Zero Streamlit dependencies — safe from any thread
"""

import os
import sys
import json
import time
import logging
import threading
import traceback
from datetime import datetime
from logging.handlers import RotatingFileHandler

# ---------------------------------------------------------------------------
# Log directory
# ---------------------------------------------------------------------------
LOG_DIR = os.path.join(os.path.dirname(__file__), "logs")
os.makedirs(LOG_DIR, exist_ok=True)

LOG_FILE = os.path.join(LOG_DIR, "omniextract.log")

# ---------------------------------------------------------------------------
# ANSI colors for console output
# ---------------------------------------------------------------------------
_COLORS = {
    "DEBUG":    "\033[36m",   # Cyan
    "INFO":     "\033[32m",   # Green
    "WARNING":  "\033[33m",   # Yellow
    "ERROR":    "\033[31m",   # Red
    "CRITICAL": "\033[35m",   # Magenta
    "RESET":    "\033[0m",
    "DIM":      "\033[90m",
    "BOLD":     "\033[1m",
}


# ---------------------------------------------------------------------------
# Custom formatter: colored for console, JSON for file
# ---------------------------------------------------------------------------
class ColorConsoleFormatter(logging.Formatter):
    def format(self, record):
        level = record.levelname
        color = _COLORS.get(level, "")
        reset = _COLORS["RESET"]
        dim   = _COLORS["DIM"]
        bold  = _COLORS["BOLD"]

        ts = datetime.fromtimestamp(record.created).strftime("%H:%M:%S.%f")[:-3]
        tid = threading.current_thread().name

        # Extra fields injected by our helpers
        ctx = getattr(record, "ctx", "")
        duration = getattr(record, "duration_ms", None)
        dur_str = f"  {dim}[{duration:.0f}ms]{reset}" if duration is not None else ""

        ctx_str = f"  {dim}[{ctx}]{reset}" if ctx else ""
        msg = record.getMessage()

        return (
            f"{dim}{ts}{reset}  "
            f"{color}{bold}{level:<8}{reset}"
            f"{dim}[{tid}]{reset}"
            f"{ctx_str}"
            f"  {msg}"
            f"{dur_str}"
        )


class JsonFileFormatter(logging.Formatter):
    def format(self, record):
        entry = {
            "ts":      datetime.fromtimestamp(record.created).isoformat(),
            "level":   record.levelname,
            "thread":  threading.current_thread().name,
            "module":  record.module,
            "func":    record.funcName,
            "line":    record.lineno,
            "msg":     record.getMessage(),
        }
        # Extra context fields
        for key in ("ctx", "duration_ms", "chunk_idx", "model", "tokens", "file"):
            val = getattr(record, key, None)
            if val is not None:
                entry[key] = val
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Build the root logger
# ---------------------------------------------------------------------------
def _build_logger() -> logging.Logger:
    logger = logging.getLogger("omniextract")
    if logger.handlers:
        return logger  # Already configured (e.g., Streamlit hot-reload)

    logger.setLevel(logging.DEBUG)

    # Console handler (INFO+)
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.DEBUG)
    ch.setFormatter(ColorConsoleFormatter())
    logger.addHandler(ch)

    # File handler — rotating, max 10 MB × 3 backups
    fh = RotatingFileHandler(
        LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(JsonFileFormatter())
    logger.addHandler(fh)

    logger.propagate = False
    return logger


log = _build_logger()


# ---------------------------------------------------------------------------
# Convenience helpers
# ---------------------------------------------------------------------------
def info(msg: str, ctx: str = "", **extra):
    log.info(msg, extra={"ctx": ctx, **extra})

def debug(msg: str, ctx: str = "", **extra):
    log.debug(msg, extra={"ctx": ctx, **extra})

def warning(msg: str, ctx: str = "", **extra):
    log.warning(msg, extra={"ctx": ctx, **extra})

def error(msg: str, ctx: str = "", exc: Exception = None, **extra):
    if exc:
        log.error(msg, exc_info=exc, extra={"ctx": ctx, **extra})
    else:
        log.error(msg, extra={"ctx": ctx, **extra})

def critical(msg: str, ctx: str = "", exc: Exception = None, **extra):
    log.critical(msg, exc_info=exc, extra={"ctx": ctx, **extra})


# ---------------------------------------------------------------------------
# Timing context manager
# ---------------------------------------------------------------------------
class Timer:
    """
    Usage:
        with Timer("LLM call", ctx="chunk_0") as t:
            result = llm.invoke(...)
        # Logs: "LLM call completed" with duration_ms
    """
    def __init__(self, label: str, ctx: str = "", level: str = "INFO", **extra):
        self.label   = label
        self.ctx     = ctx
        self.level   = level
        self.extra   = extra
        self.start   = None
        self.elapsed = None

    def __enter__(self):
        self.start = time.perf_counter()
        debug(f"→ START  {self.label}", ctx=self.ctx, **self.extra)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.elapsed = (time.perf_counter() - self.start) * 1000  # ms
        if exc_type:
            error(
                f"✗ FAILED {self.label} after {self.elapsed:.0f}ms — {exc_val}",
                ctx=self.ctx,
                duration_ms=self.elapsed,
                **self.extra,
            )
        else:
            fn = getattr(log, self.level.lower(), log.info)
            fn(
                f"✓ DONE   {self.label}",
                extra={"ctx": self.ctx, "duration_ms": self.elapsed, **self.extra},
            )
        return False  # Don't suppress exceptions


# ---------------------------------------------------------------------------
# Session separator — call once at pipeline start
# ---------------------------------------------------------------------------
def log_pipeline_start(file_names: list, model: str):
    separator = "=" * 72
    log.info(separator, extra={"ctx": "PIPELINE"})
    log.info(
        f"NEW EXTRACTION SESSION | model={model} | files={file_names}",
        extra={"ctx": "PIPELINE"},
    )
    log.info(separator, extra={"ctx": "PIPELINE"})
