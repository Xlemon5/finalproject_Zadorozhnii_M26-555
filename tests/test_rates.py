"""Свежесть курсов, кросс-курсы и обработка повреждённого кэша"""

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from valutatrade_hub.core.currencies import CURRENCY_REGISTRY, CryptoCurrency
from valutatrade_hub.core.exceptions import ApiRequestError, CurrencyNotFoundError
from valutatrade_hub.core.rates import RateService
from valutatrade_hub.core.utils import JsonStorage


class RateTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.storage = JsonStorage(self.directory.name)
        self.service = RateService(self.storage)

    def entry(self, rate, *, age=0):
        return {
            "rate": rate,
            "updated_at": (datetime.now(UTC) - timedelta(seconds=age)).isoformat(),
        }

    def test_missing_cache_uses_stub_and_second_read_does_not_write(self):
        result = self.service.get_rate("btc", "usd")
        self.assertEqual(result["rate"], 59337.21)
        self.assertEqual(result["source"], "Stub")
        with patch.object(self.storage, "save") as save:
            self.assertEqual(self.service.get_rate("BTC", "USD"), result)
        save.assert_not_called()

    def test_fresh_cache_has_priority_over_stub(self):
        self.storage.save("rates.json", {"BTC_USD": self.entry(70000)})
        with patch.object(self.storage, "save") as save:
            self.assertEqual(self.service.get_rate("BTC", "USD")["rate"], 70000)
        save.assert_not_called()

    def test_inverse_cross_and_same_currency(self):
        self.storage.save(
            "rates.json",
            {
                "BTC_USD": self.entry(60000),
                "EUR_USD": self.entry(1.2),
            },
        )
        self.assertAlmostEqual(self.service.get_rate("USD", "BTC")["rate"], 1 / 60000)
        self.assertAlmostEqual(self.service.get_rate("BTC", "EUR")["rate"], 50000)
        self.assertEqual(self.service.get_rate("EUR", "EUR")["rate"], 1.0)

    def test_expired_or_future_cache_is_refreshed(self):
        for age in (301, -100):
            with self.subTest(age=age):
                self.storage.save("rates.json", {"BTC_USD": self.entry(90000, age=age)})
                result = self.service.get_rate("BTC", "USD")
                self.assertEqual(result["rate"], 59337.21)
                self.assertEqual(
                    self.storage.load("rates.json", dict)["source"], "Stub"
                )

    def test_ttl_boundary_is_expired(self):
        now = datetime(2026, 10, 7, 12, tzinfo=UTC)
        for age, expected in ((299, 70000), (300, 59337.21)):
            with self.subTest(age=age):
                self.storage.save(
                    "rates.json",
                    {
                        "BTC_USD": {
                            "rate": 70000,
                            "updated_at": (now - timedelta(seconds=age)).isoformat(),
                        }
                    },
                )
                with patch(
                    "valutatrade_hub.core.rates.datetime", wraps=datetime
                ) as clock:
                    clock.now.return_value = now
                    self.assertEqual(
                        self.service.get_rate("BTC", "USD")["rate"], expected
                    )

    def test_custom_currency_works_only_while_cache_is_fresh(self):
        registry = patch.dict(
            CURRENCY_REGISTRY,
            {
                "DOGE": CryptoCurrency("Dogecoin", "DOGE", "Scrypt", 1e9),
            },
        )
        registry.start()
        self.addCleanup(registry.stop)
        self.storage.save("rates.json", {"DOGE_USD": self.entry(0.2)})
        self.assertEqual(self.service.get_rate("DOGE", "USD")["rate"], 0.2)
        self.storage.save("rates.json", {"DOGE_USD": self.entry(0.2, age=301)})
        before = (Path(self.directory.name) / "rates.json").read_bytes()
        with self.assertRaises(ApiRequestError):
            self.service.get_rate("DOGE", "USD")
        self.assertEqual(
            (Path(self.directory.name) / "rates.json").read_bytes(), before
        )

    def test_unknown_codes_and_invalid_syntax_are_rejected(self):
        for source, target in (("ABC", "USD"), ("ABC", "ABC")):
            with self.subTest(source=source), self.assertRaises(CurrencyNotFoundError):
                self.service.get_rate(source, target)
        for code in ("", "../USD", None):
            with self.subTest(code=code), self.assertRaises(ValueError):
                self.service.get_rate(code, "USD")

    def test_malformed_cache_is_preserved(self):
        path = Path(self.directory.name) / "rates.json"
        for invalid in (
            "not json",
            "[]",
            '{"BTC_USD": {"rate": -1, "updated_at": "2025-01-01"}}',
            '{"BTC_USD": {"rate": 1, "updated_at": "invalid"}}',
            '{"BTC_USD": {"rate": NaN, "updated_at": "2025-01-01"}}',
        ):
            path.write_text(invalid)
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.service.get_rate("BTC", "USD")
            self.assertEqual(path.read_text(), invalid)

    def test_provider_errors_and_invalid_responses_preserve_cache(self):
        self.storage.save("rates.json", {"BTC_USD": self.entry(70000, age=301)})
        path = Path(self.directory.name) / "rates.json"
        before = path.read_bytes()
        responses = (
            None,
            {},
            {"source": "Test", "rates": {"USD": 1, "BTC": -1}},
            {"source": "Test", "rates": {"USD": 1, "BTC": float("inf")}},
        )
        for response in responses:
            with self.subTest(response=response):
                with patch.object(self.service, "_provider", return_value=response):
                    with self.assertRaises(ApiRequestError):
                        self.service.get_rate("BTC", "USD")
                self.assertEqual(path.read_bytes(), before)
        with patch.object(
            self.service, "_provider", side_effect=RuntimeError("offline")
        ):
            with self.assertRaisesRegex(ApiRequestError, "offline"):
                self.service.get_rate("BTC", "USD")
        self.assertEqual(path.read_bytes(), before)
