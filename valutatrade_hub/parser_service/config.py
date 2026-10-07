"""Настройки парсера и загрузка API-ключей из окружения"""

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from valutatrade_hub.core.currencies import CryptoCurrency, FiatCurrency, get_currency
from valutatrade_hub.core.utils import validate_number
from valutatrade_hub.infra.settings import SettingsLoader


@dataclass
class ParserConfig:
    """Хранит параметры API; ключи не попадают в строковое представление"""

    EXCHANGERATE_API_KEY: str = field(
        default_factory=lambda: os.getenv("EXCHANGERATE_API_KEY", ""), repr=False
    )
    COINGECKO_API_KEY: str = field(
        default_factory=lambda: os.getenv("COINGECKO_API_KEY", ""), repr=False
    )
    COINGECKO_URL: str = "https://api.coingecko.com/api/v3/simple/price"
    EXCHANGERATE_API_URL: str = "https://v6.exchangerate-api.com/v6"
    BASE_CURRENCY: str = "USD"
    FIAT_CURRENCIES: tuple = ("EUR", "GBP", "RUB")
    CRYPTO_CURRENCIES: tuple = ("BTC", "ETH", "SOL")
    CRYPTO_ID_MAP: dict = field(
        default_factory=lambda: {"BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana"}
    )
    RATES_FILE_PATH: str = "data/rates.json"
    HISTORY_FILE_PATH: str = "data/exchange_rates.json"
    REQUEST_TIMEOUT: float = 10
    UPDATE_INTERVAL: int = 300

    def __post_init__(self):
        self.REQUEST_TIMEOUT = validate_number(self.REQUEST_TIMEOUT, positive=True)
        if type(self.UPDATE_INTERVAL) is not int or self.UPDATE_INTERVAL <= 0:
            raise ValueError(
                "Интервал обновления должен быть положительным целым числом"
            )
        if self.BASE_CURRENCY != "USD":
            raise ValueError(
                "Parser Service сохраняет курсы к USD; "
                "для отображения используйте --base"
            )
        for codes, kind in (
            (self.FIAT_CURRENCIES, FiatCurrency),
            (self.CRYPTO_CURRENCIES, CryptoCurrency),
        ):
            if not isinstance(codes, (tuple, list)) or len(set(codes)) != len(codes):
                raise ValueError("Список валют должен содержать уникальные коды")
            for code in codes:
                currency = get_currency(code)
                if currency.code != code or not isinstance(currency, kind):
                    raise ValueError("Некорректный список валют парсера")
        for code in self.CRYPTO_CURRENCIES:
            raw_id = self.CRYPTO_ID_MAP.get(code)
            if not isinstance(raw_id, str) or not raw_id.strip():
                raise ValueError(f"Не указан CoinGecko ID для {code}")
        for url in (self.COINGECKO_URL, self.EXCHANGERATE_API_URL):
            parts = urlsplit(url)
            if (
                parts.scheme != "https"
                or not parts.netloc
                or parts.username
                or parts.password
                or parts.query
                or parts.fragment
            ):
                raise ValueError(
                    "Эндпоинт API должен быть HTTPS URL без ключей и параметров"
                )
        if (
            Path(self.RATES_FILE_PATH).resolve()
            == Path(self.HISTORY_FILE_PATH).resolve()
        ):
            raise ValueError("Кэш и история должны храниться в разных файлах")

    @classmethod
    def from_settings(cls, settings=None, storage=None):
        """Согласует пути парсера с хранилищем основного сервиса"""
        settings = settings if settings is not None else SettingsLoader()

        def path(logical, key):
            if storage is not None:
                return str(storage.path_for(logical))
            return str(Path(settings.get("data_dir")) / settings.get(key))

        return cls(
            RATES_FILE_PATH=path("rates.json", "rates_file"),
            HISTORY_FILE_PATH=path("exchange_rates.json", "history_file"),
            REQUEST_TIMEOUT=settings.get("request_timeout"),
            UPDATE_INTERVAL=settings.get("parser_interval_seconds"),
        )
