"""Контракт валют, фабрика и доменные ошибки"""

import unittest

from valutatrade_hub.core.currencies import (
    CryptoCurrency,
    Currency,
    FiatCurrency,
    get_currency,
)
from valutatrade_hub.core.exceptions import (
    CurrencyNotFoundError,
    InsufficientFundsError,
)
from valutatrade_hub.core.models import Wallet


class CurrencyTests(unittest.TestCase):
    def test_abstract_base_cannot_be_instantiated(self):
        with self.assertRaises(TypeError):
            Currency("US Dollar", "USD")

    def test_polymorphic_display_matches_assignment(self):
        currencies = [
            FiatCurrency("US Dollar", "USD", "United States"),
            CryptoCurrency("Bitcoin", "BTC", "SHA-256", 1.12e12),
        ]
        self.assertEqual(
            [currency.get_display_info() for currency in currencies],
            [
                "[FIAT] USD — US Dollar (Issuing: United States)",
                "[CRYPTO] BTC — Bitcoin (Algo: SHA-256, MCAP: 1.12e12)",
            ],
        )

    def test_code_and_name_invariants_survive_assignment(self):
        currency = get_currency("USD")
        for code in ("U", "ABCDEF", "usd", " USD", "US D", "РУБ", "12", None):
            with self.subTest(code=code), self.assertRaises(ValueError):
                currency.code = code
            self.assertEqual(currency.code, "USD")
        for name in ("", "  ", None, 123):
            with self.subTest(name=name), self.assertRaises(ValueError):
                currency.name = name
            self.assertEqual(currency.name, "US Dollar")

    def test_subclass_fields_reject_invalid_values(self):
        crypto = get_currency("BTC")
        for value in (-1, True, "1", float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                crypto.market_cap = value
            self.assertEqual(crypto.market_cap, 1.12e12)
        with self.assertRaises(ValueError):
            crypto.algorithm = " "
        with self.assertRaises(ValueError):
            FiatCurrency("Euro", "EUR", "")

    def test_factory_normalizes_input_and_does_not_expose_registry_objects(self):
        first = get_currency(" usd ")
        first.name = "Changed"
        first.code = "ABC"
        second = get_currency("USD")
        self.assertIsNot(first, second)
        self.assertEqual(second.code, "USD")
        self.assertEqual(second.name, "US Dollar")

    def test_unknown_currency_is_rejected_by_factory_and_wallet(self):
        for factory in (get_currency, Wallet):
            with (
                self.subTest(factory=factory),
                self.assertRaises(CurrencyNotFoundError) as result,
            ):
                factory("ABC")
            self.assertEqual(str(result.exception), "Неизвестная валюта 'ABC'")

    def test_insufficient_funds_exposes_values_without_changing_balance(self):
        wallet = Wallet("BTC", 0.04)
        with self.assertRaises(InsufficientFundsError) as result:
            wallet.withdraw(0.05)
        self.assertEqual(result.exception.available, 0.04)
        self.assertEqual(result.exception.required, 0.05)
        self.assertEqual(result.exception.code, "BTC")
        self.assertEqual(
            str(result.exception),
            "Недостаточно средств: доступно 0.0400 BTC, требуется 0.0500 BTC",
        )
        self.assertEqual(wallet.balance, 0.04)
