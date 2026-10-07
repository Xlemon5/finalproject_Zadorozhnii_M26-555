"""Единственность инфраструктуры, конфигурация и безопасные изменения JSON"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from tests.support import seed_rates, silent_logger
from valutatrade_hub.core.rates import RateService
from valutatrade_hub.core.usecases import WalletService
from valutatrade_hub.infra.database import DatabaseManager
from valutatrade_hub.infra.settings import SettingsLoader


class InfraTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "config.json"
        self.path.write_text("{}")
        for cls in (SettingsLoader, DatabaseManager):
            reset = patch.object(cls, "_instance", None)
            reset.start()
            self.addCleanup(reset.stop)

    def configure(self, **values):
        self.path.write_text(json.dumps(values))
        return SettingsLoader(self.path)

    def test_singleton_identity_across_threads_and_repeated_construction(self):
        settings = self.configure(rates_ttl_seconds=42)
        manager = DatabaseManager(settings)
        with ThreadPoolExecutor(max_workers=8) as pool:
            instances = list(
                pool.map(lambda _: (SettingsLoader(), DatabaseManager()), range(24))
            )
        self.assertTrue(all(a is settings and b is manager for a, b in instances))
        self.assertEqual(settings.get("rates_ttl_seconds"), 42)

    def test_configuration_is_cached_until_reload(self):
        settings = self.configure(rates_ttl_seconds=42)
        self.path.write_text('{"rates_ttl_seconds": 90}')
        self.assertEqual(SettingsLoader().get("rates_ttl_seconds"), 42)
        settings.reload()
        self.assertEqual(settings.get("rates_ttl_seconds"), 90)
        fallback = {"values": []}
        settings.get("missing", fallback)["values"].append(1)
        self.assertEqual(fallback, {"values": []})

    def test_invalid_reload_preserves_previous_configuration(self):
        settings = self.configure(rates_ttl_seconds=42)
        for invalid in (
            "broken",
            "[]",
            '{"rates_ttl_seconds": 0}',
            '{"rates_ttl_seconds": true}',
            '{"unknown_key": 1}',
            '{"log_file": "../users.json"}',
            '{"users_file": "rates.json"}',
            '{"log_level": "INVALID"}',
        ):
            self.path.write_text(invalid)
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                settings.reload()
            self.assertEqual(settings.get("rates_ttl_seconds"), 42)

    def test_toml_configuration_is_loaded_from_project_section(self):
        path = self.path.with_suffix(".toml")
        path.write_text(
            "[tool.valutatrade]\nrates_ttl_seconds = 42\n"
            'default_base_currency = "EUR"\n'
        )
        settings = SettingsLoader(path)
        self.assertEqual(settings.get("default_base_currency"), "EUR")
        self.assertEqual(
            settings.get("data_dir"), str((path.parent / "data").resolve())
        )
        with self.assertRaises(ValueError):
            SettingsLoader(self.path)

    def test_database_uses_configured_filenames_and_service_uses_singletons(self):
        settings = self.configure(data_dir="accounts", users_file="accounts.json")
        service = WalletService(logger=silent_logger())
        service.storage.initialize()
        self.assertIs(service.settings, settings)
        self.assertIs(service.storage, DatabaseManager())
        service.register("alice", "1234")
        service.login("alice", "1234")
        service.deposit(100)
        directory = self.path.parent / "accounts"
        self.assertTrue((directory / "accounts.json").is_file())
        self.assertFalse((directory / "users.json").exists())
        self.assertEqual(service.show_portfolio()["total"], 100)

    def test_default_base_currency_and_ttl_follow_settings_reload(self):
        settings = self.configure(rates_ttl_seconds=60, default_base_currency="EUR")
        service = WalletService(logger=silent_logger())
        seed_rates(service.storage)
        service.register("alice", "1234")
        service.login("alice", "1234")
        service.deposit(100)
        self.assertAlmostEqual(service.show_portfolio()["total"], 100 / 1.0786)
        old = {
            "BTC_USD": {
                "rate": 70000,
                "updated_at": (datetime.now(UTC) - timedelta(seconds=70)).isoformat(),
            }
        }
        service.storage.save("rates.json", old)
        rates = RateService(service.storage, settings=settings)
        with self.assertRaisesRegex(ValueError, "устарел"):
            rates.get_rate("BTC", "USD")
        self.path.write_text('{"rates_ttl_seconds": 120}')
        settings.reload()
        service.storage.save("rates.json", old)
        self.assertEqual(rates.get_rate("BTC", "USD")["rate"], 70000)
        self.assertEqual(service.show_portfolio()["base"], "USD")

    def test_shared_manager_prevents_lost_updates_between_services(self):
        self.configure()
        first = WalletService(logger=silent_logger())
        first.register("alice", "1234")
        first.login("alice", "1234")

        def deposit(_):
            service = WalletService(logger=silent_logger())
            service.login("alice", "1234")
            service.deposit(1)

        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(deposit, range(40)))
        self.assertEqual(first.show_portfolio()["total"], 40)

    def test_import_has_no_singleton_instances_or_file_side_effects(self):
        environment = os.environ.copy()
        root = Path(__file__).resolve().parents[1]
        if (root / "valutatrade_hub").is_dir():
            environment["PYTHONPATH"] = str(root)
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import valutatrade_hub.core.usecases; "
                    "from valutatrade_hub.infra.settings import SettingsLoader; "
                    "from valutatrade_hub.infra.database import DatabaseManager; "
                    "assert SettingsLoader._instance is None; "
                    "assert DatabaseManager._instance is None"
                ),
            ],
            cwd=self.directory.name,
            env=environment,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual({p.name for p in self.path.parent.iterdir()}, {"config.json"})
