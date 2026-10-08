"""Срок кэша, конвертация, фильтры и отказ от учебных котировок"""

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from tests.support import seed_rates
from valutatrade_hub.core.exceptions import CurrencyNotFoundError
from valutatrade_hub.core.rates import RateService
from valutatrade_hub.core.utils import JsonStorage


class RateTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.storage = JsonStorage(directory.name)
        self.service = RateService(self.storage)

    def entry(self, rate, *, age=0, source="Test"):
        return {
            "rate": rate,
            "updated_at": (datetime.now(UTC) - timedelta(seconds=age)).isoformat(),
            "source": source,
        }

    def test_missing_cache_does_not_invent_rates_or_request_network(self):
        with (
            patch("requests.get") as request,
            patch.object(self.storage, "save") as save,
        ):
            with self.assertRaisesRegex(ValueError, "update-rates"):
                self.service.get_rate("BTC", "USD")
        request.assert_not_called()
        save.assert_not_called()
        self.assertEqual(self.service.get_rate("USD", "USD")["rate"], 1)

    def test_new_and_legacy_formats_support_inverse_and_cross_rates(self):
        pairs = {"BTC_USD": self.entry(60000), "EUR_USD": self.entry(1.2)}
        for cache in (pairs, {"pairs": pairs}):
            with self.subTest(cache=cache):
                self.storage.save("rates.json", cache)
                before = self.storage.path_for("rates.json").read_bytes()
                self.assertAlmostEqual(
                    self.service.get_rate("USD", "BTC")["rate"], 1 / 60000
                )
                self.assertEqual(
                    self.service.get_rate("USD", "BTC")["inverse_rate"], 60000
                )
                self.assertAlmostEqual(
                    self.service.get_rate("btc", "eur")["rate"], 50000
                )
                self.assertEqual(
                    self.storage.path_for("rates.json").read_bytes(), before
                )

    def test_expired_future_and_stub_cache_cannot_be_used_for_trades(self):
        for entry in (
            self.entry(90000, age=301),
            self.entry(90000, age=-100),
            self.entry(90000, source="Stub"),
        ):
            with self.subTest(entry=entry):
                self.storage.save("rates.json", {"BTC_USD": entry})
                before = self.storage.path_for("rates.json").read_bytes()
                with self.assertRaisesRegex(ValueError, "update-rates"):
                    self.service.get_rate("BTC", "USD")
                self.assertEqual(
                    self.storage.path_for("rates.json").read_bytes(), before
                )

    def test_ttl_boundary_is_expired(self):
        now = datetime(2026, 10, 7, 12, tzinfo=UTC)
        for age in (299, 300):
            self.storage.save(
                "rates.json",
                {
                    "pairs": {
                        "BTC_USD": {
                            "rate": 70000,
                            "updated_at": (now - timedelta(seconds=age)).isoformat(),
                        }
                    }
                },
            )
            with patch("valutatrade_hub.core.rates.datetime", wraps=datetime) as clock:
                clock.now.return_value = now
                if age == 299:
                    self.assertEqual(self.service.get_rate("BTC", "USD")["rate"], 70000)
                else:
                    with self.assertRaisesRegex(ValueError, "устарел"):
                        self.service.get_rate("BTC", "USD")

    def test_unknown_codes_and_invalid_syntax(self):
        for code in ("ABC", "", "../USD", None):
            with self.subTest(code=code), self.assertRaises(CurrencyNotFoundError):
                self.service.get_rate(code, "USD")

    def test_malformed_cache_is_preserved(self):
        path = self.storage.path_for("rates.json")
        for invalid in (
            "broken",
            "[]",
            '{"pairs": []}',
            '{"BTC_USD": {"rate": -1, "updated_at": "2025-01-01"}}',
            '{"BTC_USD": {"rate": 1, "updated_at": "invalid"}}',
            '{"BTC_USD": {"rate": NaN, "updated_at": "2025-01-01"}}',
        ):
            path.write_text(invalid)
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.service.get_rate("BTC", "USD")
            self.assertEqual(path.read_text(), invalid)

    def test_show_rates_filters_top_crypto_and_converts_base(self):
        seed_rates(self.storage)
        with patch("requests.get") as request:
            report = self.service.show_rates(top=2, base="EUR")
            self.assertEqual(
                [row["currency"] for row in report["rates"]], ["BTC", "ETH"]
            )
            self.assertAlmostEqual(report["rates"][0]["rate"], 59337.21 / 1.0786)
            report = self.service.show_rates(currency="rub")
            self.assertEqual([row["pair"] for row in report["rates"]], ["RUB_USD"])
        request.assert_not_called()
        for top in (0, -1, True, 2.5):
            with self.assertRaises(ValueError):
                self.service.show_rates(top=top)
        with self.assertRaisesRegex(ValueError, "не найден"):
            self.service.show_rates(currency="ABC")

    def test_show_rates_marks_stale_and_uses_oldest_cross_timestamp(self):
        old = self.entry(2, age=301)
        self.storage.save(
            "rates.json", {"pairs": {"EUR_USD": old, "BTC_USD": self.entry(60000)}}
        )
        row = self.service.show_rates(currency="BTC", base="EUR")["rates"][0]
        self.assertTrue(row["stale"])
        self.assertEqual(row["rate"], 30000)
        self.assertEqual(row["updated_at"], old["updated_at"])
        with self.assertRaisesRegex(ValueError, "устарел"):
            self.service.get_rate("BTC", "EUR")

    def test_show_rates_empty_and_missing_base_conversion(self):
        with self.assertRaisesRegex(ValueError, "кэш курсов пуст"):
            self.service.show_rates()
        self.storage.save("rates.json", {"pairs": {"BTC_USD": self.entry(60000)}})
        with self.assertRaisesRegex(ValueError, "не найден"):
            self.service.show_rates(base="EUR")
