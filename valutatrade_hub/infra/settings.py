"""Ленивая загрузка конфигурации в единственном экземпляре"""

import json
import threading
import tomllib
from copy import deepcopy
from pathlib import Path
from typing import Any

DEFAULTS = {
    "data_dir": "data",
    "users_file": "users.json",
    "portfolios_file": "portfolios.json",
    "rates_file": "rates.json",
    "rates_ttl_seconds": 300,
    "default_base_currency": "USD",
    "log_dir": "logs",
    "log_file": "actions.log",
    "log_format": "json",
    "log_level": "INFO",
    "log_max_bytes": 1048576,
    "log_backup_count": 3,
    "log_verbose": False,
}


class SettingsLoader:
    """Кэширует конфигурацию из tool.valutatrade либо отдельного JSON"""

    _instance = None
    _instance_lock = threading.RLock()

    def __new__(cls, config_path: str | Path | None = None):
        """Создаёт и загружает настройки только при первом обращении"""
        # __new__ выбран как простой и явный способ без отдельного метакласса
        with cls._instance_lock:
            if cls._instance is None:
                instance = super().__new__(cls)
                instance._config_path = Path(config_path or "pyproject.toml").resolve()
                instance._explicit_path = config_path is not None
                instance._lock = threading.RLock()
                instance.reload()
                cls._instance = instance
            elif config_path is not None:
                if Path(config_path).resolve() != cls._instance._config_path:
                    raise ValueError(
                        "SettingsLoader уже использует другой файл настроек"
                    )
            return cls._instance

    def get(self, key: str, default: Any = None) -> Any:
        """Возвращает копию значения из кэша или значение по умолчанию"""
        with self._lock:
            return deepcopy(self._values.get(key, default))

    def reload(self) -> None:
        """Перечитывает файл; ошибочная конфигурация не заменяет рабочую"""
        with self._lock:
            overrides = {}
            try:
                with self._config_path.open("rb") as file:
                    if self._config_path.suffix == ".json":
                        overrides = json.load(file)
                    else:
                        document = tomllib.load(file)
                        overrides = document.get("tool", {}).get("valutatrade", {})
            except FileNotFoundError:
                if self._explicit_path:
                    raise
            except (ValueError, UnicodeError, AttributeError) as error:
                raise ValueError("Некорректный файл настроек") from error
            if not isinstance(overrides, dict):
                raise ValueError("Настройки valutatrade должны быть словарём")
            unknown = overrides.keys() - DEFAULTS.keys()
            if unknown:
                raise ValueError("Неизвестные настройки: " + ", ".join(sorted(unknown)))
            values = {**DEFAULTS, **overrides}
            self._validate(values)
            for key in ("data_dir", "log_dir"):
                values[key] = str((self._config_path.parent / values[key]).resolve())
            self._values = values

    @staticmethod
    def _validate(values: dict) -> None:
        """Проверяет пути, срок курсов и параметры журнала"""
        for key in ("data_dir", "log_dir"):
            if not isinstance(values[key], str) or not values[key].strip():
                raise ValueError(f"Настройка {key} должна быть непустым путём")
        filenames = [
            values[key] for key in ("users_file", "portfolios_file", "rates_file")
        ]
        for value in [*filenames, values["log_file"]]:
            if (
                not isinstance(value, str)
                or not value.strip()
                or "/" in value
                or "\\" in value
                or value in {".", ".."}
            ):
                raise ValueError("Имена файлов должны указываться без пути")
        if len(set(filenames)) != len(filenames):
            raise ValueError("Для данных нужны три разных JSON-файла")
        for key in ("rates_ttl_seconds", "log_max_bytes", "log_backup_count"):
            if type(values[key]) is not int or values[key] <= 0:
                raise ValueError(
                    f"Настройка {key} должна быть положительным целым числом"
                )
        if values["log_format"] != "json":
            raise ValueError("Поддерживается формат журнала json")
        if values["log_level"] not in ("INFO", "DEBUG"):
            raise ValueError("Уровень журнала должен быть INFO или DEBUG")
        if type(values["log_verbose"]) is not bool:
            raise ValueError("Настройка log_verbose должна быть логическим значением")
        base = values["default_base_currency"]
        if (
            not isinstance(base, str)
            or not 2 <= len(base) <= 5
            or not base.isascii()
            or not base.isalnum()
            or not base[0].isalpha()
            or base != base.upper()
        ):
            raise ValueError("Некорректная базовая валюта в настройках")
