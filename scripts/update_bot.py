"""Подготовка и управляемое переключение выпуска на сервере Linux."""
import argparse
from datetime import datetime,timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import time
import uuid

sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from scripts.maintenance import backup_database,private_file
from voicebot.runtime import InstanceLock

ORIGINS={'https://github.com/dilukhin/unbazarbot.git','https://github.com/dilukhin/unbazarbot',
         'git@github.com:dilukhin/unbazarbot.git','ssh://git@github.com/dilukhin/unbazarbot.git'}


def run(args,*,cwd=None,binary=False):
    env={**os.environ,'GIT_TERMINAL_PROMPT':'0'}
    result=subprocess.run([str(arg) for arg in args],cwd=cwd,env=env,capture_output=True,
                          text=not binary,timeout=300)
    if result.returncode:
        raise RuntimeError('Операция '+str(args[0])+' не выполнена; вывод скрыт для защиты рабочих данных')
    return result.stdout


def main_revision(repo):
    repo=repo.resolve()
    if not (repo/'.git').exists():
        raise ValueError('Нужна существующая рабочая копия Git')
    if Path(run(['git','-C',repo,'rev-parse','--show-toplevel']).strip()).resolve()!=repo:
        raise ValueError('Корень репозитория не совпадает')
    if run(['git','-C',repo,'config','--get','remote.origin.url']).strip() not in ORIGINS:
        raise ValueError('origin не соответствует dilukhin/unbazarbot')
    run(['git','-C',repo,'fetch','--quiet','origin','main'])
    revision=run(['git','-C',repo,'rev-parse','refs/remotes/origin/main']).strip()
    if not re.fullmatch('[0-9a-f]{40}',revision):
        raise ValueError('Не удалось определить main')
    return revision


def extract_archive(data,release):
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        for member in archive.getmembers():
            path=release/member.name
            if not path.resolve().is_relative_to(release.resolve()) or not (member.isfile() or member.isdir()):
                raise ValueError('Архив содержит небезопасный путь или ссылку')
            member.mode &= 0o777
        # Все элементы уже проверены, включая тип и конечный путь (Python 3.10).
        if hasattr(tarfile,'data_filter'):
            archive.extractall(release,filter='data')
        else:
            archive.extractall(release)


def external_runtime(args,release):
    from dotenv import dotenv_values
    for path in (args.config,args.env_file,args.database):
        private_file(path)
        if path.resolve().is_relative_to(release.resolve()) or path.resolve().is_relative_to(args.repo.resolve()):
            raise ValueError('Рабочие данные должны находиться вне выпуска и Git')
    values=dotenv_values(args.env_file)
    for key,path in (('CONFIG_PATH',args.config),('DB_PATH',args.database)):
        if not values.get(key) or not Path(values[key]).is_absolute() or Path(values[key]).resolve()!=path.resolve():
            raise ValueError('В рабочем .env нужны совпадающие абсолютные CONFIG_PATH и DB_PATH')


def code_hashes(release):
    result={}
    for path in release.rglob('*'):
        parts=path.relative_to(release).parts
        if path.is_file() and not any(part in {'.venv','__pycache__','.pytest_cache','.ready'} for part in parts):
            result[str(path.relative_to(release))]=hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def prepare(args,revision):
    if args.revision!=revision:
        raise ValueError('Выбранный SHA должен совпадать с текущим origin/main; повторите check')
    releases=args.releases.resolve()
    releases.mkdir(parents=True,mode=0o700,exist_ok=True)
    if releases.stat().st_mode & 0o077:
        raise ValueError('Каталог выпусков должен быть закрыт для других пользователей')
    release=releases/revision
    external_runtime(args,release)
    if release.exists():
        ready=release/'.ready'
        if not ready.is_file():
            raise ValueError('Незавершённый выпуск уже существует; проверьте его отдельно')
        manifest=json.loads(ready.read_text())
        if manifest.get('revision')!=revision or manifest.get('files')!=code_hashes(release):
            raise ValueError('Подготовленный выпуск изменён')
    else:
        release.mkdir(mode=0o700)
        data=run(['git','-C',args.repo,'archive','--format=tar',revision],binary=True)
        extract_archive(data,release)
        (release/'REVISION').write_text(revision+'\n')
        run([sys.executable,'-m','venv',release/'.venv'])
        python=release/'.venv/bin/python'
        run([python,'-m','pip','install','-r',release/'requirements.lock'])
        run([python,'-m','compileall','-q','bot.py','voicebot','scripts','tests'],cwd=release)
        run([python,'-m','pytest','-q'],cwd=release)
        (release/'.ready').write_text(json.dumps({'revision':revision,'files':code_hashes(release)}))
    python=release/'.venv/bin/python'
    run([python,release/'scripts/maintenance.py','trial','--config',args.config,
         '--database',args.database,'--env-file',args.env_file],cwd=release)
    return release


def switch_link(current,target):
    if not current.is_symlink():
        raise ValueError('current должен быть подготовленной символической ссылкой')
    temporary=current.with_name(current.name+'.switch-'+uuid.uuid4().hex)
    try:
        temporary.symlink_to(target,target_is_directory=True)
        os.replace(temporary,current)
    finally:
        temporary.unlink(missing_ok=True)


def private_copy(source,target):
    fd=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'wb') as dst,Path(source).open('rb') as src:
        shutil.copyfileobj(src,dst)


def apply(args,release):
    current=args.current.absolute()
    if not current.is_symlink() or not current.resolve().is_dir():
        raise ValueError('Сначала настройте управляемую ссылку current и службу по инструкции')
    previous=current.resolve()
    if previous==release:
        print('Эта ревизия уже выбрана. Проверьте /about и /system.')
        return
    if os.geteuid()!=args.database.stat().st_uid:
        raise ValueError('Запускайте скрипт от владельца рабочей базы')
    prefix=['sudo','-n'] if args.sudo else []
    def ctl(*values):
        return run(prefix+['systemctl',*values,args.service]).strip()
    if ctl('show','--property=WorkingDirectory','--value')!=str(current):
        raise ValueError('WorkingDirectory службы должен указывать на current')
    if str(current/'.venv/bin/python') not in ctl('show','--property=ExecStart','--value'):
        raise ValueError('ExecStart службы должен использовать окружение current')
    environment=ctl('show','--property=EnvironmentFiles','--value')
    if environment!=str(args.env_file)+' (ignore_errors=no)':
        raise ValueError('Нужен единственный EnvironmentFile с рабочими путями')
    if ctl('is-active')!='active':
        raise ValueError('Исходная служба не активна; требуется отдельная диагностика')
    args.backups.mkdir(parents=True,mode=0o700,exist_ok=True)
    if args.backups.stat().st_mode & 0o077:
        raise ValueError('Каталог резервных копий должен быть закрыт')
    backup=args.backups/(datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid.uuid4().hex[:8])
    backup.mkdir(mode=0o700)
    private_copy(args.config,backup/'config.yaml')
    private_copy(args.env_file,backup/'bot.env')
    (backup/'release.json').write_text(json.dumps({'previous':str(previous),'new':str(release),'service':args.service}))
    ctl('stop')
    state=ctl('show','--property=ActiveState','--value')
    if state not in {'inactive','failed'}:
        raise RuntimeError('Прежняя служба не остановлена; переключение запрещено')
    # Если ошибка здесь, новый процесс ещё не запускался: прежнюю службу можно вернуть.
    try:
        backup_database(args.database,backup/'bot.sqlite3')
        switch_link(current,release)
    except BaseException:
        if current.resolve()!=previous:
            switch_link(current,previous)
        ctl('start')
        raise
    try:
        ctl('start')
        restarts=ctl('show','--property=NRestarts','--value')
        for _ in range(10):
            time.sleep(1)
            if ctl('is-active')!='active' or ctl('show','--property=NRestarts','--value')!=restarts:
                raise RuntimeError('Новая служба не удержалась в активном состоянии')
        run([current/'.venv/bin/python',current/'scripts/maintenance.py','health',
             '--config',args.config,'--database',args.database,'--env-file',args.env_file],cwd=current)
    except BaseException:
        ctl('stop')
        switch_link(current,previous)
        # После запуска нельзя исключить новые сообщения; БД не перезаписывается.
        print('Обновление остановлено. Предыдущий код возвращён, служба оставлена остановленной.\n'
              'Рабочая база сохранена. Для восстановления следуйте инструкции; копии: '+str(backup),file=sys.stderr)
        raise
    print('Установлен выпуск '+release.name+'. Служба активна, SQLite проверена.\n'
          'Подтвердите /about, /system и одно тестовое голосовое в Telegram. Копии: '+str(backup))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['check','prepare','apply'])
    parser.add_argument('--repo',required=True,type=Path)
    parser.add_argument('--revision')
    for field in ('current','releases','config','env-file','database','backups'):
        parser.add_argument('--'+field,type=Path)
    parser.add_argument('--service',default='unbazarbot.service')
    parser.add_argument('--sudo',action='store_true',help='systemctl через sudo -n с заранее выданными правами')
    args=parser.parse_args()
    try:
        if os.name=='nt':
            raise ValueError('Скрипт запускается на сервере Linux; с Windows используйте ssh_relay')
        if args.action=='check':
            print('Последняя ревизия main: '+main_revision(args.repo))
            return 0
        required=('revision','current','releases','config','env_file','database','backups')
        if any(getattr(args,key) is None for key in required):
            parser.error('Для prepare/apply нужны SHA и все пути рабочего экземпляра')
        if not re.fullmatch('[0-9a-f]{40}',args.revision):
            raise ValueError('Укажите полный SHA из check')
        if not re.fullmatch(r'[A-Za-z0-9_.@-]+\.service',args.service):
            raise ValueError('Укажите имя systemd службы')
        args.releases=args.releases.resolve()
        args.releases.mkdir(parents=True,mode=0o700,exist_ok=True)
        with InstanceLock(args.releases/'.update.lock'):
            release=prepare(args,main_revision(args.repo))
            if args.action=='apply':
                apply(args,release)
            else:
                print('Выпуск подготовлен без остановки бота: '+str(release))
    except Exception as exc:
        print('Обновление не выполнено: '+str(exc),file=sys.stderr)
        return 1
    return 0


if __name__=='__main__':
    raise SystemExit(main())
