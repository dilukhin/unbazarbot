import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts.maintenance import backup_database,check_database,private_file,trial
from scripts.update_bot import apply,extract_archive,main_revision,prepare,switch_link
from voicebot.runtime import InstanceLock


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.database=self.root/'bot.sqlite3'
        self.conn=sqlite3.connect(self.database)
        self.conn.execute('PRAGMA journal_mode=WAL')
        self.conn.execute('CREATE TABLE test(value TEXT)');self.conn.execute("INSERT INTO test VALUES('preserved')");self.conn.commit()
        os.chmod(self.database,0o600)

    def tearDown(self):
        self.conn.close();self.tmp.cleanup()

    def test_backup_includes_uncheckpointed_wal_and_restores_independently(self):
        target=self.root/'private'/'copy.sqlite3'
        backup_database(self.database,target)
        self.assertEqual(check_database(target)['database'],'ok')
        with sqlite3.connect(target) as restored:
            self.assertEqual(restored.execute('SELECT value FROM test').fetchone()[0],'preserved')
        self.assertEqual(target.stat().st_mode & 0o777,0o600)
        self.assertEqual(target.parent.stat().st_mode & 0o777,0o700)
        with self.assertRaises(FileExistsError):
            backup_database(self.database,target)

    def test_backup_does_not_create_missing_source_or_keep_bad_copy(self):
        source=self.root/'missing.sqlite3';target=self.root/'private'/'copy.sqlite3'
        with self.assertRaises(sqlite3.OperationalError):
            backup_database(source,target)
        self.assertFalse(source.exists());self.assertFalse(target.exists())

    def test_world_readable_working_file_is_rejected(self):
        os.chmod(self.database,0o644)
        with self.assertRaises(ValueError):
            private_file(self.database)

    def test_instance_lock_blocks_another_process_and_releases_after_exit(self):
        path=self.root/'instance.lock'
        code='from voicebot.runtime import InstanceLock; import sys\nwith InstanceLock(sys.argv[1]): pass'
        with InstanceLock(path):
            result=subprocess.run([sys.executable,'-c',code,str(path)],capture_output=True)
            self.assertNotEqual(result.returncode,0)
        result=subprocess.run([sys.executable,'-c',code,str(path)],capture_output=True)
        self.assertEqual(result.returncode,0)

    def test_unsafe_archive_paths_and_links_are_rejected(self):
        for name,kind in (('../escape',tarfile.REGTYPE),('/absolute',tarfile.REGTYPE),('link',tarfile.SYMTYPE)):
            buffer=io.BytesIO()
            with tarfile.open(fileobj=buffer,mode='w') as tar:
                member=tarfile.TarInfo(name);member.type=kind;tar.addfile(member)
            release=self.root/'release';release.mkdir(exist_ok=True)
            with self.subTest(name=name),self.assertRaises(ValueError):
                extract_archive(buffer.getvalue(),release)

    def test_wrong_origin_is_rejected_before_fetch(self):
        repo=self.root/'repo';(repo/'.git').mkdir(parents=True)
        def fake(args,**kwargs):
            return str(repo) if '--show-toplevel' in args else 'https://example.invalid/other.git'
        with patch('scripts.update_bot.run',side_effect=fake) as run:
            with self.assertRaises(ValueError):
                main_revision(repo)
            self.assertFalse(any('fetch' in call.args[0] for call in run.call_args_list))

    def args(self):
        releases=self.root/'releases';releases.mkdir(mode=0o700)
        previous=releases/'old';previous.mkdir();new=releases/'new';new.mkdir()
        current=self.root/'current';current.symlink_to(previous,target_is_directory=True)
        config=self.root/'config.yaml';config.write_text('test');os.chmod(config,0o600)
        env=self.root/'bot.env';env.write_text('test');os.chmod(env,0o600)
        args=SimpleNamespace(current=current,config=config,env_file=env,database=self.database,
            backups=self.root/'backups',sudo=False,service='unbazarbot.service')
        return args,previous,new

    def controller(self,args,*,fail=False):
        operations=[]
        def run(command,**kwargs):
            command=list(map(str,command));operations.append(command)
            if 'WorkingDirectory' in ' '.join(command): return str(args.current)
            if 'ExecStart' in ' '.join(command): return str(args.current/'.venv/bin/python')
            if 'EnvironmentFiles' in ' '.join(command): return str(args.env_file)+' (ignore_errors=no)'
            if 'ActiveState' in ' '.join(command): return 'inactive'
            if 'NRestarts' in ' '.join(command): return '0'
            if 'is-active' in command: return 'active'
            if fail and 'start' in command:
                self.conn.execute("INSERT INTO test VALUES('new_message')");self.conn.commit()
                raise RuntimeError('simulated startup failure')
            return ''
        return operations,run

    def test_successful_switch_stops_before_start_and_keeps_runtime(self):
        args,previous,new=self.args();operations,controller=self.controller(args)
        with patch('scripts.update_bot.run',side_effect=controller),patch('scripts.update_bot.time.sleep'):
            apply(args,new)
        self.assertEqual(args.current.resolve(),new)
        self.assertLess(next(i for i,x in enumerate(operations) if 'stop' in x),next(i for i,x in enumerate(operations) if 'start' in x))
        copies=list(args.backups.rglob('bot.sqlite3'));self.assertEqual(len(copies),1)
        self.assertEqual(check_database(copies[0])['database'],'ok')

    def test_failed_start_restores_code_without_overwriting_new_data_or_starting_old(self):
        args,previous,new=self.args();operations,controller=self.controller(args,fail=True)
        with patch('scripts.update_bot.run',side_effect=controller),patch('scripts.update_bot.time.sleep'):
            with self.assertRaises(RuntimeError): apply(args,new)
        self.assertEqual(args.current.resolve(),previous)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM test').fetchone()[0],2)
        self.assertEqual(sum('start' in call for call in operations),1)
        self.assertEqual(operations[-1][1],'stop')

    def test_backup_failure_before_new_start_resumes_previous_service(self):
        args,previous,new=self.args();operations,controller=self.controller(args)
        with patch('scripts.update_bot.run',side_effect=controller),patch('scripts.update_bot.backup_database',side_effect=OSError):
            with self.assertRaises(OSError): apply(args,new)
        self.assertEqual(args.current.resolve(),previous)
        self.assertEqual(sum('start' in call for call in operations),1)

    def test_revision_mismatch_does_not_prepare_or_stop_anything(self):
        args=SimpleNamespace(revision='a'*40)
        with patch('scripts.update_bot.run') as run:
            with self.assertRaises(ValueError): prepare(args,'b'*40)
            run.assert_not_called()

    def test_prepare_keeps_live_data_and_rejects_modified_release(self):
        sha='a'*40
        repo=self.root/'repo';repo.mkdir()
        releases=self.root/'releases'
        config=self.root/'config.yaml';config.write_text('test');os.chmod(config,0o600)
        env=self.root/'bot.env';env.write_text(f'CONFIG_PATH={config}\nDB_PATH={self.database}\n');os.chmod(env,0o600)
        args=SimpleNamespace(repo=repo,releases=releases,config=config,env_file=env,database=self.database,revision=sha)
        buffer=io.BytesIO()
        with tarfile.open(fileobj=buffer,mode='w') as tar:
            for name in ('bot.py','requirements.lock','scripts/maintenance.py'):
                body=b'# synthetic fixture\n';member=tarfile.TarInfo(name);member.size=len(body);tar.addfile(member,io.BytesIO(body))
        def run(command,**kwargs):
            return buffer.getvalue() if 'archive' in command else ''
        with patch('scripts.update_bot.run',side_effect=run) as operations:
            release=prepare(args,sha)
            self.assertTrue((release/'.ready').is_file())
            self.assertEqual((release/'REVISION').read_text().strip(),sha)
            self.assertFalse(any('systemctl' in call.args[0] for call in operations.call_args_list))
            self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM test').fetchone()[0],1)
            (release/'bot.py').write_text('changed')
            with self.assertRaises(ValueError): prepare(args,sha)

    def test_trial_migration_does_not_modify_working_database(self):
        import asyncio
        config=self.root/'config.yaml'
        config.write_text('telegram:\n  admin_user_ids: [1]\nstt:\n  default_model: t\n  models:\n    t:\n      provider_model: test\n');os.chmod(config,0o600)
        env=self.root/'bot.env';env.write_text('TELEGRAM_BOT_TOKEN=TEST_ONLY\nROUTERAI_API_KEY=TEST_ONLY\n');os.chmod(env,0o600)
        result=asyncio.run(trial(config,self.database,env))
        self.assertTrue(result['private_access']);self.assertTrue(result['budget'])
        self.assertNotIn('paid_calls',{row[0] for row in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")})


if __name__=='__main__':
    unittest.main()
