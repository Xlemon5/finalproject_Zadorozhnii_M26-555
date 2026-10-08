"""Чтение курсов из кэша Parser Service без сетевых запросов"""

from datetime import UTC, datetime

from valutatrade_hub.core.currencies import CryptoCurrency, get_currency
from valutatrade_hub.core.utils import normalize_currency_code, validate_number
from valutatrade_hub.infra.settings import SettingsLoader


def parse_timestamp(value: str) -> datetime:
    """Приводит ISO-дату к UTC; старые даты без пояса считает UTC"""
    if not isinstance(value, str):
        raise ValueError("Некорректная дата курса")
    timestamp = datetime.fromisoformat(value)
    return (
        timestamp.replace(tzinfo=UTC)
        if timestamp.tzinfo is None
        else timestamp.astimezone(UTC)
    )


def utc_timestamp() -> str:
    """Возвращает время получения данных в формате ISO UTC"""
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def validate_pair(pair: str) -> tuple[str, str]:
    """Проверяет пару FROM_TO без изменения исходных кодов"""
    source, target = pair.split("_")
    if any(normalize_currency_code(code) != code for code in (source, target)):
        raise ValueError("Некорректная валютная пара")
    return source, target


def validate_cache(document: dict) -> dict:
    """Читает новую схему и прежний плоский формат без изменения файла"""
    try:
        if document.get("last_refresh") is not None:
            parse_timestamp(document["last_refresh"])
        pairs = document.get(
            "pairs",
            {
                key: value
                for key, value in document.items()
                if key not in {"source", "last_refresh"}
            },
        )
        if not isinstance(pairs, dict):
            raise ValueError
        result = {}
        for pair, entry in pairs.items():
            validate_pair(pair)
            if not isinstance(entry, dict):
                raise ValueError
            rate = validate_number(entry["rate"], positive=True)
            parse_timestamp(entry["updated_at"])
            source = entry.get("source", document.get("source", "cache"))
            if not isinstance(source, str) or not source.strip():
                raise ValueError
            # Старые учебные значения не становятся рыночными котировками
            if source != "Stub":
                result[pair] = {**entry, "rate": rate, "source": source}
        return {"pairs": result, "last_refresh": document.get("last_refresh")}
    except (AttributeError, KeyError, TypeError, ValueError, OverflowError) as error:
        raise ValueError("Некорректные данные в rates.json") from error


class RateService:
    """Вычисляет прямые, обратные и кросс-курсы по локальному снимку"""

    def __init__(self, storage, ttl: int | None = None, *, settings=None):
        """Подключает хранилище и настройки TTL без загрузки курсов из сети"""
        self.storage = storage
        if ttl is not None and (type(ttl) is not int or ttl <= 0):
            raise ValueError("TTL должен быть положительным целым числом")
        self._ttl_override = ttl
        self.settings = settings if settings is not None else SettingsLoader()

    @property
    def ttl(self) -> int:
        """Возвращает явный TTL либо актуальное значение из SettingsLoader"""
        return self._ttl_override or self.settings.get("rates_ttl_seconds")

    def _load(self) -> dict:
        return validate_cache(self.storage.load("rates.json", dict))

    def _direct(self, pairs, source, target, now, *, allow_stale=False):
        if source == target:
            return {
                "rate": 1.0,
                "updated_at": now.isoformat(),
                "source": "identity",
                "stale": False,
            }
        for key, inverse in (
            (f"{source}_{target}", False),
            (f"{target}_{source}", True),
        ):
            entry = pairs.get(key)
            if entry is None:
                continue
            age = (now - parse_timestamp(entry["updated_at"])).total_seconds()
            stale = not 0 <= age < self.ttl
            if allow_stale or not stale:
                return {
                    **entry,
                    "rate": validate_number(
                        1 / entry["rate"] if inverse else entry["rate"], positive=True
                    ),
                    "stale": stale,
                }
        return None

    def _find(self, pairs, source, target, now, *, allow_stale=False):
        direct = self._direct(pairs, source, target, now, allow_stale=allow_stale)
        if direct is not None:
            return direct
        first = self._direct(pairs, source, "USD", now, allow_stale=allow_stale)
        second = self._direct(pairs, "USD", target, now, allow_stale=allow_stale)
        if first is None or second is None:
            return None
        sources = sorted(
            {e["source"] for e in (first, second) if e["source"] != "identity"}
        )
        return {
            "rate": validate_number(first["rate"] * second["rate"], positive=True),
            "updated_at": min(
                first["updated_at"], second["updated_at"], key=parse_timestamp
            ),
            "source": ", ".join(sources),
            "stale": first["stale"] or second["stale"],
        }

    def get_rate(self, from_currency: str, to_currency: str) -> dict:
        """Отказывает при отсутствии свежей пары; кэш обновляет только парсер"""
        source, target = (
            get_currency(from_currency).code,
            get_currency(to_currency).code,
        )
        cache = self._load()
        now = datetime.now(UTC)
        result = self._find(cache["pairs"], source, target, now)
        if result is None:
            old = self._find(cache["pairs"], source, target, now, allow_stale=True)
            reason = "устарел" if old is not None else "недоступен"
            raise ValueError(
                f"Курс {source}→{target} {reason}. Выполните 'update-rates'"
            )
        return {
            "from": source,
            "to": target,
            **result,
            "inverse_rate": validate_number(1 / result["rate"], positive=True),
        }

    def show_rates(self, currency=None, top=None, base=None) -> dict:
        """Возвращает таблицу кэша, включая явно помеченные устаревшие записи"""
        base = get_currency(base or self.settings.get("default_base_currency")).code
        if top is not None and (type(top) is not int or top <= 0):
            raise ValueError("--top должен быть положительным целым числом")
        code = normalize_currency_code(currency) if currency is not None else None
        cache = self._load()
        pairs = cache["pairs"]
        if not pairs:
            raise ValueError(
                "Локальный кэш курсов пуст. "
                "Выполните 'update-rates', чтобы загрузить данные"
            )
        codes = {part for pair in pairs for part in pair.split("_")} - {base}
        if code is not None:
            if code not in codes and code != base:
                raise ValueError(f"Курс для '{code}' не найден в кеше")
            codes = {code}
        rows = []
        now = datetime.now(UTC)
        for item in sorted(codes):
            currency_object = get_currency(item)
            if top is not None and not isinstance(currency_object, CryptoCurrency):
                continue
            quote = self._find(pairs, item, base, now, allow_stale=True)
            if quote is None:
                raise ValueError(
                    f"Курс {item}→{base} не найден в кеше. Выполните 'update-rates'"
                )
            rows.append({"currency": item, "pair": f"{item}_{base}", **quote})
        if top is not None:
            rows = sorted(rows, key=lambda row: (-row["rate"], row["currency"]))[:top]
        return {"base": base, "rates": rows, "last_refresh": cache["last_refresh"]}
