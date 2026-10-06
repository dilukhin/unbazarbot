from functools import lru_cache
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
import os
import platform
import re
import socket
import subprocess
import time

from .version import VERSION

APP_ROOT=Path(__file__).resolve().parent.parent
STARTED=time.monotonic()


@lru_cache(maxsize=1)
def revision():
    # Выпуск без .git получает точный SHA через файл REVISION при подготовке.
    path=APP_ROOT/'REVISION'
    if path.is_file():
        value=path.read_text().strip()
        if re.fullmatch(r'[0-9a-f]{40}',value):
            return value[:12]
    if (APP_ROOT/'.git').exists():
        try:
            result=subprocess.run(['git','-C',str(APP_ROOT),'rev-parse','--short=12','HEAD'],
                                  check=True,capture_output=True,text=True,timeout=2)
            value=result.stdout.strip()
            if re.fullmatch(r'[0-9a-f]{7,40}',value):
                return value
        except (OSError,subprocess.SubprocessError):
            pass
    return 'не определена'


def about_text():
    return (f'Базарбот — голосовые сообщения Telegram в текст через RouterAI.\n'
            f'Версия: {VERSION}; ревизия: {revision()}.\n'
            'https://github.com/dilukhin/unbazarbot')


def system_text(ctx):
    def package(name):
        try:
            return version(name)
        except PackageNotFoundError:
            return 'не определена'
    config=Path(os.getenv('CONFIG_PATH','config.yaml')).resolve()
    database=ctx.db.path.resolve()
    launch='systemd (есть INVOCATION_ID)' if os.getenv('INVOCATION_ID') else 'не определён; признака systemd нет'
    seconds=int(time.monotonic()-STARTED)
    return '\n'.join([
        about_text(),f'Сервер: {socket.gethostname()}',
        f'Имя по настройке: {os.getenv("SERVER_LABEL","не задано")[:200]}; адрес по настройке: {os.getenv("SERVER_ADDRESS","не задан")[:200]}',
        f'Система: {platform.system()} {platform.release()}, {platform.machine()}',
        f'Python: {platform.python_version()}',
        f'aiogram: {package("aiogram")}; httpx: {package("httpx")}',
        f'Процесс: PID {os.getpid()}; время работы {seconds//3600} ч {(seconds//60)%60} мин',
        f'Способ запуска: {launch}',f'Каталог приложения: {APP_ROOT}',
        f'Рабочий каталог: {Path.cwd()}',f'Конфигурация: {config}',f'База: {database}',
    ])
