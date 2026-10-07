"""Разбор команд и вывод результатов; операции выполняет WalletService"""

import shlex

from prettytable import PrettyTable

from valutatrade_hub.core.currencies import (
    CryptoCurrency,
    get_currency,
    get_supported_codes,
)
from valutatrade_hub.core.exceptions import (
    ApiRequestError,
    CurrencyNotFoundError,
    InsufficientFundsError,
)
from valutatrade_hub.core.usecases import WalletService
from valutatrade_hub.parser_service.updater import format_update

COMMAND_OPTIONS = {
    "register": ({"username", "password"}, set()),
    "login": ({"username", "password"}, set()),
    "show-portfolio": (set(), {"base"}),
    "deposit": ({"amount"}, set()),
    "buy": ({"currency", "amount"}, set()),
    "sell": ({"currency", "amount"}, set()),
    "get-rate": ({"from", "to"}, set()),
    "update-rates": (set(), {"source"}),
    "show-rates": (set(), {"currency", "top", "base"}),
    "currencies": (set(), set()),
    "help": (set(), set()),
    "logout": (set(), set()),
    "exit": (set(), set()),
}
HELP = """Команды:
  register --username <имя> --password <пароль>
  login --username <имя> --password <пароль>
  show-portfolio [--base USD]
  deposit --amount <число>                 Пополнить виртуальный USD-баланс
  buy --currency <код> --amount <число>
  sell --currency <код> --amount <число>
  get-rate --from <код> --to <код>
  update-rates [--source coingecko|exchangerate]
  show-rates [--currency <код>] [--top <N>] [--base USD]
  currencies                             Показать поддерживаемые валюты
  logout                                 Выйти из учетной записи
  help                                   Показать справку
  exit                                   Завершить приложение
Сумма buy/sell — количество единиц выбранной валюты.
Имена и пароли с пробелами заключайте в кавычки."""


def parse_command(line: str) -> tuple[str, dict]:
    """Разбирает аргументы без выполнения кода и проверяет список флагов"""
    try:
        tokens = shlex.split(line)
    except ValueError as error:
        raise ValueError("Некорректные кавычки или экранирование в команде") from error
    if not tokens:
        return "", {}
    command = tokens[0]
    if command not in COMMAND_OPTIONS:
        raise ValueError(f"Неизвестная команда '{command}'. Введите help")
    required, optional = COMMAND_OPTIONS[command]
    options = {}
    index = 1
    while index < len(tokens):
        flag, separator, value = tokens[index].partition("=")
        if not flag.startswith("--") or flag[2:] not in required | optional:
            raise ValueError(f"Неизвестный аргумент '{flag}' для команды {command}")
        name = flag[2:]
        if name in options:
            raise ValueError(f"Аргумент --{name} указан повторно")
        if not separator:
            index += 1
            if index >= len(tokens):
                raise ValueError(f"Не указано значение --{name}")
            value = tokens[index]
        options[name] = value
        index += 1
    missing = required - options.keys()
    if missing:
        raise ValueError(
            "Обязательные аргументы: " + ", ".join(f"--{n}" for n in sorted(missing))
        )
    if "amount" in options:
        try:
            options["amount"] = float(options["amount"])
        except ValueError as error:
            raise ValueError("'amount' должен быть положительным числом") from error
    if "top" in options:
        try:
            options["top"] = int(options["top"])
        except ValueError as error:
            raise ValueError("--top должен быть положительным целым числом") from error
    return command, options


class WalletCLI:
    """Интерактивный интерфейс одной пользовательской сессии"""

    def __init__(self, service: WalletService | None = None) -> None:
        """Принимает сервис для приложения или изолированной проверки"""
        self.service = service if service is not None else WalletService()

    def execute(self, line: str) -> bool:
        """Выполняет одну команду; возвращает False только для exit"""
        try:
            command, options = parse_command(line)
            if command == "exit":
                return False
            if not command:
                return True
            if command == "help":
                print(HELP)
            elif command == "register":
                user = self.service.register(options["username"], options["password"])
                print(
                    f"Пользователь '{user.username}' зарегистрирован "
                    f"(id={user.user_id}). "
                    f"Войдите: login --username {shlex.quote(user.username)} "
                    "--password <пароль>"
                )
            elif command == "login":
                user = self.service.login(options["username"], options["password"])
                print(f"Вы вошли как '{user.username}'")
            elif command == "logout":
                self.service.logout()
                print("Вы вышли из учетной записи")
            elif command == "show-portfolio":
                self._print_portfolio(self.service.show_portfolio(options.get("base")))
            elif command == "deposit":
                result = self.service.deposit(options["amount"])
                print(f"Виртуальный баланс пополнен на {result['amount']:,.2f} USD")
                print(
                    f"USD: было {result['before']:,.2f} → стало {result['after']:,.2f}"
                )
            elif command in {"buy", "sell"}:
                method = self.service.buy if command == "buy" else self.service.sell
                self._print_trade(
                    command, method(options["currency"], options["amount"])
                )
            elif command == "currencies":
                print("\n".join(self.service.list_currencies()))
            elif command == "update-rates":
                print("Обновление курсов...")
                print(format_update(self.service.update_rates(options.get("source"))))
            elif command == "show-rates":
                self._print_rates(self.service.show_rates(**options))
            elif command == "get-rate":
                result = self.service.get_rate(options["from"], options["to"])
                print(
                    f"Курс {result['from']}→{result['to']}: {result['rate']:.8f} "
                    f"(обновлено: {result['updated_at']})"
                )
                print(
                    f"Обратный курс {result['to']}→{result['from']}: "
                    f"{1.0 / result['rate']:.8f}"
                )
        except InsufficientFundsError as error:
            print(error)
        except CurrencyNotFoundError as error:
            print(error)
            print("Поддерживаемые коды: " + ", ".join(get_supported_codes()))
        except ApiRequestError as error:
            print(error)
            print("Повторите попытку позже или проверьте подключение к сети")
        except ValueError as error:
            print(error)
        except OSError as error:
            print(f"Ошибка работы с файлами: {error}")
        return True

    @staticmethod
    def _print_rates(report: dict) -> None:
        """Отмечает устаревшие котировки и показывает время каждой пары"""
        print(
            "Курсы из кэша "
            f"(последнее обновление: {report['last_refresh'] or 'не указано'}):"
        )
        table = PrettyTable(["Пара", "Курс", "Получен (UTC)", "Источник", "Статус"])
        for row in report["rates"]:
            table.add_row(
                [
                    row["pair"],
                    f"{row['rate']:.8f}",
                    row["updated_at"],
                    row["source"],
                    "устарел" if row["stale"] else "свежий",
                ]
            )
        print(table)
        if not report["rates"]:
            print("Нет курсов, соответствующих фильтрам")
        if any(row["stale"] for row in report["rates"]):
            print("Есть устаревшие курсы. Выполните 'update-rates' перед торговлей")

    @staticmethod
    def _print_portfolio(report: dict) -> None:
        """Выводит готовую оценку портфеля таблицей"""
        base = report["base"]
        print(f"Портфель пользователя '{report['username']}' (база: {base}):")
        if not report["wallets"]:
            print("Портфель пуст. Пополните баланс: deposit --amount <число>")
            return
        table = PrettyTable(["Валюта", "Баланс", f"Стоимость, {base}"])
        table.align = "r"
        table.align["Валюта"] = "l"
        for row in report["wallets"]:
            digits = (
                4
                if isinstance(get_currency(row["currency_code"]), CryptoCurrency)
                else 2
            )
            table.add_row(
                [
                    row["currency_code"],
                    f"{row['balance']:.{digits}f}",
                    f"{row['value']:,.2f}",
                ]
            )
        print(table)
        print(f"ИТОГО: {report['total']:,.2f} {base}")

    @staticmethod
    def _print_trade(command: str, report: dict) -> None:
        """Показывает сумму сделки и изменения обоих кошельков"""
        action = "Покупка выполнена" if command == "buy" else "Продажа выполнена"
        value_label = "Стоимость покупки" if command == "buy" else "Выручка"
        code = report["currency"]
        print(
            f"{action}: {report['amount']:.4f} {code} "
            f"по курсу {report['rate']:.2f} USD/{code}"
        )
        print("Изменения в портфеле:")
        print(f"- {code}: было {report['before']:.4f} → стало {report['after']:.4f}")
        print(
            f"- USD: было {report['usd_before']:.2f} → стало {report['usd_after']:.2f}"
        )
        print(f"{value_label}: {report['cost']:,.2f} USD")

    def run(self) -> None:
        """Принимает команды до exit, Ctrl+C или конца ввода"""
        print(
            "*** Валютный кошелек ***\n"
            "Курсы из локального кэша; обновление: update-rates"
        )
        print(HELP)
        try:
            self.service.storage.initialize()
        except OSError as error:
            print(f"Ошибка работы с файлами: {error}")
        while True:
            try:
                line = input("> ")
            except (EOFError, KeyboardInterrupt):
                print()
                return
            if not self.execute(line):
                return


def main() -> None:
    """Запускает общий CLI для команд wallet, project и python -m"""
    try:
        WalletCLI().run()
    except (OSError, ValueError) as error:
        print(f"Не удалось запустить приложение: {error}")
        raise SystemExit(1) from error
