"""Координация API-клиентов, журналирование и сохранение результатов"""

from valutatrade_hub.core.exceptions import ApiRequestError
from valutatrade_hub.core.rates import parse_timestamp, utc_timestamp, validate_pair
from valutatrade_hub.logging_config import configure_logging
from valutatrade_hub.parser_service.api_clients import (
    CoinGeckoClient,
    ExchangeRateApiClient,
)
from valutatrade_hub.parser_service.config import ParserConfig
from valutatrade_hub.parser_service.storage import ParserStorage, validate_record


class RatesUpdater:
    """Продолжает обновление остальных источников при отказе одного клиента"""

    def __init__(self, clients, storage, *, logger=None):
        """Принимает API-клиенты, хранилище и необязательный журнал"""
        self.clients = list(clients)
        self.storage = storage
        self.logger = logger if logger is not None else configure_logging(parser=True)

    def run_update(self) -> dict:
        """Сохраняет успешные ответы и возвращает число курсов и ошибки источников"""
        self.logger.info(
            {
                "event": "update_started",
                "sources": [client.source for client in self.clients],
            }
        )
        records, errors, successes = [], {}, {}
        for client in self.clients:
            self.logger.info({"event": "fetch_started", "source": client.source})
            try:
                rates = client.fetch_rates()
                if not isinstance(rates, dict) or not rates:
                    raise ValueError("Пустой ответ")
                timestamp = (
                    parse_timestamp(client.fetched_at or utc_timestamp())
                    .isoformat()
                    .replace("+00:00", "Z")
                )
                batch = []
                for pair, rate in rates.items():
                    source, target = validate_pair(pair)
                    batch.append(
                        validate_record(
                            {
                                "id": f"{pair}_{timestamp}",
                                "from_currency": source,
                                "to_currency": target,
                                "rate": rate,
                                "timestamp": timestamp,
                                "source": client.source,
                                "meta": client.last_metadata.get(pair, {}),
                            }
                        )
                    )
            except (ApiRequestError, ValueError, TypeError, AttributeError) as error:
                message = (
                    str(error)
                    if isinstance(error, ApiRequestError)
                    else "Некорректный ответ клиента"
                )
                errors[client.source] = message
                self.logger.error(
                    {"event": "fetch_failed", "source": client.source, "error": message}
                )
                continue
            records.extend(batch)
            successes[client.source] = len(batch)
            self.logger.info(
                {
                    "event": "fetch_succeeded",
                    "source": client.source,
                    "count": len(batch),
                }
            )
        if not records:
            self.logger.error({"event": "update_failed", "errors": errors})
            raise ApiRequestError(
                "Не получено ни одного курса. " + "; ".join(errors.values())
            )
        try:
            result = self.storage.save(records, last_refresh=utc_timestamp())
        except (OSError, ValueError) as error:
            self.logger.error(
                {"event": "storage_failed", "error_type": type(error).__name__}
            )
            raise
        report = {**result, "errors": errors, "sources": successes}
        self.logger.info({"event": "update_completed", **report})
        return report


def create_updater(
    *, source=None, settings=None, storage=None, config=None, logger=None
):
    """Собирает парсер для CLI и отдельного процесса с теми же настройками"""
    if source not in (None, "coingecko", "exchangerate"):
        raise ValueError("--source: выберите coingecko или exchangerate")
    config = (
        config if config is not None else ParserConfig.from_settings(settings, storage)
    )
    clients = []
    if source in (None, "coingecko"):
        clients.append(CoinGeckoClient(config))
    if source in (None, "exchangerate"):
        clients.append(ExchangeRateApiClient(config))
    return RatesUpdater(
        clients,
        ParserStorage(config),
        logger=logger
        if logger is not None
        else configure_logging(settings, parser=True),
    )


def format_update(report: dict) -> str:
    """Формирует общий итог для двух способов запуска парсера"""
    lines = [
        f"{source}: получено курсов — {count}"
        for source, count in report["sources"].items()
    ]
    lines.extend(f"{source}: {error}" for source, error in report["errors"].items())
    status = (
        "Обновление завершено с ошибками"
        if report["errors"]
        else "Обновление завершено успешно"
    )
    lines.append(
        f"{status}. Обновлено курсов: {report['updated']}. "
        f"Последнее обновление: {report['last_refresh']}"
    )
    if report["errors"]:
        lines.append("Подробности — в журнале parser.log")
    return "\n".join(lines)
