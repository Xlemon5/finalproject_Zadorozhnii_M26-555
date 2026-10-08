"""Запись настоящего CLI в формате asciicast v2 на macOS/Linux"""

import argparse
import codecs
import getpass
import json
import os
import pty
import select
import subprocess
import sys
import tempfile
import termios
import time
from pathlib import Path

from valutatrade_hub.parser_service.config import get_api_key

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TERMINAL_WIDTH = 132
TERMINAL_HEIGHT = 30
COMMAND_TIMEOUT = 40
EXPECTED_OUTPUT = (
    "Локальный кэш курсов пуст",
    "Пользователь 'demo' зарегистрирован",
    "Вы вошли как 'demo'",
    "Обновлено курсов: 6",
    "Покупка выполнена",
    "Продажа выполнена",
    "ИТОГО: 10,000.00 USD",
    "Обратный курс BTC→USD",
    "RUB_USD",
    "Недостаточно средств",
    "Неизвестная валюта 'ABC'",
    "'amount' должен быть положительным числом",
    "Сначала выполните login",
)


def record_session(
    commands: list[str], pause: float, *, live: bool = False
) -> list[list]:
    """Собирает реальный вывод дочернего процесса и проверяет полный сценарий"""
    events = []
    started = time.monotonic()
    decoder = codecs.getincrementaldecoder("utf-8")()
    with tempfile.TemporaryDirectory(prefix="valutatrade-demo-") as directory:
        master, slave = pty.openpty()
        termios.tcsetwinsize(slave, (TERMINAL_HEIGHT, TERMINAL_WIDTH))
        environment = {**os.environ, "TERM": "xterm-256color", "PYTHONUNBUFFERED": "1"}
        process = subprocess.Popen(
            [sys.executable, "-m", "valutatrade_hub"],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            cwd=directory,
            env=environment,
        )
        os.close(slave)

        def read_output(*, until_exit=False):
            """Ждет готовности CLI, сохраняя полученные байты и их время"""
            received = ""
            deadline = time.monotonic() + COMMAND_TIMEOUT
            while time.monotonic() < deadline:
                ready, _, _ = select.select([master], [], [], 0.1)
                if ready:
                    try:
                        data = os.read(master, 65536)
                    except OSError:
                        data = b""
                    if not data:
                        break
                    text = decoder.decode(data)
                    if text:
                        if live:
                            sys.stdout.write(text)
                            sys.stdout.flush()
                        events.append([round(time.monotonic() - started, 6), "o", text])
                        received += text
                    if not until_exit and received.endswith("> "):
                        return
                elif process.poll() is not None:
                    break
            if until_exit and process.wait(timeout=5) == 0:
                return
            raise RuntimeError(
                "CLI завершился раньше времени или не вернул приглашение"
            )

        try:
            read_output()
            for command in commands:
                time.sleep(pause)
                events.append(
                    [round(time.monotonic() - started, 6), "m", command.split()[0]]
                )
                os.write(master, (command + "\n").encode())
                read_output(until_exit=command == "exit")
            files = {path.name for path in (Path(directory) / "data").glob("*.json")}
            if files != {
                "users.json",
                "portfolios.json",
                "rates.json",
                "exchange_rates.json",
            }:
                raise RuntimeError("Сценарий не создал все четыре файла данных")
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)
            os.close(master)
    transcript = "".join(event[2] for event in events if event[1] == "o")
    missing = [fragment for fragment in EXPECTED_OUTPUT if fragment not in transcript]
    if missing:
        raise RuntimeError("Не выполнены шаги демо: " + ", ".join(missing))
    for name in ("EXCHANGERATE_API_KEY", "COINGECKO_API_KEY"):
        secret = os.getenv(name)
        if secret and secret in transcript:
            raise RuntimeError("Запись содержит API-ключ и не будет сохранена")
    return events


def main() -> None:
    """Записывает демо в .cast и текстовый файл без личных данных пользователя"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=PROJECT_ROOT / "docs/demo.cast")
    parser.add_argument(
        "--prompt-key", action="store_true", help="скрытый ввод ключа ExchangeRate-API"
    )
    parser.add_argument(
        "--pause", type=float, default=2, help="пауза перед командой в секундах"
    )
    parser.add_argument(
        "--live", action="store_true", help="показывать сеанс в текущем терминале"
    )
    args = parser.parse_args()
    if not 0 <= args.pause <= 10:
        parser.error("--pause должен быть от 0 до 10 секунд")
    if args.prompt_key:
        os.environ["EXCHANGERATE_API_KEY"] = getpass.getpass("ExchangeRate-API key: ")
    for name in ("EXCHANGERATE_API_KEY", "COINGECKO_API_KEY"):
        os.environ.setdefault(name, get_api_key(name))
    if not os.getenv("EXCHANGERATE_API_KEY"):
        parser.error("Задайте EXCHANGERATE_API_KEY или используйте --prompt-key")
    commands = (PROJECT_ROOT / "docs/demo.commands").read_text().splitlines()
    timestamp = int(time.time())
    events = record_session(commands, args.pause, live=args.live)
    header = {
        "version": 2,
        "width": TERMINAL_WIDTH,
        "height": TERMINAL_HEIGHT,
        "timestamp": timestamp,
        "duration": events[-1][0],
        "title": "ValutaTrade Hub: реальные API, торговля и ошибки",
        "command": "python -m valutatrade_hub",
        "env": {"TERM": "xterm-256color"},
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        "\n".join(json.dumps(item, ensure_ascii=False) for item in [header, *events])
        + "\n"
    )
    transcript = "".join(event[2] for event in events if event[1] == "o")
    args.output.with_suffix(".txt").write_text(transcript.replace("\r\n", "\n"))
    print(f"Записано {len(commands)} команд: {args.output}")


if __name__ == "__main__":
    main()
