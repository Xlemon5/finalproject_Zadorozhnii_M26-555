"""Единый контракт курсов: локальный кэш и учебная заглушка"""

from datetime import UTC, datetime

from valutatrade_hub.core.currencies import get_currency, get_supported_codes
from valutatrade_hub.core.exceptions import ApiRequestError
from valutatrade_hub.core.utils import (
    EXCHANGE_RATES,
    JsonStorage,
    normalize_currency_code,
    validate_number,
)
from valutatrade_hub.infra.database import DatabaseManager
from valutatrade_hub.infra.settings import SettingsLoader


class RateService:
    """Читает прямые, обратные и кросс-курсы с ограниченным сроком жизни"""

    def __init__(
        self,
        storage: JsonStorage | DatabaseManager,
        ttl: int | None = None,
        *,
        settings: SettingsLoader | None = None,
        provider=None,
    ) -> None:
        """Настраивает хранилище и срок свежести в секундах"""
        self.storage = storage
        if ttl is not None and (type(ttl) is not int or ttl <= 0):
            raise ValueError("TTL должен быть положительным целым числом")
        self._ttl_override = ttl
        self.settings = settings if settings is not None else SettingsLoader()
        self._provider = provider if provider is not None else self._stub_rates

    @property
    def ttl(self) -> int:
        """Возвращает актуальный TTL из настроек либо явное переопределение"""
        if self._ttl_override is not None:
            return self._ttl_override
        return self.settings.get("rates_ttl_seconds")

    @staticmethod
    def _stub_rates() -> dict:
        """Возвращает учебные курсы в контракте будущего Parser Service"""
        return {"source": "Stub", "rates": EXCHANGE_RATES.copy()}

    @staticmethod
    def _timestamp(value: str) -> datetime:
        """Читает ISO-дату; даты без часового пояса трактует как UTC"""
        if not isinstance(value, str):
            raise ValueError("Некорректная дата курса")
        timestamp = datetime.fromisoformat(value)
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=UTC)
        return timestamp.astimezone(UTC)

    def _load(self) -> dict:
        """Проверяет кэш до использования или обновления"""
        cache = self.storage.load("rates.json", dict)
        try:
            for pair, entry in cache.items():
                if pair == "source":
                    if not isinstance(entry, str):
                        raise ValueError
                    continue
                if pair == "last_refresh":
                    self._timestamp(entry)
                    continue
                source, target = pair.split("_")
                if (
                    normalize_currency_code(source) != source
                    or normalize_currency_code(target) != target
                    or not isinstance(entry, dict)
                ):
                    raise ValueError
                validate_number(entry["rate"], positive=True)
                self._timestamp(entry["updated_at"])
                if "source" in entry and not isinstance(entry["source"], str):
                    raise ValueError
        except (KeyError, TypeError, ValueError, OverflowError) as error:
            raise ValueError("Некорректные данные в rates.json") from error
        return cache

    def known_currencies(self) -> set[str]:
        """Возвращает поддерживаемые валюты из общего реестра"""
        return set(get_supported_codes())

    def _direct(self, cache: dict, source: str, target: str, now: datetime):
        """Ищет свежий прямой или обратный курс"""
        if source == target:
            return {"rate": 1.0, "updated_at": now.isoformat(), "source": "identity"}
        for key, inverse in (
            (f"{source}_{target}", False),
            (f"{target}_{source}", True),
        ):
            entry = cache.get(key)
            if entry is None:
                continue
            age = (now - self._timestamp(entry["updated_at"])).total_seconds()
            if 0 <= age < self.ttl:
                rate = 1.0 / entry["rate"] if inverse else entry["rate"]
                return {
                    "rate": validate_number(rate, positive=True),
                    "updated_at": entry["updated_at"],
                    "source": entry.get("source", cache.get("source", "cache")),
                }
        return None

    def _find(self, cache: dict, source: str, target: str, now: datetime):
        """Ищет прямой курс или вычисляет кросс-курс через USD"""
        direct = self._direct(cache, source, target, now)
        if direct is not None:
            return direct
        first = self._direct(cache, source, "USD", now)
        second = self._direct(cache, "USD", target, now)
        if first is None or second is None:
            return None
        return {
            "rate": validate_number(first["rate"] * second["rate"], positive=True),
            "updated_at": min(
                first["updated_at"],
                second["updated_at"],
                key=self._timestamp,
            ),
            "source": first["source"] if source != "USD" else second["source"],
        }

    def get_rate(self, from_currency: str, to_currency: str) -> dict:
        """Возвращает rate, updated_at и source; обновляет просроченный кэш"""
        source = get_currency(from_currency).code
        target = get_currency(to_currency).code
        with self.storage.transaction():
            return self._get_rate(source, target)

    def _get_rate(self, source: str, target: str) -> dict:
        """Читает и при необходимости обновляет кэш под блокировкой хранилища"""
        cache = self._load()
        now = datetime.now(UTC)
        result = self._find(cache, source, target, now)
        if result is not None:
            return {"from": source, "to": target, **result}
        try:
            response = self._provider()
            rates = response["rates"]
            provider_name = response["source"]
            if not isinstance(rates, dict) or not isinstance(provider_name, str):
                raise ValueError("Некорректный ответ поставщика курсов")
            for code, rate in rates.items():
                if get_currency(code).code != code:
                    raise ValueError("Некорректный код валюты в ответе")
                validate_number(rate, positive=True)
            if rates.get("USD") != 1.0 or source not in rates or target not in rates:
                raise ValueError(f"Нет курса {source}→{target}")
        except ApiRequestError:
            raise
        except Exception as error:
            raise ApiRequestError(str(error)) from error
        timestamp = now.isoformat(timespec="seconds")
        refreshed = cache.copy()
        for code, rate in rates.items():
            if code != "USD":
                refreshed[f"{code}_USD"] = {
                    "rate": rate,
                    "updated_at": timestamp,
                    "source": provider_name,
                }
        refreshed["source"] = provider_name
        refreshed["last_refresh"] = timestamp
        try:
            result = self._find(refreshed, source, target, now)
        except (ValueError, ArithmeticError) as error:
            raise ApiRequestError("Некорректный курс в ответе поставщика") from error
        if result is None:
            raise ApiRequestError(f"Нет курса {source}→{target}")
        self.storage.save("rates.json", refreshed)
        return {"from": source, "to": target, **result}
