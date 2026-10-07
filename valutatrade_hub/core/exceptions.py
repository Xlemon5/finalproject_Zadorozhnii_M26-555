"""Доменные исключения с сообщениями для консольного интерфейса"""


class InsufficientFundsError(ValueError):
    """В кошельке недостаточно средств для запрошенного списания"""

    def __init__(self, available: float, required: float, code: str) -> None:
        """Сохраняет суммы и код валюты для сообщения и журнала"""
        self.available = available
        self.required = required
        self.code = code
        super().__init__(
            f"Недостаточно средств: доступно {available:.4f} {code}, "
            f"требуется {required:.4f} {code}"
        )


class CurrencyNotFoundError(ValueError):
    """Код валюты отсутствует в реестре"""

    def __init__(self, code: str) -> None:
        """Сохраняет неизвестный код валюты"""
        self.code = code
        super().__init__(f"Неизвестная валюта '{code}'")


class ApiRequestError(RuntimeError):
    """Поставщик курсов не смог вернуть необходимые данные"""

    def __init__(self, reason: str) -> None:
        """Сохраняет причину сбоя поставщика курсов"""
        self.reason = reason
        super().__init__(f"Ошибка при обращении к внешнему API: {reason}")
