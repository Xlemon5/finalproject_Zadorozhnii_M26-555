"""Инварианты паролей, балансов и владельца портфеля"""

import hashlib
import unittest
from datetime import UTC, datetime

from valutatrade_hub.core.models import Portfolio, User, Wallet


def make_user() -> User:
    """Создаёт пользователя с известным паролем без обращения к файлам"""
    return User(
        1,
        "alice",
        hashlib.sha256(b"1234salt").hexdigest(),
        "salt",
        datetime.now(UTC),
    )


class UserTests(unittest.TestCase):
    def test_password_change_invalidates_old_password(self):
        user = make_user()
        self.assertTrue(user.verify_password("1234"))
        user.change_password("новый пароль")
        self.assertTrue(user.verify_password("новый пароль"))
        self.assertFalse(user.verify_password("1234"))
        self.assertFalse(user.verify_password(None))
        old_hash = user.hashed_password
        with self.assertRaisesRegex(ValueError, "не короче 4"):
            user.change_password("123")
        self.assertEqual(user.hashed_password, old_hash)

    def test_info_has_no_secrets_and_identity_is_read_only(self):
        user = make_user()
        self.assertEqual(
            set(user.get_user_info()), {"user_id", "username", "registration_date"}
        )
        self.assertNotIn("1234", str(user.to_dict()))
        for attribute in ("user_id", "salt", "hashed_password", "registration_date"):
            with self.subTest(attribute=attribute), self.assertRaises(AttributeError):
                setattr(user, attribute, "replacement")
        with self.assertRaises(ValueError):
            user.username = "   "
        self.assertEqual(user.username, "alice")


class WalletTests(unittest.TestCase):
    def test_deposit_withdraw_and_full_withdrawal(self):
        wallet = Wallet(" usd ", 50)
        wallet.deposit(20)
        wallet.withdraw(10)
        self.assertEqual(
            wallet.get_balance_info(), {"currency_code": "USD", "balance": 60.0}
        )
        wallet.withdraw(60)
        self.assertEqual(wallet.balance, 0.0)

    def test_bad_amounts_never_change_balance(self):
        for amount in (
            0,
            -1,
            True,
            "10",
            None,
            float("nan"),
            float("inf"),
            -float("inf"),
            10**1000,
        ):
            for method in ("deposit", "withdraw"):
                with self.subTest(amount=str(amount)[:20], method=method):
                    wallet = Wallet("BTC", 2)
                    with self.assertRaises(ValueError):
                        getattr(wallet, method)(amount)
                    self.assertEqual(wallet.balance, 2.0)

    def test_bad_balances_and_insufficient_funds(self):
        wallet = Wallet("USD", 5)
        for balance in (-1, True, "5", None, float("nan"), float("inf")):
            with self.subTest(balance=balance), self.assertRaises(ValueError):
                wallet.balance = balance
            self.assertEqual(wallet.balance, 5.0)
        with self.assertRaisesRegex(ValueError, "Недостаточно средств"):
            wallet.withdraw(6)
        self.assertEqual(wallet.balance, 5.0)

    def test_overflow_and_unrepresentable_changes_are_rejected(self):
        wallet = Wallet("USD", 1e308)
        for amount in (1e308, 1e-30):
            with self.subTest(amount=amount), self.assertRaises(ValueError):
                wallet.deposit(amount)
            self.assertEqual(wallet.balance, 1e308)


class PortfolioTests(unittest.TestCase):
    def test_owner_and_wallet_mapping_cannot_be_replaced_from_outside(self):
        user = make_user()
        wallets = {"USD": Wallet("USD", 100)}
        portfolio = Portfolio(1, wallets, user=user)
        wallets.clear()
        snapshot = portfolio.wallets
        snapshot.clear()
        self.assertEqual(portfolio.get_wallet("USD").balance, 100)
        self.assertIs(portfolio.user, user)
        with self.assertRaises(AttributeError):
            portfolio.user = make_user()
        with self.assertRaises(ValueError):
            Portfolio(2, user=user)

    def test_adding_existing_currency_keeps_balance(self):
        portfolio = Portfolio(1, user=make_user())
        wallet = portfolio.add_currency("btc")
        wallet.deposit(0.05)
        self.assertIs(portfolio.add_currency("BTC"), wallet)
        self.assertEqual(wallet.balance, 0.05)
        for code in ("", "../USD", "   ", None):
            with self.subTest(code=code), self.assertRaises(ValueError):
                portfolio.add_currency(code)

    def test_total_converts_all_currencies_and_does_not_mutate_them(self):
        portfolio = Portfolio(
            1, {"USD": Wallet("USD", 100), "EUR": Wallet("EUR", 200)}, user=make_user()
        )
        self.assertAlmostEqual(portfolio.get_total_value(), 100 + 200 * 1.0786)
        self.assertAlmostEqual(portfolio.get_total_value("EUR"), 100 / 1.0786 + 200)
        self.assertEqual(portfolio.get_wallet("EUR").balance, 200)
        with self.assertRaisesRegex(ValueError, "Неизвестная валюта"):
            portfolio.get_total_value("ABC")
        portfolio.add_currency("ETH").deposit(1)
        with self.assertRaisesRegex(ValueError, "Не удалось получить курс"):
            portfolio.get_total_value(exchange_rates={"USD": 1, "EUR": 1.0786})
