"""Вспомогательные объекты для изолированных проверок"""

import logging


def silent_logger() -> logging.Logger:
    """Отключает запись журнала в тестах, не проверяющих логирование"""
    logger = logging.Logger("wallet-tests")
    logger.addHandler(logging.NullHandler())
    return logger
