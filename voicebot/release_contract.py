"""Read-only compatibility gate for monetary SQLite and prepared releases."""
from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3

SUPPORTED_FINANCIAL_SCHEMA = 1
CONTRACT_FILE = 'runtime-contract.json'


def financial_schema(database):
    path = Path(database)
    if not path.exists():
        return 0
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=10)) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        version = int(bool(tables & {'money_accounts', 'money_journal', 'financial_calls'}))
        if 'money_meta' in tables:
            for (key,) in conn.execute("SELECT key FROM money_meta WHERE key LIKE 'financial-schema:%'"):
                if not re.fullmatch(r'financial-schema:[1-9][0-9]{0,8}', key):
                    raise ValueError('Некорректная версия денежной схемы')
                version = max(version, int(key.split(':')[1]))
        return version


def ensure_supported_database(database):
    if financial_schema(database) > SUPPORTED_FINANCIAL_SCHEMA:
        raise ValueError('Денежная схема новее программы; запуск без записи в SQLite запрещён')


def ensure_release_compatible(database, release):
    required = financial_schema(database)
    path = Path(release) / CONTRACT_FILE
    if not path.exists():
        if required:
            raise ValueError('Выпуск без денежного договора несовместим с финансовой SQLite')
        return
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 4096:
        raise ValueError('Некорректный договор выпуска')
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('Повтор поля договора выпуска')
            result[key] = value
        return result
    value = json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=unique)
    if (not isinstance(value, dict) or set(value) != {'schema', 'project', 'financial_schema'}
            or type(value['schema']) is not int or value['schema'] != 1
            or value['project'] != 'dilukhin/unbazarbot'
            or type(value['financial_schema']) is not int
            or value['financial_schema'] != SUPPORTED_FINANCIAL_SCHEMA
            or required > value['financial_schema']):
        raise ValueError('Несовместимый договор денежной схемы выпуска')
