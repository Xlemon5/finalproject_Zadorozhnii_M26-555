"""Валидация, учебные курсы и чтение/запись JSON"""

import json
import math
import os
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path

# Учебные значения: стоимость одной единицы валюты в USD
EXCHANGE_RATES = {
    "USD": 1.0,
    "EUR": 1.0786,
    "BTC": 59337.21,
    "RUB": 0.01016,
    "ETH": 3720.0,
}


def normalize_currency_code(currency_code: str) -> str:
    """Проверяет код и приводит его к верхнему регистру"""
    if not isinstance(currency_code, str):
        raise ValueError("Код валюты должен быть непустой строкой")
    code = currency_code.strip().upper()
    if (
        not 2 <= len(code) <= 5
        or not code.isascii()
        or not code.isalnum()
        or not code[0].isalpha()
    ):
        raise ValueError("Код валюты должен содержать 2–5 латинских букв или цифр")
    return code


def validate_number(value: float, *, positive: bool = False) -> float:
    """Принимает конечные int/float; исключает bool и недопустимые знаки"""
    message = (
        "'amount' должен быть положительным числом"
        if positive
        else "Баланс должен быть неотрицательным конечным числом"
    )
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(message)
    try:
        number = float(value)
    except OverflowError as error:
        raise ValueError(message) from error
    if not math.isfinite(number) or number < 0 or (positive and number == 0):
        raise ValueError(message)
    return number


def validate_user_id(user_id: int) -> int:
    """Проверяет положительный целочисленный идентификатор"""
    if type(user_id) is not int or user_id <= 0:
        raise ValueError("user_id должен быть положительным целым числом")
    return user_id


def normalize_username(username: str) -> str:
    """Удаляет окружающие пробелы и запрещает пустое имя"""
    if not isinstance(username, str) or not username.strip():
        raise ValueError("Имя пользователя не может быть пустым")
    return username.strip()


def validate_password(password: str) -> None:
    """Проверяет минимальную длину пароля без изменения его содержимого"""
    if not isinstance(password, str) or len(password) < 4:
        raise ValueError("Пароль должен быть не короче 4 символов")


class JsonStorage:
    """Хранит файлы в выбранной папке; каждый файл заменяет атомарно"""

    def __init__(self, data_dir: str | Path = "data") -> None:
        """Выбирает папку данных относительно текущего рабочего каталога"""
        self.data_dir = Path(data_dir)
        self._lock = threading.RLock()

    @contextmanager
    def transaction(self):
        """Объединяет чтение и запись в одну операцию в пределах экземпляра"""
        with self._lock:
            yield self

    def load(self, filename: str, expected_type: type) -> dict | list:
        """Читает JSON; отсутствие файла означает пустую коллекцию"""
        path = self.data_dir / filename
        try:
            with path.open(encoding="utf-8") as file:
                data = json.load(file)
        except FileNotFoundError:
            return expected_type()
        except (ValueError, UnicodeError) as error:
            raise ValueError(f"Повреждён файл {path}: некорректный JSON") from error
        if not isinstance(data, expected_type):
            raise ValueError(f"Некорректная структура файла {path}")
        return data

    def save(self, filename: str, data: dict | list) -> None:
        """Записывает временный файл рядом с исходным и заменяет его"""
        encoded = json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        path = self.data_dir / filename
        temporary_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.data_dir,
                prefix=f".{filename}.",
                suffix=".tmp",
                delete=False,
            ) as file:
                temporary_path = Path(file.name)
                file.write(encoded + "\n")
            os.replace(temporary_path, path)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    def initialize(self) -> None:
        """Создаёт отсутствующие файлы, не меняя уже существующие"""
        for filename, empty in (
            ("users.json", []),
            ("portfolios.json", []),
            ("rates.json", {}),
        ):
            if not (self.data_dir / filename).exists():
                self.save(filename, empty)
