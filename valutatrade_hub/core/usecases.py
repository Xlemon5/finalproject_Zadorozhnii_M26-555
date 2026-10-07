"""Регистрация, сессия, портфель и сделки с сохранением в JSON"""

import hashlib
import logging
import secrets
from datetime import UTC, datetime

from valutatrade_hub.core.currencies import get_currency, get_supported_codes
from valutatrade_hub.core.exceptions import InsufficientFundsError
from valutatrade_hub.core.models import Portfolio, User, Wallet
from valutatrade_hub.core.rates import RateService
from valutatrade_hub.core.utils import (
    JsonStorage,
    normalize_username,
    validate_number,
    validate_password,
)
from valutatrade_hub.decorators import log_action
from valutatrade_hub.infra.database import DatabaseManager
from valutatrade_hub.infra.settings import SettingsLoader
from valutatrade_hub.logging_config import configure_logging


class WalletService:
    """Выполняет пользовательские операции в пределах одной сессии"""

    def __init__(
        self,
        storage: JsonStorage | DatabaseManager | None = None,
        *,
        settings: SettingsLoader | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        """Подключает хранилище и сервис курсов; сессия изначально пуста"""
        self.settings = settings if settings is not None else SettingsLoader()
        self.storage = (
            storage if storage is not None else DatabaseManager(self.settings)
        )
        self.rates = RateService(self.storage, settings=self.settings)
        self.logger = logger if logger is not None else configure_logging(self.settings)
        self._current_user = None

    @property
    def current_user(self) -> User | None:
        """Возвращает текущего пользователя либо None до входа"""
        return self._current_user

    def _require_login(self) -> User:
        """Запрещает операции с портфелем до успешного входа"""
        if self.current_user is None:
            raise ValueError("Сначала выполните login")
        return self.current_user

    def _load_users(self) -> dict[int, User]:
        """Восстанавливает пользователей и проверяет уникальность имён и ID"""
        records = self.storage.load("users.json", list)
        users = {}
        usernames = set()
        try:
            for record in records:
                if not isinstance(record, dict):
                    raise ValueError
                user = User(
                    record["user_id"],
                    record["username"],
                    record["hashed_password"],
                    record["salt"],
                    datetime.fromisoformat(record["registration_date"]),
                )
                if user.user_id in users or user.username in usernames:
                    raise ValueError
                users[user.user_id] = user
                usernames.add(user.username)
        except (KeyError, ValueError, TypeError, OverflowError) as error:
            raise ValueError("Некорректные данные в users.json") from error
        return users

    def _load_portfolios(self, users: dict[int, User]) -> dict[int, Portfolio]:
        """Восстанавливает кошельки и проверяет их связь с пользователями"""
        records = self.storage.load("portfolios.json", list)
        portfolios = {}
        try:
            for record in records:
                if not isinstance(record, dict) or not isinstance(
                    record["wallets"], dict
                ):
                    raise ValueError
                user_id = record["user_id"]
                if (
                    type(user_id) is not int
                    or user_id not in users
                    or user_id in portfolios
                ):
                    raise ValueError
                wallets = {}
                for code, data in record["wallets"].items():
                    if (
                        not isinstance(data, dict)
                        or data.get("currency_code", code) != code
                    ):
                        raise ValueError
                    wallets[code] = Wallet(code, data["balance"])
                portfolios[user_id] = Portfolio(user_id, wallets, user=users[user_id])
            if set(portfolios) != set(users):
                raise ValueError
        except (KeyError, ValueError, TypeError, OverflowError) as error:
            raise ValueError("Некорректные данные в portfolios.json") from error
        return portfolios

    def register(self, username: str, password: str) -> User:
        """Регистрирует пользователя с индивидуальным salt и пустым портфелем"""
        with self.storage.transaction():
            username = normalize_username(username)
            validate_password(password)
            users = self._load_users()
            if any(user.username == username for user in users.values()):
                raise ValueError(f"Имя пользователя '{username}' уже занято")
            portfolios = self._load_portfolios(users)
            old_portfolios = [portfolio.to_dict() for portfolio in portfolios.values()]
            salt = secrets.token_hex(16)
            user = User(
                max(users, default=0) + 1,
                username,
                hashlib.sha256((password + salt).encode("utf-8")).hexdigest(),
                salt,
                datetime.now(UTC),
            )
            users[user.user_id] = user
            portfolios[user.user_id] = Portfolio(user.user_id, user=user)
            self.storage.save(
                "portfolios.json", [p.to_dict() for p in portfolios.values()]
            )
            try:
                self.storage.save("users.json", [u.to_dict() for u in users.values()])
            except OSError:
                self.storage.save("portfolios.json", old_portfolios)
                raise
            return user

    def login(self, username: str, password: str) -> User:
        """Проверяет пароль; неудачный вход оставляет сессию незалогиненной"""
        with self.storage.transaction():
            self._current_user = None
            username = normalize_username(username)
            users = self._load_users()
            user = next(
                (user for user in users.values() if user.username == username), None
            )
            if user is None:
                raise ValueError(f"Пользователь '{username}' не найден")
            if not user.verify_password(password):
                raise ValueError("Неверный пароль")
            self._current_user = user
            return user

    def logout(self) -> None:
        """Завершает текущую сессию"""
        self._current_user = None

    def _current_portfolios(self) -> tuple[dict[int, Portfolio], Portfolio]:
        """Читает актуальное состояние всех портфелей и выбирает текущий"""
        current = self._require_login()
        portfolios = self._load_portfolios(self._load_users())
        if current.user_id not in portfolios:
            self.logout()
            raise ValueError("Пользователь удалён. Сначала выполните login")
        return portfolios, portfolios[current.user_id]

    def show_portfolio(self, base_currency: str | None = None) -> dict:
        """Возвращает кошельки, оценку каждого и общую стоимость"""
        with self.storage.transaction():
            self._require_login()
            if base_currency is None:
                base_currency = self.settings.get("default_base_currency")
            base = get_currency(base_currency).code
            _, portfolio = self._current_portfolios()
            rows = []
            rates = {base: 1.0}
            for code, wallet in sorted(portfolio.wallets.items()):
                rate = (
                    self.rates.get_rate(code, base)["rate"] if wallet.balance else 0.0
                )
                rates[code] = rate if code != base else 1.0
                rows.append(
                    {
                        **wallet.get_balance_info(),
                        "value": validate_number(wallet.balance * rate),
                    }
                )
            return {
                "username": portfolio.user.username,
                "base": base,
                "wallets": rows,
                "total": portfolio.get_total_value(base, rates),
            }

    def deposit(self, amount: float) -> dict:
        """Пополняет USD-кошелёк виртуальными средствами для учебной торговли"""
        with self.storage.transaction():
            self._require_login()
            amount = validate_number(amount, positive=True)
            portfolios, portfolio = self._current_portfolios()
            wallet = portfolio.add_currency("USD")
            before = wallet.balance
            wallet.deposit(amount)
            self.storage.save(
                "portfolios.json", [p.to_dict() for p in portfolios.values()]
            )
            return {"amount": amount, "before": before, "after": wallet.balance}

    @log_action("BUY")
    def buy(self, currency_code: str, amount: float) -> dict:
        """Покупает валюту за USD; сохраняет оба баланса одним JSON-файлом"""
        return self._trade(currency_code, amount, buying=True)

    @log_action("SELL")
    def sell(self, currency_code: str, amount: float) -> dict:
        """Продаёт валюту с зачислением выручки на USD-кошелёк"""
        return self._trade(currency_code, amount, buying=False)

    def _trade(self, currency_code: str, amount: float, *, buying: bool) -> dict:
        """Проверяет сделку и сохраняет портфели только после всех расчётов"""
        with self.storage.transaction():
            self._require_login()
            code = get_currency(currency_code).code
            amount = validate_number(amount, positive=True)
            if code == "USD":
                raise ValueError(
                    "USD — расчётная валюта. Для пополнения: deposit --amount <число>"
                )
            portfolios, portfolio = self._current_portfolios()
            if not buying:
                if code not in portfolio.wallets:
                    raise InsufficientFundsError(0.0, amount, code)
                wallet = portfolio.get_wallet(code)
                # Проверка достаточности до обращения к сервису курсов
                Wallet(code, wallet.balance).withdraw(amount)
            quote = self.rates.get_rate(code, "USD")
            cost = validate_number(amount * quote["rate"], positive=True)
            usd = portfolio.add_currency("USD")
            wallet = portfolio.add_currency(code)
            before, usd_before = wallet.balance, usd.balance
            if buying:
                usd.withdraw(cost)
                wallet.deposit(amount)
            else:
                wallet.withdraw(amount)
                usd.deposit(cost)
            self.storage.save(
                "portfolios.json", [p.to_dict() for p in portfolios.values()]
            )
            return {
                "currency": code,
                "amount": amount,
                "rate": quote["rate"],
                "base": "USD",
                "cost": cost,
                "before": before,
                "after": wallet.balance,
                "usd_before": usd_before,
                "usd_after": usd.balance,
            }

    def get_rate(self, from_currency: str, to_currency: str) -> dict:
        """Получает курс без необходимости входа в систему"""
        return self.rates.get_rate(from_currency, to_currency)

    @staticmethod
    def list_currencies() -> list[str]:
        """Представляет фиатные и криптовалюты через единый интерфейс"""
        return [get_currency(code).get_display_info() for code in get_supported_codes()]
