"""Сценарии торговли и проверки неизменности файлов при ошибках"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.support import seed_rates, silent_logger
from valutatrade_hub.core.usecases import WalletService
from valutatrade_hub.core.utils import JsonStorage


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.storage = JsonStorage(self.directory.name)
        self.storage.initialize()
        seed_rates(self.storage)
        self.service = WalletService(self.storage, logger=silent_logger())

    def register_and_login(self, username="alice"):
        self.service.register(username, "1234")
        self.service.login(username, "1234")

    def portfolios_bytes(self):
        return (Path(self.directory.name) / "portfolios.json").read_bytes()

    def test_registration_creates_empty_portfolio_and_distinct_salted_hashes(self):
        first = self.service.register("alice", "1234")
        second = self.service.register("bob", "1234")
        self.assertEqual((first.user_id, second.user_id), (1, 2))
        self.assertNotEqual(first.salt, second.salt)
        self.assertNotEqual(first.hashed_password, second.hashed_password)
        self.assertIsNone(self.service.current_user)
        self.assertEqual(
            self.storage.load("portfolios.json", list),
            [
                {"user_id": 1, "wallets": {}},
                {"user_id": 2, "wallets": {}},
            ],
        )
        users = self.storage.load("users.json", list)
        self.assertNotIn("password", users[0])

    def test_invalid_registration_keeps_files_unchanged(self):
        self.service.register("alice", "1234")
        users_before = (Path(self.directory.name) / "users.json").read_bytes()
        portfolios_before = self.portfolios_bytes()
        for name, password, error in (
            ("alice", "1234", "уже занято"),
            (" ", "1234", "пустым"),
            ("bob", "123", "не короче 4"),
        ):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, error):
                self.service.register(name, password)
        self.assertEqual(
            (Path(self.directory.name) / "users.json").read_bytes(), users_before
        )
        self.assertEqual(self.portfolios_bytes(), portfolios_before)

    def test_failed_login_clears_previous_session(self):
        self.register_and_login()
        with self.assertRaisesRegex(ValueError, "Неверный пароль"):
            self.service.login("alice", "wrong")
        self.assertIsNone(self.service.current_user)
        with self.assertRaisesRegex(ValueError, "не найден"):
            self.service.login("nobody", "1234")

    def test_operations_require_login_but_rates_do_not(self):
        for operation in (
            lambda: self.service.deposit(100),
            lambda: self.service.buy("BTC", 1),
            lambda: self.service.sell("BTC", 1),
            self.service.show_portfolio,
        ):
            with self.assertRaisesRegex(ValueError, "Сначала выполните login"):
                operation()
        self.assertGreater(self.service.get_rate("USD", "BTC")["rate"], 0)

    def test_buy_sell_conserve_value_and_survive_restart(self):
        self.register_and_login()
        self.assertEqual(self.service.show_portfolio()["wallets"], [])
        self.service.deposit(10000)
        bought = self.service.buy("btc", 0.05)
        self.assertAlmostEqual(bought["usd_after"], 10000 - 0.05 * bought["rate"])
        sold = self.service.sell("BTC", 0.01)
        self.assertAlmostEqual(sold["after"], 0.04)
        self.assertAlmostEqual(sold["usd_after"], bought["usd_after"] + sold["cost"])
        self.assertAlmostEqual(self.service.show_portfolio()["total"], 10000)
        self.assertAlmostEqual(
            self.service.show_portfolio("EUR")["total"], 10000 / 1.0786
        )
        restarted = WalletService(self.storage, logger=silent_logger())
        self.assertIsNone(restarted.current_user)
        restarted.login("alice", "1234")
        self.assertEqual(restarted.show_portfolio(), self.service.show_portfolio())

    def test_accounts_are_isolated(self):
        self.register_and_login("alice")
        self.service.deposit(500)
        self.register_and_login("bob")
        self.assertEqual(self.service.show_portfolio()["total"], 0)
        self.service.deposit(200)
        self.service.login("alice", "1234")
        self.assertEqual(self.service.show_portfolio()["total"], 500)

    def test_failed_trades_leave_no_wallets_or_partial_debits(self):
        self.register_and_login()
        before = self.portfolios_bytes()
        for operation, error in (
            (lambda: self.service.buy("BTC", 1), "Недостаточно средств"),
            (lambda: self.service.sell("BTC", 1), "Недостаточно средств"),
            (lambda: self.service.buy("ABC", 1), "Неизвестная валюта"),
            (lambda: self.service.buy("USD", 1), "расчетная валюта"),
            (lambda: self.service.show_portfolio("ABC"), "Неизвестная валюта"),
        ):
            with self.subTest(error=error), self.assertRaisesRegex(ValueError, error):
                operation()
            self.assertEqual(self.portfolios_bytes(), before)
        self.service.deposit(500)
        self.service.buy("EUR", 100)
        before = self.portfolios_bytes()
        with self.assertRaisesRegex(ValueError, "Недостаточно средств"):
            self.service.sell("EUR", 101)
        self.assertEqual(self.portfolios_bytes(), before)

    def test_bad_amounts_do_not_change_portfolios(self):
        self.register_and_login()
        before = self.portfolios_bytes()
        for amount in (0, -1, "3", True, float("nan"), float("inf")):
            for operation in (
                lambda: self.service.deposit(amount),
                lambda: self.service.buy("BTC", amount),
                lambda: self.service.sell("BTC", amount),
            ):
                with self.subTest(amount=amount), self.assertRaises(ValueError):
                    operation()
                self.assertEqual(self.portfolios_bytes(), before)

    def test_corrupted_json_and_schema_are_not_overwritten(self):
        self.register_and_login()
        path = Path(self.directory.name) / "portfolios.json"
        for invalid in (
            "{broken",
            "{}",
            '[{"user_id": 1, "wallets": {"USD": {"balance": -1}}}]',
        ):
            path.write_text(invalid)
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.service.deposit(100)
            self.assertEqual(path.read_text(), invalid)

    def test_user_validation_detects_duplicate_ids(self):
        self.register_and_login()
        users = self.storage.load("users.json", list)
        self.storage.save("users.json", users + users)
        with self.assertRaisesRegex(ValueError, "users.json"):
            self.service.login("alice", "1234")

    def test_portfolio_write_failure_keeps_old_balance_and_cleans_temp_files(self):
        self.register_and_login()
        self.service.deposit(500)
        before = self.portfolios_bytes()
        with patch(
            "valutatrade_hub.core.utils.os.replace", side_effect=OSError("disk error")
        ):
            with self.assertRaises(OSError):
                self.service.deposit(100)
        self.assertEqual(self.portfolios_bytes(), before)
        self.assertEqual(list(Path(self.directory.name).glob("*.tmp")), [])
        self.assertEqual(self.service.show_portfolio()["total"], 500)

    def test_registration_rolls_back_portfolio_if_users_save_fails(self):
        original_save = self.storage.save

        def save(filename, data):
            if filename == "users.json":
                raise OSError("disk error")
            original_save(filename, data)

        with patch.object(self.storage, "save", side_effect=save):
            with self.assertRaises(OSError):
                self.service.register("alice", "1234")
        self.assertEqual(self.storage.load("users.json", list), [])
        self.assertEqual(self.storage.load("portfolios.json", list), [])

    def test_no_extra_json_files_or_plaintext_passwords(self):
        self.register_and_login()
        self.service.deposit(100)
        self.service.buy("EUR", 10)
        for path in Path(self.directory.name).glob("*.json"):
            json.loads(path.read_text())
        self.assertEqual(
            {p.name for p in Path(self.directory.name).iterdir()},
            {"users.json", "portfolios.json", "rates.json"},
        )
