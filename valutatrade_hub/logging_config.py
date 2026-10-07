"""JSON-журнал действий с ротацией по размеру файла"""

import json
import logging
import threading
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from valutatrade_hub.infra.settings import SettingsLoader

_configuration_lock = threading.RLock()


class ActionFormatter(logging.Formatter):
    """Записывает одно событие на строку в формате JSON"""

    def format(self, record: logging.LogRecord) -> str:
        """Добавляет UTC-время и уровень к структурированному событию"""
        event = (
            dict(record.msg)
            if isinstance(record.msg, dict)
            else {
                "message": record.getMessage(),
            }
        )
        event["timestamp"] = datetime.fromtimestamp(record.created, UTC).isoformat(
            timespec="milliseconds"
        )
        event["level"] = record.levelname
        return json.dumps(event, ensure_ascii=False, allow_nan=False)


def configure_logging(
    settings: SettingsLoader | None = None, *, parser=False
) -> logging.Logger:
    """Настраивает один обработчик; повторные вызовы не дублируют записи"""
    settings = settings if settings is not None else SettingsLoader()
    path = Path(settings.get("log_dir")) / settings.get(
        "parser_log_file" if parser else "log_file"
    )
    signature = (
        str(path),
        settings.get("log_max_bytes"),
        settings.get("log_backup_count"),
    )
    with _configuration_lock:
        logger = logging.getLogger(
            "valutatrade.parser" if parser else "valutatrade.actions"
        )
        logger.setLevel(settings.get("log_level"))
        logger.propagate = False
        for handler in logger.handlers:
            if getattr(handler, "wallet_signature", None) == signature:
                return logger
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            path,
            maxBytes=signature[1],
            backupCount=signature[2],
            encoding="utf-8",
        )
        handler.wallet_signature = signature
        handler.setFormatter(ActionFormatter())
        for previous in logger.handlers[:]:
            if hasattr(previous, "wallet_signature"):
                logger.removeHandler(previous)
                previous.close()
        logger.addHandler(handler)
        return logger
