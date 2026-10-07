"""Единый менеджер JSON-хранилища в пределах процесса"""

import threading
from contextlib import contextmanager
from pathlib import Path

from valutatrade_hub.core.utils import JsonStorage
from valutatrade_hub.infra.settings import SettingsLoader


class DatabaseManager:
    """Применяет настройки файлов и блокирует операции чтение–изменение–запись"""

    _instance = None
    _instance_lock = threading.RLock()
    _file_settings = {
        "users.json": "users_file",
        "portfolios.json": "portfolios_file",
        "rates.json": "rates_file",
    }

    def __new__(cls, settings: SettingsLoader | None = None):
        """Создаёт один менеджер при первом обращении, а не при импорте"""
        # __new__ сохраняет Singleton явным и не требует метакласса
        with cls._instance_lock:
            if cls._instance is None:
                instance = super().__new__(cls)
                instance._settings = (
                    settings if settings is not None else SettingsLoader()
                )
                instance._lock = threading.RLock()
                cls._instance = instance
            elif settings is not None and settings is not cls._instance._settings:
                raise ValueError("DatabaseManager уже использует другие настройки")
            return cls._instance

    @property
    def data_dir(self) -> Path:
        """Возвращает настроенный путь к данным"""
        return Path(self._settings.get("data_dir"))

    def _filename(self, filename: str) -> str:
        """Сопоставляет логическое имя файла с настройкой"""
        if filename not in self._file_settings:
            raise ValueError(f"Неизвестный файл хранилища: {filename}")
        return self._settings.get(self._file_settings[filename])

    @contextmanager
    def transaction(self):
        """Не допускает пересечения операций записи в одном процессе"""
        with self._lock:
            yield self

    def load(self, filename: str, expected_type: type) -> dict | list:
        """Читает настроенный JSON-файл под общей блокировкой"""
        with self.transaction():
            return JsonStorage(self.data_dir).load(
                self._filename(filename), expected_type
            )

    def save(self, filename: str, data: dict | list) -> None:
        """Атомарно сохраняет настроенный JSON-файл"""
        with self.transaction():
            JsonStorage(self.data_dir).save(self._filename(filename), data)

    def initialize(self) -> None:
        """Создаёт только отсутствующие файлы с учётом настроек"""
        with self.transaction():
            for filename, empty in (
                ("users.json", []),
                ("portfolios.json", []),
                ("rates.json", {}),
            ):
                if not (self.data_dir / self._filename(filename)).exists():
                    self.save(filename, empty)
