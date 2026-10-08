"""API-контракты, частичные отказы, история и интеграция с CLI"""

import io
import json
import os
import tempfile
import unittest
from concurrent.futures import ProcessPoolExecutor
from contextlib import redirect_stdout
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from unittest.mock import Mock, patch

import requests

from tests.support import silent_logger
from valutatrade_hub.cli.interface import WalletCLI
from valutatrade_hub.core.exceptions import ApiRequestError
from valutatrade_hub.core.usecases import WalletService
from valutatrade_hub.core.utils import JsonStorage
from valutatrade_hub.infra.database import DatabaseManager
from valutatrade_hub.infra.settings import SettingsLoader
from valutatrade_hub.logging_config import configure_logging
from valutatrade_hub.parser_service.api_clients import (
    BaseApiClient,
    CoinGeckoClient,
    ExchangeRateApiClient,
)
from valutatrade_hub.parser_service.config import ParserConfig
from valutatrade_hub.parser_service.scheduler import RatesScheduler, main
from valutatrade_hub.parser_service.storage import ParserStorage
from valutatrade_hub.parser_service.updater import RatesUpdater, create_updater

CRYPTO = {
    "bitcoin": {"usd": 60000, "last_updated_at": 1791370000},
    "ethereum": {"usd": 3000},
    "solana": {"usd": 150},
}
FIAT = {
    "result": "success",
    "base_code": "USD",
    "conversion_rates": {"USD": 1, "EUR": 0.8, "GBP": 0.5, "RUB": 100},
    "time_last_update_unix": 1791370000,
}


def response(payload, status=200):
    result = requests.Response()
    result.status_code = status
    result._content = json.dumps(payload).encode()
    result._content_consumed = True
    result.headers["ETag"] = 'W/"test"'
    return result


def measurement(code="BTC", rate=60000, timestamp="2026-10-07T12:00:00Z"):
    return {
        "id": f"{code}_USD_{timestamp}",
        "from_currency": code,
        "to_currency": "USD",
        "rate": rate,
        "timestamp": timestamp,
        "source": "Test",
        "meta": {},
    }


def concurrent_write(paths, index):
    config = ParserConfig(RATES_FILE_PATH=paths[0], HISTORY_FILE_PATH=paths[1])
    timestamp = f"2026-10-07T12:00:{index:02d}Z"
    ParserStorage(config).save(
        [measurement(rate=index + 1, timestamp=timestamp)], last_refresh=timestamp
    )


class ParserTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.config = ParserConfig(
            EXCHANGERATE_API_KEY="private-exchange-key",
            COINGECKO_API_KEY="private-demo-key",
            RATES_FILE_PATH=str(self.directory / "rates.json"),
            HISTORY_FILE_PATH=str(self.directory / "exchange_rates.json"),
        )
        self.storage = ParserStorage(self.config)
        self.backend = JsonStorage(self.directory)
        self.logger = silent_logger()

    def updater(self, clients):
        return RatesUpdater(clients, self.storage, logger=self.logger)

    def read(self, filename):
        return json.loads((self.directory / filename).read_text())

    def test_config_reads_environment_on_construction_and_hides_keys(self):
        with patch.dict(
            os.environ, {"EXCHANGERATE_API_KEY": "first", "COINGECKO_API_KEY": "second"}
        ):
            first = ParserConfig()
        self.assertEqual(first.EXCHANGERATE_API_KEY, "first")
        self.assertEqual(first.COINGECKO_API_KEY, "second")
        self.assertNotIn("first", repr(first))
        self.assertNotIn("second", repr(first))
        first.CRYPTO_ID_MAP["BTC"] = "changed"
        self.assertEqual(ParserConfig().CRYPTO_ID_MAP["BTC"], "bitcoin")
        for overrides in (
            {"REQUEST_TIMEOUT": 0},
            {"REQUEST_TIMEOUT": float("nan")},
            {"UPDATE_INTERVAL": True},
            {"CRYPTO_ID_MAP": {}},
            {"FIAT_CURRENCIES": ("BTC",)},
            {"COINGECKO_URL": "http://example.com"},
            {"HISTORY_FILE_PATH": self.config.RATES_FILE_PATH},
        ):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                replace(self.config, **overrides)
        with self.assertRaises(TypeError):
            BaseApiClient(self.config)

    def test_local_env_file_is_loaded_without_mutating_process_environment(self):
        (self.directory / ".env").write_text(
            'EXCHANGERATE_API_KEY="local-fixture-key"\n'
            'COINGECKO_API_KEY="local-demo-key"\n'
            "UNRELATED_SETTING=ignored\n"
        )
        with (
            patch.dict(os.environ, {}, clear=True),
            patch(
                "valutatrade_hub.parser_service.config.Path.cwd",
                return_value=self.directory,
            ),
        ):
            config = ParserConfig()
            self.assertEqual(config.EXCHANGERATE_API_KEY, "local-fixture-key")
            self.assertEqual(config.COINGECKO_API_KEY, "local-demo-key")
            self.assertNotIn("EXCHANGERATE_API_KEY", os.environ)
            self.assertNotIn("UNRELATED_SETTING", os.environ)
            self.assertNotIn("local-fixture-key", repr(config))
            with patch.dict(os.environ, {"EXCHANGERATE_API_KEY": "environment-key"}):
                self.assertEqual(ParserConfig().EXCHANGERATE_API_KEY, "environment-key")
            with patch.dict(os.environ, {"EXCHANGERATE_API_KEY": ""}):
                self.assertEqual(ParserConfig().EXCHANGERATE_API_KEY, "")
            explicit = ParserConfig(EXCHANGERATE_API_KEY="explicit-key")
            self.assertEqual(explicit.EXCHANGERATE_API_KEY, "explicit-key")

    def test_missing_local_env_and_environment_give_empty_keys(self):
        with (
            patch.dict(os.environ, {}, clear=True),
            patch(
                "valutatrade_hub.parser_service.config.Path.cwd",
                return_value=self.directory,
            ),
        ):
            config = ParserConfig()
        self.assertEqual(config.EXCHANGERATE_API_KEY, "")
        self.assertEqual(config.COINGECKO_API_KEY, "")

    def test_crypto_request_ids_timeout_header_and_metadata(self):
        client = CoinGeckoClient(self.config)
        with patch("requests.get", return_value=response(CRYPTO)) as get:
            self.assertEqual(
                client.fetch_rates(),
                {"BTC_USD": 60000, "ETH_USD": 3000, "SOL_USD": 150},
            )
        kwargs = get.call_args.kwargs
        self.assertEqual(kwargs["params"]["ids"], "bitcoin,ethereum,solana")
        self.assertEqual(kwargs["params"]["vs_currencies"], "usd")
        self.assertEqual(kwargs["timeout"], 10)
        self.assertFalse(kwargs["allow_redirects"])
        self.assertEqual(kwargs["headers"], {"x-cg-demo-api-key": "private-demo-key"})
        self.assertNotIn("private-demo-key", get.call_args.args[0])
        meta = client.last_metadata["BTC_USD"]
        self.assertEqual(meta["raw_id"], "bitcoin")
        self.assertEqual(meta["status_code"], 200)
        self.assertEqual(meta["etag"], 'W/"test"')
        self.assertIn("provider_updated_at", meta)
        self.assertGreaterEqual(meta["request_ms"], 0)
        self.assertEqual(datetime.fromisoformat(client.fetched_at).tzinfo, UTC)

    def test_coin_public_request_omits_key_header(self):
        client = CoinGeckoClient(replace(self.config, COINGECKO_API_KEY=""))
        with patch("requests.get", return_value=response(CRYPTO)) as get:
            client.fetch_rates()
        self.assertEqual(get.call_args.kwargs["headers"], {})

    def test_fiat_inverts_base_quotes_and_accepts_teaching_field(self):
        client = ExchangeRateApiClient(self.config)
        for field in ("conversion_rates", "rates"):
            payload = {**FIAT, field: FIAT["conversion_rates"]}
            if field == "rates":
                del payload["conversion_rates"]
            with patch("requests.get", return_value=response(payload)) as get:
                rates = client.fetch_rates()
            self.assertEqual(rates, {"EUR_USD": 1.25, "GBP_USD": 2, "RUB_USD": 0.01})
            self.assertTrue(
                get.call_args.args[0].endswith("/private-exchange-key/latest/USD")
            )

    def test_no_fiat_key_fails_before_request(self):
        client = ExchangeRateApiClient(replace(self.config, EXCHANGERATE_API_KEY=""))
        with (
            patch("requests.get") as get,
            self.assertRaisesRegex(ApiRequestError, "EXCHANGERATE_API_KEY"),
        ):
            client.fetch_rates()
        get.assert_not_called()

    def test_http_and_transport_errors_do_not_expose_secrets(self):
        for client in (
            CoinGeckoClient(self.config),
            ExchangeRateApiClient(self.config),
        ):
            for status in (301, 401, 403, 429, 500):
                with self.subTest(client=client.source, status=status):
                    with patch(
                        "requests.get",
                        return_value=response(
                            {"error": "private-exchange-key"}, status
                        ),
                    ):
                        with self.assertRaises(ApiRequestError) as caught:
                            client.fetch_rates()
                    self.assertIn(str(status), str(caught.exception))
                    self.assertNotIn("private-exchange-key", str(caught.exception))
            for error in (
                requests.Timeout("private-demo-key"),
                requests.ConnectionError("https://example.com/private-exchange-key"),
            ):
                with patch("requests.get", side_effect=error):
                    with self.assertRaises(ApiRequestError) as caught:
                        client.fetch_rates()
                self.assertNotIn("private-", str(caught.exception))

    def test_invalid_json_and_crypto_values_are_rejected(self):
        client = CoinGeckoClient(self.config)
        invalid = response({})
        invalid._content = b"not json"
        with (
            patch("requests.get", return_value=invalid),
            self.assertRaises(ApiRequestError),
        ):
            client.fetch_rates()
        for payload in (
            [],
            {},
            {"bitcoin": {"usd": 5}},
            *(
                {**CRYPTO, "bitcoin": {"usd": bad}}
                for bad in (None, True, 0, -1, "60000", float("nan"), float("inf"))
            ),
        ):
            with self.subTest(payload=payload):
                with (
                    patch("requests.get", return_value=response(payload)),
                    self.assertRaises(ApiRequestError),
                ):
                    client.fetch_rates()
                self.assertEqual(client.last_metadata, {})

    def test_fiat_rejects_api_errors_wrong_base_and_invalid_rates(self):
        client = ExchangeRateApiClient(self.config)
        for payload in (
            {"result": "error", "error-type": "invalid-key"},
            {"result": "error", "error-type": "quota-reached"},
            {**FIAT, "base_code": "EUR"},
            {**FIAT, "conversion_rates": {"EUR": 0}},
            {**FIAT, "conversion_rates": []},
        ):
            with self.subTest(payload=payload):
                with (
                    patch("requests.get", return_value=response(payload)),
                    self.assertRaises(ApiRequestError),
                ):
                    client.fetch_rates()

    def test_update_persists_six_rates_history_and_provider_metadata(self):
        updater = self.updater(
            [CoinGeckoClient(self.config), ExchangeRateApiClient(self.config)]
        )
        with patch("requests.get", side_effect=[response(CRYPTO), response(FIAT)]):
            report = updater.run_update()
        self.assertEqual(report["updated"], 6)
        self.assertEqual(report["errors"], {})
        cache = self.read("rates.json")
        self.assertEqual(set(cache), {"pairs", "last_refresh"})
        self.assertEqual(cache["pairs"]["EUR_USD"]["rate"], 1.25)
        history = self.read("exchange_rates.json")
        self.assertEqual(len(history), 6)
        self.assertEqual(history[0]["id"], f"BTC_USD_{history[0]['timestamp']}")
        self.assertEqual(history[0]["meta"]["raw_id"], "bitcoin")
        for filename in ("rates.json", "exchange_rates.json"):
            self.assertNotIn("private-", (self.directory / filename).read_text())

    def test_partial_failure_preserves_other_provider_quotes(self):
        record = measurement("EUR", 1.1)
        self.storage.save([record], last_refresh=record["timestamp"])
        updater = self.updater(
            [ExchangeRateApiClient(self.config), CoinGeckoClient(self.config)]
        )
        with self.assertLogs(self.logger, level="INFO") as logs:
            with patch(
                "requests.get",
                side_effect=[
                    requests.Timeout("private-exchange-key"),
                    response(CRYPTO),
                ],
            ):
                report = updater.run_update()
        self.assertEqual(report["updated"], 3)
        self.assertIn("ExchangeRate-API", report["errors"])
        self.assertEqual(self.read("rates.json")["pairs"]["EUR_USD"]["rate"], 1.1)
        self.assertEqual(len(self.read("exchange_rates.json")), 4)
        self.assertIn("fetch_failed", " ".join(logs.output))
        self.assertIn("update_completed", " ".join(logs.output))
        self.assertNotIn("private-exchange-key", " ".join(logs.output))

    def test_total_failure_preserves_both_files(self):
        record = measurement()
        self.storage.save([record], last_refresh=record["timestamp"])
        before = {p.name: p.read_bytes() for p in self.directory.glob("*.json")}
        updater = self.updater(
            [CoinGeckoClient(self.config), ExchangeRateApiClient(self.config)]
        )
        with patch("requests.get", side_effect=requests.ConnectionError("offline")):
            with self.assertRaises(ApiRequestError):
                updater.run_update()
        self.assertEqual(
            {p.name: p.read_bytes() for p in self.directory.glob("*.json")}, before
        )

    def test_history_deduplicates_and_older_measurement_cannot_replace_snapshot(self):
        new = measurement(rate=65000, timestamp="2026-10-07T12:01:00Z")
        old = measurement()
        self.storage.save([new], last_refresh=new["timestamp"])
        self.storage.save([new, old], last_refresh=old["timestamp"])
        cache = self.read("rates.json")
        self.assertEqual(len(self.read("exchange_rates.json")), 2)
        self.assertEqual(cache["pairs"]["BTC_USD"]["rate"], 65000)
        self.assertEqual(cache["last_refresh"], new["timestamp"])
        with self.assertRaisesRegex(ValueError, "Конфликт"):
            self.storage.save([{**new, "rate": 1}], last_refresh=new["timestamp"])
        self.assertEqual(self.read("rates.json"), cache)

    def test_malformed_records_and_existing_files_are_preserved(self):
        record = measurement()
        self.storage.save([record], last_refresh=record["timestamp"])
        before = {p.name: p.read_bytes() for p in self.directory.glob("*.json")}
        for invalid in (
            {**record, "rate": float("nan")},
            {**record, "id": "wrong"},
            {**record, "timestamp": "broken"},
            {**record, "from_currency": "btc"},
        ):
            with self.assertRaises(ValueError):
                self.storage.save([invalid], last_refresh=record["timestamp"])
            self.assertEqual(
                {p.name: p.read_bytes() for p in self.directory.glob("*.json")}, before
            )
        for filename in ("rates.json", "exchange_rates.json"):
            path = self.directory / filename
            path.write_text("broken")
            with self.assertRaises(ValueError):
                self.storage.save([record], last_refresh=record["timestamp"])
            self.assertEqual(path.read_text(), "broken")
            path.write_bytes(before[filename])

    def test_failed_atomic_replace_leaves_existing_file_and_cleans_temporary(self):
        record = measurement()
        self.storage.save([record], last_refresh=record["timestamp"])
        before = {p.name: p.read_bytes() for p in self.directory.glob("*.json")}
        with patch(
            "valutatrade_hub.core.utils.os.replace", side_effect=OSError("disk full")
        ):
            with self.assertRaises(OSError):
                self.storage.save(
                    [measurement(rate=5, timestamp="2026-10-07T12:01:00Z")],
                    last_refresh=record["timestamp"],
                )
        self.assertEqual(
            {p.name: p.read_bytes() for p in self.directory.glob("*.json")}, before
        )
        self.assertFalse(list(self.directory.glob("*.tmp")))

    def test_processes_do_not_lose_history_or_regress_snapshot(self):
        paths = (self.config.RATES_FILE_PATH, self.config.HISTORY_FILE_PATH)
        with ProcessPoolExecutor(max_workers=3) as pool:
            list(pool.map(concurrent_write, [paths] * 9, reversed(range(9))))
        self.assertEqual(len(self.read("exchange_rates.json")), 9)
        self.assertEqual(self.read("rates.json")["pairs"]["BTC_USD"]["rate"], 9)

    def test_source_selection_and_custom_database_paths(self):
        path = self.directory / "config.json"
        path.write_text(
            json.dumps({"rates_file": "quotes.json", "history_file": "history.json"})
        )
        with (
            patch.object(SettingsLoader, "_instance", None),
            patch.object(DatabaseManager, "_instance", None),
        ):
            settings = SettingsLoader(path)
            backend = DatabaseManager(settings)
            updater = create_updater(
                source="coingecko",
                settings=settings,
                storage=backend,
                logger=self.logger,
            )
            with patch("requests.get", return_value=response(CRYPTO)) as get:
                updater.run_update()
            self.assertEqual(get.call_count, 1)
            self.assertEqual(
                backend.load("rates.json", dict)["pairs"]["BTC_USD"]["rate"], 60000
            )
            self.assertTrue((self.directory / "data/history.json").is_file())
        with self.assertRaisesRegex(ValueError, "--source"):
            create_updater(source="unknown", config=self.config, logger=self.logger)

    def test_cli_update_show_trade_and_reload_without_network(self):
        service = WalletService(self.backend, logger=self.logger)
        cli = WalletCLI(service)
        updater = self.updater(
            [CoinGeckoClient(self.config), ExchangeRateApiClient(self.config)]
        )
        with patch(
            "valutatrade_hub.core.usecases.create_updater", return_value=updater
        ):
            with patch("requests.get", side_effect=[response(CRYPTO), response(FIAT)]):
                with redirect_stdout(io.StringIO()) as output:
                    self.assertTrue(cli.execute("update-rates"))
        self.assertIn("Обновлено курсов: 6", output.getvalue())
        with patch("requests.get") as request, redirect_stdout(io.StringIO()) as output:
            for command in (
                "register --username alice --password 1234",
                "login --username alice --password 1234",
                "deposit --amount 1000",
                "buy --currency SOL --amount 2",
                "sell --currency SOL --amount 1",
                "show-portfolio",
                "show-rates --top 2 --base EUR",
                "show-rates --currency RUB",
            ):
                self.assertTrue(cli.execute(command))
        request.assert_not_called()
        self.assertIn("ИТОГО: 1,000.00 USD", output.getvalue())
        self.assertIn("BTC_EUR", output.getvalue())
        self.assertIn("RUB_USD", output.getvalue())
        restarted = WalletService(self.backend, logger=self.logger)
        restarted.login("alice", "1234")
        self.assertEqual(restarted.show_portfolio()["total"], 1000)

    def test_scheduler_waits_recovers_from_error_and_stops(self):
        stop = Event()
        updater = Mock()
        updater.run_update.side_effect = [
            ApiRequestError("offline"),
            {"sources": {}, "errors": {}, "updated": 1, "last_refresh": "now"},
        ]
        scheduler = RatesScheduler(updater, 15, stop_event=stop)
        with (
            patch.object(stop, "wait", side_effect=[False, True]) as wait,
            redirect_stdout(io.StringIO()),
        ):
            scheduler.run()
        self.assertEqual(updater.run_update.call_count, 2)
        self.assertEqual(wait.call_args.args, (15,))
        updater.run_update.side_effect = KeyboardInterrupt
        with redirect_stdout(io.StringIO()):
            scheduler.run()
        self.assertTrue(stop.is_set())

    def test_standalone_partial_success_returns_nonzero(self):
        updater = Mock()
        updater.run_update.return_value = {
            "sources": {"CoinGecko": 3},
            "errors": {"ExchangeRate-API": "missing key"},
            "updated": 3,
            "last_refresh": "now",
        }
        with (
            patch("sys.argv", ["rates-parser"]),
            patch(
                "valutatrade_hub.parser_service.scheduler.create_updater",
                return_value=updater,
            ),
            redirect_stdout(io.StringIO()),
        ):
            with self.assertRaises(SystemExit) as caught:
                main()
        self.assertEqual(caught.exception.code, 1)

    def test_parser_logger_has_separate_file_and_rotation(self):
        path = self.directory / "config.json"
        path.write_text('{"log_max_bytes": 350, "log_backup_count": 2}')
        with patch.object(SettingsLoader, "_instance", None):
            settings = SettingsLoader(path)
            logger = configure_logging(settings, parser=True)
            try:
                for i in range(15):
                    logger.info(
                        {"event": "fetch_started", "index": i, "source": "CoinGecko"}
                    )
            finally:
                for handler in logger.handlers[:]:
                    logger.removeHandler(handler)
                    handler.close()
        self.assertEqual(
            {p.name for p in (self.directory / "logs").iterdir()},
            {"parser.log", "parser.log.1", "parser.log.2"},
        )
