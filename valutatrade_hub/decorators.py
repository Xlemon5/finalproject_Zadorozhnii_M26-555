"""Декоратор регистрации доменных операций без записи паролей"""

import math
from functools import wraps
from inspect import signature

from valutatrade_hub.logging_config import configure_logging


def _log_value(value):
    """Преобразует некорректный ввод в безопасное для JSON представление"""
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return f"<{type(value).__name__}>"


def log_action(action: str, *, verbose: bool | None = None):
    """Записывает результат операции и пробрасывает её исключения без замены"""

    def decorator(function):
        """Оборачивает функцию с сохранением её имени, документации и сигнатуры"""
        parameters = signature(function)

        @wraps(function)
        def wrapper(*args, **kwargs):
            """Записывает только разрешённые поля аргументов и результата"""
            arguments = parameters.bind(*args, **kwargs).arguments
            service = arguments.get("self")
            settings = getattr(service, "settings", None)
            logger = getattr(service, "logger", None)
            if logger is None:
                logger = configure_logging(settings)
            user = getattr(service, "current_user", None)
            event = {
                "action": action.upper(),
                "user_id": getattr(user, "user_id", arguments.get("user_id")),
                "username": getattr(user, "username", arguments.get("username")),
                "currency_code": _log_value(arguments.get("currency_code")),
                "amount": _log_value(arguments.get("amount")),
                "rate": None,
                "base": "USD" if action.upper() in {"BUY", "SELL"} else None,
            }
            details = (
                verbose
                if verbose is not None
                else (
                    settings.get("log_verbose", False)
                    if settings is not None
                    else False
                )
            )
            try:
                result = function(*args, **kwargs)
            except Exception as error:
                event.update(
                    result="ERROR",
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                if details and hasattr(error, "available"):
                    event["context"] = {
                        "available": error.available,
                        "required": error.required,
                    }
                logger.info(event)
                raise
            event["result"] = "OK"
            if isinstance(result, dict):
                event.update(
                    currency_code=result.get("currency", event["currency_code"]),
                    amount=result.get("amount", event["amount"]),
                    rate=result.get("rate"),
                    base=result.get("base", event["base"]),
                )
                if details:
                    event["context"] = {
                        key: result[key]
                        for key in ("before", "after", "usd_before", "usd_after")
                        if key in result
                    }
            logger.info(event)
            return result

        return wrapper

    return decorator
