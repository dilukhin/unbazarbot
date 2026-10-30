import asyncio
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts.maintenance import backup_database, check_database, trial
from scripts.update_bot import external_runtime, prepare
from voicebot.db import Database
from voicebot.money import MoneySettings, to_atoms
from voicebot.release_contract import ensure_release_compatible, financial_schema
from voicebot.runtime_paths import validate_tariff_path
from voicebot.tariffs import PriceCatalog, TariffError
from tests.tariff_fixtures import NOW, document


class RuntimePathTests(unittest.TestCase):
    def test_cache_and_sidecars_cannot_alias_database_secrets_or_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);db=root/'bot.sqlite3';config=root/'config.yaml';env=root/'bot.env'
            for p in (db,config,env):p.write_bytes(b'synthetic')
            def validate(cache,**kwargs):
                return validate_tariff_path(cache,database=db,config=config,env_file=env,**kwargs)
            for path in (db,Path(str(db)+'-wal'),Path(str(db)+'-shm'),Path(str(db)+'.lock'),config,env):
                with self.subTest(path=path),self.assertRaises(ValueError):validate(path)
            price=root/'prices.json'
            for suffix in ('.lock','.paused'):
                with self.subTest(suffix=suffix),self.assertRaises(ValueError):
                    validate(price,lock=Path(str(price)+suffix))
            for path in (root/'linked',root/'hardlinked'):
                if path.name=='linked':path.symlink_to(db)
                else:os.link(db,path)
                with self.subTest(path=path),self.assertRaises(ValueError):validate(path)
            with self.assertRaises(ValueError):validate(Path('relative.json'))
            with self.assertRaises(ValueError):validate(root/'source'/'prices.json',application_roots=(root/'source',))
            self.assertEqual(validate(price),price)
            self.assertEqual(db.read_bytes(),b'synthetic')

    def test_valid_remote_document_cannot_overwrite_sqlite_even_with_wrong_call_site(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'wrong.json'
            with sqlite3.connect(path) as conn:
                conn.execute('CREATE TABLE synthetic(value TEXT)')
                conn.execute("INSERT INTO synthetic VALUES('preserve')")
            before=path.read_bytes()
            service=PriceCatalog(path,clock=lambda:NOW)
            with self.assertRaises(TariffError):service._accept(json.dumps(document()).encode())
            self.assertEqual(path.read_bytes(),before)

    def test_updater_checks_actual_environment_file_and_all_release_roots(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);repo=root/'source';repo.mkdir();releases=root/'releases';releases.mkdir()
            db=root/'bot.sqlite3';config=root/'config.yaml';env=root/'bot.env'
            for p in (db,config):p.write_text('synthetic');p.chmod(0o600)
            args=SimpleNamespace(repo=repo,releases=releases,database=db,config=config,env_file=env)
            for bad in (env,db,releases/'other'/'prices.json',repo/'prices.json'):
                env.write_text(f'CONFIG_PATH={config}\nDB_PATH={db}\nTARIFF_CACHE_PATH={bad}\n');env.chmod(0o600)
                with self.subTest(path=bad),self.assertRaises(ValueError):external_runtime(args,releases/'new')


class ReleaseContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_future_schema_refused_before_any_write_but_backup_remains_available(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'future.sqlite3'
            with sqlite3.connect(path) as conn:
                conn.execute('CREATE TABLE money_meta(key TEXT PRIMARY KEY)')
                conn.execute("INSERT INTO money_meta VALUES('financial-schema:2')")
            before=path.read_bytes()
            db=Database(path)
            with self.assertRaises(ValueError):await db.open()
            self.assertIsNone(db.conn);self.assertEqual(path.read_bytes(),before)
            self.assertFalse(Path(str(path)+'-wal').exists())
            copy=Path(tmp)/'backup'/'future.sqlite3';backup_database(path,copy)
            self.assertEqual(check_database(copy)['financial_schema'],2)

    async def test_legacy_and_corrupt_or_foreign_release_contracts_cannot_bypass_money(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);path=root/'safe.sqlite3';db=Database(path)
            await db.open();await db.close()
            self.assertEqual(financial_schema(path),1)
            release=root/'old';release.mkdir()
            with self.assertRaises(ValueError):ensure_release_compatible(path,release)
            contract=release/'runtime-contract.json'
            source=json.loads(Path('runtime-contract.json').read_text())
            for field,value in (('schema',True),('project','foreign'),('financial_schema',0),('financial_schema',True),('financial_schema',2)):
                invalid={**source,field:value};contract.write_text(json.dumps(invalid))
                with self.subTest(field=field,value=value),self.assertRaises(ValueError):
                    ensure_release_compatible(path,release)
            contract.write_text('{"schema":1,"schema":1}')
            with self.assertRaises(ValueError):ensure_release_compatible(path,release)
            contract.write_text(json.dumps(source));ensure_release_compatible(path,release)

    async def test_migration_on_wal_copy_and_restart_preserve_legacy_cache_rights_and_money(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);path=root/'legacy.sqlite3'
            # Pre-money schema, including a legacy REAL cost and a private permission.
            with sqlite3.connect(path) as conn:
                conn.execute('''CREATE TABLE private_users(user_id INTEGER PRIMARY KEY,username TEXT,first_name TEXT,
                    status TEXT NOT NULL,approved_by_user_id INTEGER,approved_at TEXT,revoked_by_user_id INTEGER,
                    revoked_at TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL)''')
                conn.execute("INSERT INTO private_users(user_id,status,created_at,updated_at) VALUES(7,'approved','2026-09-30T10:00:00+00:00','2026-09-30T10:00:00+00:00')")
                conn.execute('''CREATE TABLE transcription_jobs(id TEXT PRIMARY KEY,chat_id INTEGER,user_id INTEGER,message_id INTEGER,
                    file_id TEXT,file_unique_id TEXT,model_alias TEXT,provider_model TEXT,status TEXT,transcript TEXT,cost REAL,
                    duration_seconds REAL,error TEXT,created_at TEXT,updated_at TEXT)''')
                conn.execute("INSERT INTO transcription_jobs(id,file_unique_id,model_alias,provider_model,status,transcript,cost,created_at,updated_at) VALUES('old','file','alias','synthetic/asr','done','artificial text',0.123,'2026-09-30T10:00:00+00:00','2026-09-30T10:00:00+00:00')")
            path.chmod(0o600)
            config=root/'config.yaml';config.write_text('telegram:\n  admin_user_ids: [1]\nstt:\n  default_model: t\n  models:\n    t:\n      provider_model: synthetic/asr\n');config.chmod(0o600)
            env=root/'bot.env';env.write_text('TELEGRAM_BOT_TOKEN=TEST_ONLY\nROUTERAI_API_KEY=TEST_ONLY\n');env.chmod(0o600)
            before=path.read_bytes();result=await trial(config,path,env)
            self.assertEqual(result['financial_schema'],1);self.assertTrue(result['money'] and result['financial'])
            self.assertEqual(path.read_bytes(),before);self.assertEqual(financial_schema(path),0)
            working=root/'copy'/'test.sqlite3';backup_database(path,working)
            db=Database(working,MoneySettings(global_daily_rub='10'))
            await db.open()
            try:
                # The snapshot includes last_seen: observe both sides at the same
                # explicit time, independently of a wall-clock second boundary.
                await db.upsert_config_admins({1});await db.touch_money_user(7,at=NOW)
                await db.adjust_money('extra','user',7,'12.345678901',admin_id=1,at=NOW)
                await db.reserve_money('unknown','user',7,'1.23456789001',at=NOW)
                balance=await db.money_balance('user',7,at=NOW)
                count=(await (await db.db.execute('SELECT COUNT(*) FROM money_journal')).fetchone())[0]
                self.assertEqual(balance['held_atoms'],to_atoms('1.23456789001',rub=True))
                self.assertEqual((await db.access_subject('user',7))['status'],'approved')
                self.assertEqual((await db.cached_transcript('file','alias','synthetic/asr'))['transcript'],'artificial text')
                await db.close();await db.open()
                again=await db.money_balance('user',7,at=NOW)
                self.assertEqual(again,balance)
                self.assertEqual((await (await db.db.execute('SELECT COUNT(*) FROM money_journal')).fetchone())[0],count)
                self.assertEqual((await (await db.db.execute("SELECT cost,cost_rub FROM transcription_jobs WHERE id='old'")).fetchone())[:],(0.123,None))
            finally:await db.close()

    async def test_prepare_rejects_old_code_before_running_candidate_tests_or_trial(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);db=Database(root/'money.sqlite3');await db.open();await db.close()
            release=root/'releases'/('a'*40);release.mkdir(parents=True,mode=0o700)
            release.parent.chmod(0o700)
            (release/'.ready').write_text(json.dumps({'revision':'a'*40,'files':{}}))
            args=SimpleNamespace(revision='a'*40,releases=release.parent,database=db.path)
            with patch('scripts.update_bot.external_runtime'),patch('scripts.update_bot.run') as run:
                with self.assertRaisesRegex(ValueError,'без денежного договора'):prepare(args,'a'*40)
                run.assert_not_called()


if __name__=='__main__':unittest.main()
