"""Пользователь, отдельный кошелёк и портфель пользователя"""

import hashlib
import hmac
import math
from datetime import datetime

from valutatrade_hub.core.utils import (
    EXCHANGE_RATES,
    normalize_currency_code,
    normalize_username,
    validate_number,
    validate_password,
    validate_user_id,
)


class User:
    """Пользователь с хешем пароля, параметром salt и датой регистрации"""

    def __init__(
        self,
        user_id: int,
        username: str,
        hashed_password: str,
        salt: str,
        registration_date: datetime,
    ) -> None:
        """Создаёт пользователя из полей, сохранённых в хранилище"""
        self._user_id = validate_user_id(user_id)
        self.username = username
        if (
            not isinstance(hashed_password, str)
            or len(hashed_password) != 64
            or any(character not in "0123456789abcdef" for character in hashed_password)
        ):
            raise ValueError("Некорректный хеш пароля")
        if not isinstance(salt, str) or not salt:
            raise ValueError("Параметр salt должен быть непустой строкой")
        if not isinstance(registration_date, datetime):
            raise ValueError("Дата регистрации должна иметь тип datetime")
        self._hashed_password = hashed_password
        self._salt = salt
        self._registration_date = registration_date

    @property
    def user_id(self) -> int:
        """Возвращает неизменяемый идентификатор"""
        return self._user_id

    @property
    def username(self) -> str:
        """Возвращает имя пользователя"""
        return self._username

    @username.setter
    def username(self, value: str) -> None:
        """Проверяет и устанавливает непустое имя"""
        self._username = normalize_username(value)

    @property
    def hashed_password(self) -> str:
        """Возвращает хеш для сохранения в хранилище"""
        return self._hashed_password

    @property
    def salt(self) -> str:
        """Возвращает индивидуальный случайный параметр salt"""
        return self._salt

    @property
    def registration_date(self) -> datetime:
        """Возвращает дату регистрации"""
        return self._registration_date

    def get_user_info(self) -> dict:
        """Возвращает публичную информацию без пароля, хеша и параметра salt"""
        return {
            "user_id": self.user_id,
            "username": self.username,
            "registration_date": self.registration_date.isoformat(),
        }

    def change_password(self, new_password: str) -> None:
        """Проверяет пароль и сохраняет SHA-256 от строки password + salt"""
        validate_password(new_password)
        self._hashed_password = hashlib.sha256(
            (new_password + self.salt).encode("utf-8")
        ).hexdigest()

    def verify_password(self, password: str) -> bool:
        """Сравнивает хеш введённого пароля с сохранённым"""
        if not isinstance(password, str):
            return False
        candidate = hashlib.sha256((password + self.salt).encode("utf-8")).hexdigest()
        return hmac.compare_digest(candidate, self.hashed_password)

    def to_dict(self) -> dict:
        """Возвращает запись для users.json, включая хеш и параметр salt"""
        return {
            **self.get_user_info(),
            "hashed_password": self.hashed_password,
            "salt": self.salt,
        }


class Wallet:
    """Баланс одной валюты с проверками пополнения и снятия"""

    def __init__(self, currency_code: str, balance: float = 0.0) -> None:
        """Создаёт кошелёк с неотрицательным балансом"""
        self._currency_code = normalize_currency_code(currency_code)
        self.balance = balance

    @property
    def currency_code(self) -> str:
        """Возвращает код валюты, неизменяемый после создания кошелька"""
        return self._currency_code

    @property
    def balance(self) -> float:
        """Возвращает текущий баланс"""
        return self._balance

    @balance.setter
    def balance(self, value: float) -> None:
        """Запрещает отрицательные, бесконечные и нечисловые значения"""
        self._balance = validate_number(value)

    def deposit(self, amount: float) -> None:
        """Пополняет кошелёк положительной суммой"""
        amount = validate_number(amount, positive=True)
        result = validate_number(self.balance + amount)
        if result == self.balance:
            raise ValueError("Сумма слишком мала для изменения текущего баланса")
        self.balance = result

    def withdraw(self, amount: float) -> None:
        """Списывает сумму, если в кошельке достаточно средств"""
        amount = validate_number(amount, positive=True)
        if amount > self.balance:
            raise ValueError(
                f"Недостаточно средств: доступно {self.balance:.4f} "
                f"{self.currency_code}, требуется {amount:.4f} {self.currency_code}"
            )
        result = self.balance - amount
        if result == self.balance:
            raise ValueError("Сумма слишком мала для изменения текущего баланса")
        self.balance = result

    def get_balance_info(self) -> dict:
        """Возвращает код валюты и текущий баланс"""
        return {"currency_code": self.currency_code, "balance": self.balance}


class Portfolio:
    """Кошельки одного пользователя с общей оценкой стоимости"""

    def __init__(
        self,
        user_id: int,
        wallets: dict[str, Wallet] | None = None,
        *,
        user: User,
    ) -> None:
        """Принимает ID, кошельки и объект владельца с тем же ID"""
        self._user_id = validate_user_id(user_id)
        if not isinstance(user, User) or user.user_id != self._user_id:
            raise ValueError("Пользователь не соответствует владельцу портфеля")
        self._user = user
        if wallets is not None and not isinstance(wallets, dict):
            raise ValueError("Кошельки должны быть словарём")
        self._wallets = {}
        for code, wallet in (wallets or {}).items():
            if not isinstance(wallet, Wallet) or code != wallet.currency_code:
                raise ValueError("Код валюты не соответствует кошельку")
            self._wallets[code] = wallet

    @property
    def user_id(self) -> int:
        """Возвращает идентификатор владельца"""
        return self._user_id

    @property
    def user(self) -> User:
        """Возвращает владельца без возможности заменить его"""
        return self._user

    @property
    def wallets(self) -> dict[str, Wallet]:
        """Возвращает поверхностную копию словаря кошельков"""
        return self._wallets.copy()

    def add_currency(self, currency_code: str) -> Wallet:
        """Добавляет пустой кошелёк; существующий возвращает без изменений"""
        code = normalize_currency_code(currency_code)
        if code not in self._wallets:
            self._wallets[code] = Wallet(code)
        return self._wallets[code]

    def get_wallet(self, currency_code: str) -> Wallet:
        """Возвращает кошелёк либо сообщает о его отсутствии"""
        code = normalize_currency_code(currency_code)
        if code not in self._wallets:
            raise ValueError(
                f"У вас нет кошелька '{code}'. Добавьте валюту: "
                "она создаётся автоматически при покупке"
            )
        return self._wallets[code]

    def get_total_value(
        self,
        base_currency: str = "USD",
        exchange_rates: dict[str, float] | None = None,
    ) -> float:
        """Суммирует балансы по курсам к общей валюте; по умолчанию к USD"""
        base = normalize_currency_code(base_currency)
        rates = EXCHANGE_RATES if exchange_rates is None else exchange_rates
        if base not in rates:
            raise ValueError(f"Неизвестная базовая валюта '{base}'")
        base_rate = validate_number(rates[base], positive=True)
        values = []
        for code, wallet in self._wallets.items():
            if wallet.balance == 0:
                continue
            if code == base:
                values.append(wallet.balance)
            else:
                if code not in rates:
                    raise ValueError(f"Не удалось получить курс для {code}→{base}")
                rate = validate_number(rates[code], positive=True)
                values.append(validate_number(wallet.balance * rate / base_rate))
        try:
            return validate_number(math.fsum(values))
        except OverflowError as error:
            raise ValueError("Общая стоимость портфеля слишком велика") from error

    def to_dict(self) -> dict:
        """Возвращает запись портфеля для portfolios.json"""
        return {
            "user_id": self.user_id,
            "wallets": {
                code: {"balance": wallet.balance}
                for code, wallet in self._wallets.items()
            },
        }
