"""Ручной и периодический запуск Parser Service отдельным процессом"""

import argparse
from threading import Event

from valutatrade_hub.core.exceptions import ApiRequestError
from valutatrade_hub.infra.settings import SettingsLoader
from valutatrade_hub.parser_service.updater import create_updater, format_update


class RatesScheduler:
    """Запускает первый цикл сразу, затем ждет интервал после каждого цикла"""

    def __init__(self, updater, interval: int = 300, *, stop_event=None):
        """Настраивает обновление с интервалом и сигналом остановки"""
        if type(interval) is not int or interval <= 0:
            raise ValueError("Интервал должен быть положительным целым числом")
        self.updater = updater
        self.interval = interval
        self.stop_event = stop_event if stop_event is not None else Event()

    def run(self):
        """Повторяет обновления до сигнала остановки или Ctrl+C"""
        try:
            while not self.stop_event.is_set():
                try:
                    print(format_update(self.updater.run_update()), flush=True)
                except (ApiRequestError, OSError, ValueError) as error:
                    print(error, flush=True)
                if self.stop_event.wait(self.interval):
                    break
        except KeyboardInterrupt:
            self.stop_event.set()
            print("Планировщик остановлен")


def main():
    """Без --watch выполняет один цикл и возвращает код результата"""
    parser = argparse.ArgumentParser(description="Обновление кэша валютных курсов")
    parser.add_argument("--source", choices=("coingecko", "exchangerate"))
    parser.add_argument(
        "--watch", action="store_true", help="обновлять периодически до Ctrl+C"
    )
    parser.add_argument("--interval", type=int, help="секунды между циклами с --watch")
    args = parser.parse_args()
    if args.interval is not None and (not args.watch or args.interval <= 0):
        parser.error("--interval должен быть положительным и использоваться с --watch")
    try:
        settings = SettingsLoader()
        updater = create_updater(source=args.source, settings=settings)
        if args.watch:
            RatesScheduler(
                updater, args.interval or settings.get("parser_interval_seconds")
            ).run()
        else:
            report = updater.run_update()
            print(format_update(report))
            if report["errors"]:
                raise SystemExit(1)
    except (ApiRequestError, OSError, ValueError) as error:
        print(error)
        raise SystemExit(1) from None
    except KeyboardInterrupt:
        print("Обновление остановлено")
        raise SystemExit(130) from None
