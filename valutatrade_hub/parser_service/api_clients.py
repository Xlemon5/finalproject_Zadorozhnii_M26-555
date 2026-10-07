"""Клиенты внешних API с единым контрактом валютных пар"""

from abc import ABC, abstractmethod
from datetime import UTC, datetime
from time import perf_counter
from urllib.parse import quote

import requests

from valutatrade_hub.core.exceptions import ApiRequestError
from valutatrade_hub.core.rates import utc_timestamp
from valutatrade_hub.core.utils import validate_number
from valutatrade_hub.parser_service.config import ParserConfig


class BaseApiClient(ABC):
    """Возвращает числовые курсы и сохраняет метаданные последнего ответа"""

    source = "API"

    def __init__(self, config: ParserConfig):
        self.config = config
        self.fetched_at = None
        self.last_metadata = {}

    @abstractmethod
    def fetch_rates(self) -> dict[str, float]:
        """Возвращает словарь пар вида BTC_USD после валидации ответа"""

    def _request(self, url, *, params=None, headers=None):
        self.fetched_at = None
        self.last_metadata = {}
        started = perf_counter()
        try:
            response = requests.get(
                url,
                params=params,
                headers=headers,
                timeout=self.config.REQUEST_TIMEOUT,
                allow_redirects=False,
            )
        except requests.Timeout:
            raise ApiRequestError(f"{self.source}: превышен таймаут запроса") from None
        except requests.RequestException:
            # Текст requests может содержать URL с ключом, поэтому его не выводим
            raise ApiRequestError(f"{self.source}: ошибка сети") from None
        try:
            if response.status_code != 200:
                reason = {
                    401: "проверьте API-ключ в переменной окружения",
                    403: "доступ запрещен; проверьте API-ключ и тариф",
                    429: "лимит запросов исчерпан; повторите позже",
                }.get(response.status_code, "сервис недоступен")
                raise ApiRequestError(
                    f"{self.source}: HTTP {response.status_code}, {reason}"
                )
            try:
                payload = response.json()
            except ValueError:
                raise ApiRequestError(
                    f"{self.source}: ответ не является JSON"
                ) from None
            if not isinstance(payload, dict):
                raise ApiRequestError(f"{self.source}: некорректная структура ответа")
            self.fetched_at = utc_timestamp()
            metadata = {
                "request_ms": round((perf_counter() - started) * 1000, 3),
                "status_code": response.status_code,
            }
            if response.headers.get("ETag"):
                metadata["etag"] = response.headers["ETag"]
            return payload, metadata
        finally:
            response.close()

    @staticmethod
    def _provider_time(value):
        value = validate_number(value, positive=True)
        return datetime.fromtimestamp(value, UTC).isoformat().replace("+00:00", "Z")


class CoinGeckoClient(BaseApiClient):
    """Получает криптовалюты одним запросом по CoinGecko ID"""

    source = "CoinGecko"

    def fetch_rates(self) -> dict[str, float]:
        headers = {}
        if self.config.COINGECKO_API_KEY:
            headers["x-cg-demo-api-key"] = self.config.COINGECKO_API_KEY
        ids = [
            self.config.CRYPTO_ID_MAP[code] for code in self.config.CRYPTO_CURRENCIES
        ]
        if not ids:
            raise ApiRequestError("CoinGecko: список криптовалют пуст")
        payload, metadata = self._request(
            self.config.COINGECKO_URL,
            params={
                "ids": ",".join(ids),
                "vs_currencies": "usd",
                "include_last_updated_at": "true",
            },
            headers=headers,
        )
        rates, details = {}, {}
        try:
            for code, raw_id in zip(self.config.CRYPTO_CURRENCIES, ids, strict=True):
                entry = payload[raw_id]
                pair = f"{code}_USD"
                rates[pair] = validate_number(entry["usd"], positive=True)
                details[pair] = {**metadata, "raw_id": raw_id}
                if "last_updated_at" in entry:
                    details[pair]["provider_updated_at"] = self._provider_time(
                        entry["last_updated_at"]
                    )
        except (KeyError, TypeError, ValueError, OverflowError, OSError):
            raise ApiRequestError(
                "CoinGecko: отсутствует или некорректен курс запрошенной валюты"
            ) from None
        self.last_metadata = details
        return rates


class ExchangeRateApiClient(BaseApiClient):
    """Преобразует котировки USD→валюта в валюту→USD"""

    source = "ExchangeRate-API"

    def fetch_rates(self) -> dict[str, float]:
        if not self.config.EXCHANGERATE_API_KEY:
            raise ApiRequestError(
                "ExchangeRate-API: задайте EXCHANGERATE_API_KEY в окружении"
            )
        key = quote(self.config.EXCHANGERATE_API_KEY, safe="")
        url = (
            f"{self.config.EXCHANGERATE_API_URL.rstrip('/')}/{key}/latest/"
            f"{self.config.BASE_CURRENCY}"
        )
        payload, metadata = self._request(url)
        if payload.get("result") != "success":
            error_type = payload.get("error-type")
            if not isinstance(error_type, str):
                error_type = "unknown"
            reason = {
                "invalid-key": "неверный API-ключ",
                "inactive-account": "учетная запись не активирована",
                "quota-reached": "лимит запросов исчерпан; повторите позже",
                "unsupported-code": "базовая валюта не поддерживается",
                "malformed-request": "неверный формат запроса",
            }.get(error_type, "сервис отклонил запрос")
            raise ApiRequestError(f"{self.source}: {reason}")
        try:
            if payload["base_code"] != self.config.BASE_CURRENCY:
                raise ValueError
            conversion = payload.get("conversion_rates", payload.get("rates"))
            if not isinstance(conversion, dict):
                raise ValueError
            if "time_last_update_unix" in payload:
                metadata["provider_updated_at"] = self._provider_time(
                    payload["time_last_update_unix"]
                )
            rates = {}
            for code in self.config.FIAT_CURRENCIES:
                if code != "USD":
                    rate = validate_number(conversion[code], positive=True)
                    rates[f"{code}_USD"] = validate_number(1 / rate, positive=True)
            if not rates:
                raise ValueError
        except (KeyError, TypeError, ValueError, OverflowError, OSError):
            raise ApiRequestError(
                "ExchangeRate-API: отсутствует или некорректен курс запрошенной валюты"
            ) from None
        self.last_metadata = {pair: dict(metadata) for pair in rates}
        return rates
