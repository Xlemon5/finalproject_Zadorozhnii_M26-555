"""Вспомогательные объекты для изолированных проверок"""

import logging
from datetime import UTC, datetime

from valutatrade_hub.core.utils import EXCHANGE_RATES


def seed_rates(storage):
    """Создает явные тестовые данные без сетевых запросов"""
    timestamp = datetime.now(UTC).isoformat()
    storage.save(
        "rates.json",
        {
            "pairs": {
                f"{code}_USD": {
                    "rate": rate,
                    "updated_at": timestamp,
                    "source": "TestFixture",
                }
                for code, rate in {**EXCHANGE_RATES, "GBP": 1.25, "SOL": 145.12}.items()
                if code != "USD"
            },
            "last_refresh": timestamp,
        },
    )


def silent_logger() -> logging.Logger:
    """Отключает запись журнала в тестах, не проверяющих логирование"""
    logger = logging.Logger("wallet-tests")
    logger.addHandler(logging.NullHandler())
    return logger
