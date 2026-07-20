"""
Logging structuré pour le pipeline.
Support JSON et texte. Logs en fichier et console.
"""

import logging
import logging.handlers
import json
import sys
from pathlib import Path
from datetime import datetime
from typing import Any
from config import settings


class JsonFormatter(logging.Formatter):
    """Formateur JSON pour structured logging."""
    
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
        
        # Ajouter les informations d'exception si présente
        if record.exc_info:
            log_data["exception"] = self.formatException(record.exc_info)
        
        # Ajouter les extras
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


def setup_logging():
    """Configure le système de logging global."""
    
    # Configuration de base
    log_level = getattr(logging, settings.logging.level.upper(), logging.INFO)
    
    # Formatter
    if settings.logging.format == "json":
        formatter = JsonFormatter()
    else:
        formatter = TextFormatter()
    
    # Root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)
    root_logger.handlers.clear()  # évite les handlers dupliqués si appelé plusieurs fois

    # Handler fichier
    log_file = Path(settings.logging.file)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    
    file_handler = logging.handlers.RotatingFileHandler(
        log_file,
        maxBytes=10 * 1024 * 1024,  # 10 MB
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setLevel(log_level)
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)

    # Handler console
    # Force l'UTF-8 sur stdout : la console Windows (cp1252) plante sinon
    # sur certains caractères utilisés dans les logs (ex. "✓").
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(log_level)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)
    
    return root_logger


def get_logger(name: str) -> logging.LoggerAdapter:
    """Récupère un logger avec le nom fourni."""
    logger = logging.getLogger(name)
    
    class LoggerAdapter(logging.LoggerAdapter):
        def process(self, msg, kwargs):
            return msg, kwargs
        
        def log_with_data(self, level, msg, extra_data=None, **kwargs):
            """Log avec données supplémentaires (contexte)."""
            if extra_data:
                record = self.logger.makeRecord(
                    self.logger.name,
                    level,
                    self.logger.name,
                    1,
                    msg,
                    (),
                    None,
                    **kwargs
                )
                record.extra_data = extra_data
                self.logger.handle(record)
            else:
                self.logger.log(level, msg, **kwargs)
    
    return LoggerAdapter(logger, {})


# Setup au démarrage du module
_logger = setup_logging()