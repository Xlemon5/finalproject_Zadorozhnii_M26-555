"""История измерений и атомарное обновление снимка курсов"""

import json
from pathlib import Path

from valutatrade_hub.core.rates import parse_timestamp, validate_cache, validate_pair
from valutatrade_hub.core.utils import JsonStorage, validate_number
from valutatrade_hub.infra.file_lock import file_lock
from valutatrade_hub.parser_service.config import ParserConfig


def validate_record(record: dict) -> dict:
    """Проверяет запись целиком до записи любого файла"""
    try:
        source, target = validate_pair(
            f"{record['from_currency']}_{record['to_currency']}"
        )
        timestamp = (
            parse_timestamp(record["timestamp"]).isoformat().replace("+00:00", "Z")
        )
        if record["id"] != f"{source}_{target}_{timestamp}":
            raise ValueError
        if not isinstance(record["source"], str) or not record["source"].strip():
            raise ValueError
        if not isinstance(record.get("meta", {}), dict):
            raise ValueError
        result = {
            **record,
            "rate": validate_number(record["rate"], positive=True),
            "timestamp": timestamp,
        }
        json.dumps(result, allow_nan=False)
        return result
    except (KeyError, TypeError, ValueError, OverflowError) as error:
        raise ValueError("Некорректная запись истории курсов") from error


class ParserStorage:
    """Сохраняет все измерения; более старый ответ не заменяет свежий курс"""

    def __init__(self, config: ParserConfig):
        self.rates_path = Path(config.RATES_FILE_PATH).resolve()
        self.history_path = Path(config.HISTORY_FILE_PATH).resolve()

    @staticmethod
    def _load(path, kind):
        return JsonStorage(path.parent).load(path.name, kind)

    @staticmethod
    def _save(path, data):
        JsonStorage(path.parent).save(path.name, data)

    def save(self, records: list[dict], *, last_refresh: str) -> dict:
        """Обновляет файлы под общей межпроцессной блокировкой кэша"""
        records = [validate_record(record) for record in records]
        parse_timestamp(last_refresh)
        if not records:
            raise ValueError("Нет измерений для сохранения")
        lock = self.rates_path.with_name(f".{self.rates_path.name}.lock")
        with file_lock(lock):
            history = self._load(self.history_path, list)
            by_id = {}
            for entry in history:
                entry = validate_record(entry)
                if entry["id"] in by_id:
                    raise ValueError("Повторяющийся ID в истории курсов")
                by_id[entry["id"]] = entry
            cache = validate_cache(self._load(self.rates_path, dict))
            pairs = cache["pairs"]
            changed = 0
            for record in records:
                old = by_id.get(record["id"])
                if old is not None and (old["rate"], old["source"]) != (
                    record["rate"],
                    record["source"],
                ):
                    raise ValueError("Конфликт измерений с одинаковым ID")
                by_id.setdefault(record["id"], record)
                pair = f"{record['from_currency']}_{record['to_currency']}"
                current = pairs.get(pair)
                if current is None or parse_timestamp(
                    record["timestamp"]
                ) > parse_timestamp(current["updated_at"]):
                    pairs[pair] = {
                        "rate": record["rate"],
                        "updated_at": record["timestamp"],
                        "source": record["source"],
                    }
                    changed += 1
            previous_refresh = cache.get("last_refresh")
            if previous_refresh is None or parse_timestamp(
                last_refresh
            ) > parse_timestamp(previous_refresh):
                cache["last_refresh"] = last_refresh
            # Сначала история: при сбое кэша сохраненные измерения не теряются
            self._save(self.history_path, list(by_id.values()))
            self._save(self.rates_path, cache)
            return {"updated": changed, "last_refresh": cache["last_refresh"]}
