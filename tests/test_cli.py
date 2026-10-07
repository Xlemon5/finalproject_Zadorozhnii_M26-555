"""Пользовательские сценарии через CLI и отдельные процессы"""

import io
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from tests.support import silent_logger
from valutatrade_hub.cli.interface import WalletCLI, parse_command
from valutatrade_hub.core.usecases import WalletService
from valutatrade_hub.core.utils import JsonStorage


class CLITests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.cli = WalletCLI(
            WalletService(JsonStorage(self.directory.name), logger=silent_logger())
        )

    def execute(self, command):
        output = io.StringIO()
        with redirect_stdout(output):
            keep_running = self.cli.execute(command)
        return keep_running, output.getvalue()

    def test_quoted_arguments_and_equal_sign_syntax(self):
        command, options = parse_command(
            'register --username "Alice Smith" --password="a b c d"'
        )
        self.assertEqual(command, "register")
        self.assertEqual(options, {"username": "Alice Smith", "password": "a b c d"})
        self.execute('register --username "Alice Smith" --password "a b c d"')
        _, output = self.execute('login --username "Alice Smith" --password "a b c d"')
        self.assertIn("Вы вошли как 'Alice Smith'", output)
        self.assertNotIn("a b c d", output)

    def test_unknown_missing_duplicate_arguments_and_bad_quotes(self):
        for line in (
            "unknown",
            "login --username alice",
            "help extra",
            "exit --force",
            "get-rate --from USD --from EUR --to BTC",
            "deposit --amount",
            "deposit --amount nope",
            'login --username "broken',
        ):
            with self.subTest(line=line):
                keep_running, output = self.execute(line)
                self.assertTrue(keep_running)
                self.assertTrue(output.strip())
                self.assertNotIn("Traceback", output)

    def test_errors_do_not_end_session_loop(self):
        for line, message in (
            ("show-portfolio", "Сначала выполните login"),
            ("register --username alice --password 123", "не короче 4"),
            ("register --username alice --password 1234", "зарегистрирован"),
            ("login --username alice --password wrong", "Неверный пароль"),
            ("login --username alice --password 1234", "Вы вошли"),
            ("deposit --amount nan", "положительным числом"),
            ("deposit --amount 1000", "пополнен"),
            ("buy --currency EUR --amount 10", "Покупка выполнена"),
            ("sell --currency EUR --amount 1", "Продажа выполнена"),
            ("show-portfolio", "ИТОГО: 1,000.00 USD"),
            ("get-rate --from USD --to BTC", "Обратный курс"),
            ("logout", "Вы вышли"),
            ("buy --currency EUR --amount 1", "Сначала выполните login"),
        ):
            with self.subTest(line=line):
                keep_running, output = self.execute(line)
                self.assertTrue(keep_running)
                self.assertIn(message, output)
        self.assertFalse(self.execute("exit")[0])

    def test_corrupted_file_prints_error_and_help_still_works(self):
        path = Path(self.directory.name) / "users.json"
        path.write_text("{broken")
        keep_running, output = self.execute("register --username alice --password 1234")
        self.assertTrue(keep_running)
        self.assertIn("Повреждён файл", output)
        self.assertEqual(path.read_text(), "{broken")
        self.assertIn("Команды:", self.execute("help")[1])

    def test_eof_and_keyboard_interrupt_exit_cleanly(self):
        for error in (EOFError, KeyboardInterrupt):
            with self.subTest(error=error), redirect_stdout(io.StringIO()):
                with patch("builtins.input", side_effect=error):
                    self.cli.run()

    def test_process_restart_preserves_data_but_not_login(self):
        environment = os.environ.copy()
        # Тестируется пакет из текущего окружения, включая установленный wheel
        root = Path(__file__).resolve().parents[1]
        if (root / "valutatrade_hub").is_dir():
            environment["PYTHONPATH"] = str(root)
        commands = (
            "register --username alice --password 1234\n"
            "login --username alice --password 1234\n"
            "deposit --amount 5000\n"
            "buy --currency BTC --amount 0.05\nexit\n"
        )
        for input_text, expected in (
            (commands, "Покупка выполнена"),
            (
                "show-portfolio\nlogin --username alice --password 1234\n"
                "show-portfolio\nexit\n",
                "ИТОГО: 5,000.00 USD",
            ),
        ):
            result = subprocess.run(
                [sys.executable, "-m", "valutatrade_hub"],
                input=input_text,
                capture_output=True,
                text=True,
                cwd=self.directory.name,
                env=environment,
                timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(expected, result.stdout)
            self.assertNotIn("Traceback", result.stdout + result.stderr)
        self.assertIn("Сначала выполните login", result.stdout)
