"""Проверка и согласованная копия SQLite без внешних API."""
import argparse
from contextlib import closing
import asyncio
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time

sys.path.insert(0,str(Path(__file__).resolve().parent.parent))


def private_file(path):
    path=Path(path)
    if not path.is_file() or path.is_symlink():
        raise ValueError('Ожидается обычный файл')
    if os.name!='nt' and path.stat().st_mode & 0o077:
        raise ValueError('Рабочие данные доступны не только владельцу')


def readonly(path):
    return sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True,timeout=10)


def check_database(path):
    with closing(readonly(path)) as conn:
        if conn.execute('PRAGMA quick_check').fetchall()!=[('ok',)]:
            raise ValueError('Проверка SQLite не прошла')
        tables={row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    return {'database':'ok','private_access':'private_users' in tables,'budget':'paid_calls' in tables}


def backup_database(source,target):
    source,target=Path(source),Path(target)
    target.parent.mkdir(parents=True,mode=0o700,exist_ok=True)
    if os.name!='nt' and target.parent.stat().st_mode & 0o077:
        raise ValueError('Каталог копии должен быть закрыт для других пользователей')
    fd=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    os.close(fd)
    started=time.monotonic()
    def progress(*args):
        if time.monotonic()-started>60:
            raise TimeoutError('Не удалось завершить копирование SQLite за 60 секунд')
    try:
        with closing(readonly(source)) as src,closing(sqlite3.connect(target)) as dst:
            src.backup(dst,pages=128,progress=progress,sleep=0.1)
        check_database(target)
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return target


def health(config,database,env_file=None):
    from voicebot.config import load_config
    private_file(config)
    cfg=load_config(config)
    if not cfg.admin_user_ids:
        raise ValueError('Список администраторов пуст')
    if env_file:
        from dotenv import dotenv_values
        private_file(env_file)
        values=dotenv_values(env_file)
        if not values.get('TELEGRAM_BOT_TOKEN') or not values.get('ROUTERAI_API_KEY'):
            raise ValueError('Не заданы необходимые секреты')
    private_file(database)
    return {'config':'ok',**check_database(database)}


async def trial(config,database,env_file):
    from voicebot.db import Database
    health(config,database,env_file)
    with tempfile.TemporaryDirectory(prefix='unbazar_trial_') as tmp:
        copy=backup_database(database,Path(tmp)/'trial.sqlite3')
        db=Database(copy)
        try:
            await db.open()
        finally:
            await db.close()
        return check_database(copy)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['health','trial','backup'])
    parser.add_argument('--database',required=True,type=Path)
    parser.add_argument('--config',type=Path)
    parser.add_argument('--env-file',type=Path)
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    try:
        if args.action=='backup':
            if not args.output:
                parser.error('Укажите --output')
            backup_database(args.database,args.output)
            result={'backup':'ok'}
        elif args.action=='trial':
            result=asyncio.run(trial(args.config,args.database,args.env_file))
        else:
            result=health(args.config,args.database,args.env_file)
        print(json.dumps(result,ensure_ascii=False))
    except Exception as exc:
        print('Проверка не выполнена: '+type(exc).__name__+'. Проверьте пути, права и настройки.',file=sys.stderr)
        return 1
    return 0


if __name__=='__main__':
    raise SystemExit(main())
