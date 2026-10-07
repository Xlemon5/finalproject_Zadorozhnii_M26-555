"""Иерархия валют и фабрика объектов из общего реестра"""

from abc import ABC, abstractmethod
from copy import copy

from valutatrade_hub.core.exceptions import CurrencyNotFoundError
from valutatrade_hub.core.utils import normalize_currency_code, validate_number


def _required_text(value: str, field: str) -> str:
    """Проверяет непустое текстовое свойство валюты"""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} должен быть непустой строкой")
    return value.strip()


class Currency(ABC):
    """Задает общие свойства валюты и контракт ее представления"""

    def __init__(self, name: str, code: str) -> None:
        """Проверяет имя и код при создании валюты"""
        self.name = name
        self.code = code

    @property
    def name(self) -> str:
        """Возвращает человекочитаемое имя"""
        return self._name

    @name.setter
    def name(self, value: str) -> None:
        """Запрещает пустое имя"""
        self._name = _required_text(value, "name")

    @property
    def code(self) -> str:
        """Возвращает код валюты"""
        return self._code

    @code.setter
    def code(self, value: str) -> None:
        """Требует код из 2–5 символов в верхнем регистре без пробелов"""
        normalized = normalize_currency_code(value)
        if value != normalized:
            raise ValueError("Код валюты должен быть в верхнем регистре без пробелов")
        self._code = normalized

    @abstractmethod
    def get_display_info(self) -> str:
        """Возвращает строковое представление для интерфейса и журнала"""
        ...


class FiatCurrency(Currency):
    """Фиатная валюта со страной или зоной эмиссии"""

    def __init__(self, name: str, code: str, issuing_country: str) -> None:
        """Создает фиатную валюту с проверкой страны эмиссии"""
        super().__init__(name, code)
        self.issuing_country = issuing_country

    @property
    def issuing_country(self) -> str:
        """Возвращает страну или зону эмиссии"""
        return self._issuing_country

    @issuing_country.setter
    def issuing_country(self, value: str) -> None:
        """Проверяет название страны или зоны"""
        self._issuing_country = _required_text(value, "issuing_country")

    def get_display_info(self) -> str:
        """Добавляет страну или зону эмиссии к представлению валюты"""
        return f"[FIAT] {self.code} — {self.name} (Issuing: {self.issuing_country})"


class CryptoCurrency(Currency):
    """Криптовалюта с алгоритмом и последней известной капитализацией"""

    def __init__(self, name: str, code: str, algorithm: str, market_cap: float) -> None:
        """Создает криптовалюту с проверкой дополнительных атрибутов"""
        super().__init__(name, code)
        self.algorithm = algorithm
        self.market_cap = market_cap

    @property
    def algorithm(self) -> str:
        """Возвращает название алгоритма"""
        return self._algorithm

    @algorithm.setter
    def algorithm(self, value: str) -> None:
        """Проверяет непустое название алгоритма"""
        self._algorithm = _required_text(value, "algorithm")

    @property
    def market_cap(self) -> float:
        """Возвращает последнюю известную капитализацию"""
        return self._market_cap

    @market_cap.setter
    def market_cap(self, value: float) -> None:
        """Запрещает отрицательную и неконечную капитализацию"""
        self._market_cap = validate_number(value)

    def get_display_info(self) -> str:
        """Добавляет алгоритм и капитализацию в научной записи"""
        mantissa, exponent = f"{self.market_cap:.2e}".split("e")
        return (
            f"[CRYPTO] {self.code} — {self.name} "
            f"(Algo: {self.algorithm}, MCAP: {mantissa}e{int(exponent)})"
        )


# Параметры криптовалют взяты как учебные примеры, без запроса актуальных данных
CURRENCY_REGISTRY = {
    "USD": FiatCurrency("US Dollar", "USD", "United States"),
    "EUR": FiatCurrency("Euro", "EUR", "Eurozone"),
    "RUB": FiatCurrency("Russian Ruble", "RUB", "Russia"),
    "GBP": FiatCurrency("Pound Sterling", "GBP", "United Kingdom"),
    "BTC": CryptoCurrency("Bitcoin", "BTC", "SHA-256", 1.12e12),
    "ETH": CryptoCurrency("Ethereum", "ETH", "Ethash", 4.5e11),
    "SOL": CryptoCurrency("Solana", "SOL", "PoS / PoH", 0.0),
}


def get_currency(code: str) -> Currency:
    """Возвращает отдельный объект валюты или CurrencyNotFoundError"""
    try:
        normalized = normalize_currency_code(code)
    except ValueError as error:
        display_code = code.strip().upper() if isinstance(code, str) else str(code)
        raise CurrencyNotFoundError(display_code) from error
    if normalized not in CURRENCY_REGISTRY:
        raise CurrencyNotFoundError(normalized)
    return copy(CURRENCY_REGISTRY[normalized])


def get_supported_codes() -> tuple[str, ...]:
    """Возвращает отсортированные коды зарегистрированных валют"""
    return tuple(sorted(CURRENCY_REGISTRY))
