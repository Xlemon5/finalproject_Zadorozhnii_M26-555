"""Единый контракт курсов: локальный кэш и учебная заглушка"""

from datetime import UTC, datetime

from valutatrade_hub.core.utils import (
    EXCHANGE_RATES,
    RATE_TTL_SECONDS,
    JsonStorage,
    normalize_currency_code,
    validate_number,
)


class RateUnavailableError(ValueError):
    """Нет свежего курса и заглушка не поддерживает валютную пару"""


class RateService:
    """Читает прямые, обратные и кросс-курсы с ограниченным сроком жизни"""

    def __init__(self, storage: JsonStorage, ttl: int = RATE_TTL_SECONDS) -> None:
        """Настраивает хранилище и срок свежести в секундах"""
        self.storage = storage
        self.ttl = ttl

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
        """Возвращает валюты заглушки и локального кэша"""
        result = set(EXCHANGE_RATES)
        for pair in self._load():
            if pair not in {"source", "last_refresh"}:
                result.update(pair.split("_"))
        return result

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
        source = normalize_currency_code(from_currency)
        target = normalize_currency_code(to_currency)
        cache = self._load()
        known = set(EXCHANGE_RATES)
        for pair in cache:
            if pair not in {"source", "last_refresh"}:
                known.update(pair.split("_"))
        now = datetime.now(UTC)
        result = None
        if source in known and target in known:
            result = self._find(cache, source, target, now)
        if result is not None:
            return {"from": source, "to": target, **result}
        if source not in EXCHANGE_RATES or target not in EXCHANGE_RATES:
            raise RateUnavailableError(
                f"Курс {source}→{target} недоступен. Повторите попытку позже."
            )
        timestamp = now.isoformat(timespec="seconds")
        for code, rate in EXCHANGE_RATES.items():
            if code != "USD":
                cache[f"{code}_USD"] = {
                    "rate": rate,
                    "updated_at": timestamp,
                    "source": "Stub",
                }
        cache["source"] = "Stub"
        cache["last_refresh"] = timestamp
        self.storage.save("rates.json", cache)
        result = self._find(cache, source, target, now)
        return {"from": source, "to": target, **result}
