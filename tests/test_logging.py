"""Журнал успешных и неудачных операций, ротация и сохранение исключений"""

import inspect
import io
import json
import logging
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from valutatrade_hub.cli.interface import WalletCLI
from valutatrade_hub.core.exceptions import (
    ApiRequestError,
    CurrencyNotFoundError,
    InsufficientFundsError,
)
from valutatrade_hub.core.usecases import WalletService
from valutatrade_hub.core.utils import JsonStorage
from valutatrade_hub.decorators import log_action
from valutatrade_hub.infra.settings import SettingsLoader
from valutatrade_hub.logging_config import configure_logging


class LoggingTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        reset = patch.object(SettingsLoader, "_instance", None)
        reset.start()
        self.addCleanup(reset.stop)
        self.config = Path(self.directory.name) / "config.json"
        self.config.write_text("{}")
        self.settings = SettingsLoader(self.config)
        self.logger = configure_logging(self.settings)
        self.addCleanup(self.close_handlers)
        self.path = Path(self.directory.name) / "logs/actions.log"
        self.storage = JsonStorage(Path(self.directory.name) / "data")
        self.service = WalletService(
            self.storage, settings=self.settings, logger=self.logger
        )

    def close_handlers(self):
        for handler in self.logger.handlers[:]:
            self.logger.removeHandler(handler)
            handler.close()

    def records(self):
        return [json.loads(line) for line in self.path.read_text().splitlines()]

    def login_and_deposit(self):
        self.service.register("alice", "private-password")
        self.service.login("alice", "private-password")
        self.service.deposit(5000)

    def test_success_and_failure_have_required_fields_without_passwords(self):
        self.login_and_deposit()
        self.service.buy("btc", 0.05)
        with self.assertRaises(InsufficientFundsError):
            self.service.sell("BTC", 1)
        success, failure = self.records()
        for event in (success, failure):
            self.assertEqual(event["level"], "INFO")
            self.assertIn("T", event["timestamp"])
            self.assertEqual(event["username"], "alice")
            self.assertEqual(event["user_id"], 1)
            self.assertEqual(event["currency_code"], "BTC")
            self.assertEqual(event["base"], "USD")
            self.assertNotIn("context", event)
        self.assertEqual(success["action"], "BUY")
        self.assertEqual(success["result"], "OK")
        self.assertEqual(success["amount"], 0.05)
        self.assertEqual(success["rate"], 59337.21)
        self.assertEqual(failure["action"], "SELL")
        self.assertEqual(failure["result"], "ERROR")
        self.assertEqual(failure["error_type"], "InsufficientFundsError")
        self.assertIn("Недостаточно средств", failure["error_message"])
        for secret in ("private-password", "hashed_password", "salt"):
            self.assertNotIn(secret, self.path.read_text())

    def test_verbose_uses_settings_and_adds_balance_context(self):
        self.config.write_text('{"log_verbose": true}')
        self.settings.reload()
        self.login_and_deposit()
        self.service.buy("EUR", 100)
        with self.assertRaises(InsufficientFundsError):
            self.service.sell("EUR", 101)
        success, failure = self.records()
        self.assertEqual(success["context"]["before"], 0)
        self.assertEqual(success["context"]["after"], 100)
        self.assertEqual(success["context"]["usd_before"], 5000)
        self.assertAlmostEqual(success["context"]["usd_after"], 5000 - 107.86)
        self.assertEqual(failure["context"], {"available": 100, "required": 101})

    def test_decorator_preserves_metadata_result_and_exception_identity(self):
        def buy(user_id, currency_code, amount):
            """Описание тестовой операции"""
            return {
                "amount": amount,
                "currency": currency_code,
                "before": 0,
                "after": amount,
            }

        wrapped = log_action("BUY", verbose=True)(buy)
        self.assertEqual(wrapped.__name__, buy.__name__)
        self.assertEqual(wrapped.__doc__, buy.__doc__)
        self.assertIs(wrapped.__wrapped__, buy)
        self.assertEqual(inspect.signature(wrapped), inspect.signature(buy))
        self.assertEqual(wrapped(1, currency_code="BTC", amount=2), buy(1, "BTC", 2))
        original = CurrencyNotFoundError("ABC")

        @log_action("SELL")
        def fail(user_id, currency_code, amount):
            raise original

        with self.assertRaises(CurrencyNotFoundError) as caught:
            fail(1, "ABC", 2)
        self.assertIs(caught.exception, original)
        self.assertEqual(self.records()[0]["context"], {"before": 0, "after": 2})
        self.assertEqual(self.records()[1]["error_type"], "CurrencyNotFoundError")

    def test_invalid_numbers_are_logged_as_valid_json(self):
        self.login_and_deposit()
        for number in (float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                self.service.buy("BTC", number)
        self.assertEqual([event["amount"] for event in self.records()], ["nan", "inf"])

    def test_rotation_keeps_only_configured_backups(self):
        self.config.write_text('{"log_max_bytes": 400, "log_backup_count": 2}')
        self.settings.reload()
        logger = configure_logging(self.settings)
        for index in range(20):
            logger.info(
                {"action": "TEST", "result": "OK", "index": index, "message": "x" * 200}
            )
        files = {path.name for path in self.path.parent.iterdir()}
        self.assertEqual(files, {"actions.log", "actions.log.1", "actions.log.2"})
        self.assertEqual(self.records()[-1]["index"], 19)
        for path in self.path.parent.iterdir():
            for line in path.read_text().splitlines():
                json.loads(line)

    def test_repeated_configuration_does_not_duplicate_records(self):
        for _ in range(5):
            self.assertIs(configure_logging(self.settings), self.logger)
        self.logger.info({"action": "TEST", "result": "OK"})
        self.assertEqual(len(self.records()), 1)
        self.assertEqual(len(self.logger.handlers), 1)
        self.config.write_text('{"log_level": "DEBUG"}')
        self.settings.reload()
        configure_logging(self.settings)
        self.assertTrue(self.logger.isEnabledFor(logging.DEBUG))

    def test_provider_failure_is_logged_without_changing_cache_or_balances(self):
        self.login_and_deposit()
        self.storage.save("rates.json", {})
        rates_before = (self.storage.data_dir / "rates.json").read_bytes()
        portfolios_before = (self.storage.data_dir / "portfolios.json").read_bytes()
        with patch.object(
            self.service.rates, "_provider", side_effect=TimeoutError("timeout")
        ):
            with self.assertRaises(ApiRequestError):
                self.service.buy("BTC", 0.01)
        self.assertEqual(
            (self.storage.data_dir / "rates.json").read_bytes(), rates_before
        )
        self.assertEqual(
            (self.storage.data_dir / "portfolios.json").read_bytes(), portfolios_before
        )
        self.assertEqual(self.records()[0]["error_type"], "ApiRequestError")

    def test_cli_explains_domain_errors_and_continues(self):
        self.login_and_deposit()
        cli = WalletCLI(self.service)
        for command, fragments in (
            (
                "get-rate --from ABC --to USD",
                ["Неизвестная валюта 'ABC'", "Поддерживаемые коды:"],
            ),
            (
                "sell --currency BTC --amount 1",
                ["Недостаточно средств: доступно 0.0000 BTC"],
            ),
            ("currencies", ["[FIAT] USD", "[CRYPTO] BTC"]),
        ):
            with (
                self.subTest(command=command),
                redirect_stdout(io.StringIO()) as output,
            ):
                self.assertTrue(cli.execute(command))
            for fragment in fragments:
                self.assertIn(fragment, output.getvalue())
        with patch.object(
            self.service.rates, "_provider", side_effect=TimeoutError("timeout")
        ):
            with redirect_stdout(io.StringIO()) as output:
                self.assertTrue(cli.execute("get-rate --from BTC --to USD"))
        self.assertIn("Ошибка при обращении к внешнему API: timeout", output.getvalue())
        self.assertIn("Повторите попытку позже", output.getvalue())
