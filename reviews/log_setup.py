"""
Logging structuré (JSON ou texte), fichier + console.
setup_logging() est appelé explicitement par les points d'entrée
(CLI, API, scheduler) — aucun effet de bord à l'import.
"""

import logging
import logging.handlers
import json
import sys
from pathlib import Path
from datetime import datetime

from reviews.config import get_settings


class JsonFormatter(logging.Formatter):
    """Formateur JSON pour logging structuré."""

    def format(self, record: logging.LogRecord) -> str:
        log_data = {
            "timestamp": datetime.utcnow().isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
        }
        if record.exc_info:
            log_data["exception"] = self.formatException(record.exc_info)
        if hasattr(record, "extra_data"):
            log_data.update(record.extra_data)
        return json.dumps(log_data, ensure_ascii=False)


class TextFormatter(logging.Formatter):
    """Formateur texte lisible."""

    def format(self, record: logging.LogRecord) -> str:
        timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        extra = ""
        if hasattr(record, "extra_data"):
            extra = " | " + json.dumps(record.extra_data)
        return (
            f"[{timestamp}] {record.levelname:8} | "
            f"{record.name}:{record.funcName}:{record.lineno} | "
            f"{record.getMessage()}{extra}"
        )


def setup_logging() -> logging.Logger:
    """Configure le logging global (idempotent)."""
    settings = get_settings()
    log_level = getattr(logging, settings.logging.level.upper(), logging.INFO)
    formatter = JsonFormatter() if settings.logging.format == "json" else TextFormatter()

    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)
    root_logger.handlers.clear()  # évite les handlers dupliqués

    # Fichier (rotation)
    log_file = Path(settings.logging.file)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    file_handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setLevel(log_level)
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)

    # Console (force UTF-8 : la console Windows plante sinon sur "✓", etc.)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(log_level)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

    return root_logger
