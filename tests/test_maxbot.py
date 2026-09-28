import asyncio
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import httpx

from maxbot.app import MaxBot, audio_attachment, create_app, infer_format, media_url
from maxbot.config import MaxConfig, load_max_config
from maxbot.storage import MaxStorage


class MaxConfigTests(unittest.TestCase):
    def test_example_requires_admin_and_never_opens_public_private_access(self):
        with self.assertRaises(ValueError):
            load_max_config("max_config.example.yaml")

    def test_media_url_is_limited_to_configured_https_hosts(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text("max:\n  admin_user_ids: [42]\n  download_hosts: [vu.okcdn.ru]\n"
                            "stt:\n  default_model: w\n  models:\n    w:\n"
                            "      provider_model: example/model\n", encoding="utf-8")
            config = load_max_config(path)
            self.assertEqual(media_url({"payload": {"url": "https://vu.okcdn.ru/a"}}, config),
                             "https://vu.okcdn.ru/a")
            for url in ("http://vu.okcdn.ru/a", "https://vu.okcdn.ru.evil.test/a",
                        "https://127.0.0.1/a", "https://user@vu.okcdn.ru/a"):
                with self.subTest(url=url), self.assertRaises(ValueError):
                    media_url({"payload": {"url": url}}, config)
            self.assertTrue(config.private_admin_only)

    def test_mp3_rejected_and_audio_file_selected(self):
        message = {"body": {"attachments": [{"type": "file", "filename": "memo.ogg", "payload": {}}]}}
        self.assertEqual(audio_attachment(message)["filename"], "memo.ogg")
        with self.assertRaisesRegex(ValueError, "MP3"):
            infer_format({"filename": "memo.mp3"}, "audio/mpeg")
        self.assertEqual(infer_format({"filename": "memo.ogg"}, "application/octet-stream"), "ogg")


class MaxStorageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.storage = MaxStorage(Path(self.tmp.name) / "max.sqlite3")
        await self.storage.open()

    async def asyncTearDown(self):
        await self.storage.close()
        self.tmp.cleanup()

    async def test_group_denied_until_approval_and_once_is_atomic(self):
        s = self.storage
        self.assertFalse(await s.reserve(101, "m1", "whisper", False))
        self.assertTrue(await s.request(101, 7))
        self.assertFalse(await s.request(101, 7))
        self.assertTrue(await s.decide(101, "once"))
        self.assertFalse(await s.set_auto(101, True))
        results = await asyncio.gather(
            s.reserve(101, "m1", "whisper", False),
            s.reserve(101, "m2", "whisper", False),
        )
        self.assertEqual(results.count(True), 1)
        self.assertEqual((await s.group(101))["state"], "revoked")
        self.assertFalse(await s.reserve(101, "m3", "whisper", False))
        self.assertTrue(await s.request(101, 7))
        self.assertTrue(await s.decide(101, "always"))
        self.assertTrue(await s.reserve(101, "m3", "whisper", False))

    async def test_webhook_replay_not_enqueued_twice_and_running_job_not_retried(self):
        s = self.storage
        self.assertTrue(await s.enqueue("message:m1", {"update_type": "message_created"}))
        self.assertFalse(await s.enqueue("message:m1", {"update_type": "message_created"}))
        self.assertEqual((await s.next_event())[0], "message:m1")
        self.assertTrue(await s.reserve(1, "m1", "whisper", True))
        await s.close()
        await s.open()
        self.assertIsNone(await s.next_event())
        self.assertFalse(await s.reserve(1, "m1", "whisper", True))

    async def test_revoke_disables_auto_and_paid_job(self):
        s = self.storage
        await s.request(101, 7)
        await s.decide(101, "always")
        self.assertTrue(await s.set_auto(101, True))
        await s.revoke(101)
        self.assertFalse((await s.group(101))["auto_enabled"])
        self.assertFalse(await s.reserve(101, "m1", "whisper", False))

    async def test_admin_reply_and_group_auto_route_without_provider_call(self):
        config = MaxConfig(frozenset({7}), {"w": "provider/w"}, "w", "https://routerai.ru/api/v1",
                           "ru", 1024, 60, frozenset({"vu.okcdn.ru"}), True)
        with patch.dict(os.environ, {"ALL_PROXY": "", "all_proxy": "", "HTTPS_PROXY": "", "https_proxy": ""}):
            bot = MaxBot(config, self.storage, "placeholder", "placeholder")
        sent, calls = [], []

        async def send(text, **kwargs):
            sent.append((text, kwargs))

        async def transcribe(message, target, model, private):
            calls.append((target["body"]["mid"], model, private))

        bot.send, bot.transcribe = send, transcribe
        private_reply = {
            "update_type": "message_created",
            "message": {"sender": {"user_id": 7},
                        "recipient": {"user_id": 7, "chat_id": None, "chat_type": "dialog"},
                        "body": {"mid": "command", "text": "/tr", "attachments": []},
                        "link": {"type": "reply", "message": {"mid": "audio", "attachments": [
                            {"type": "audio", "payload": {"url": "https://vu.okcdn.ru/a"}}]}}}
        }
        await bot.handle(private_reply)
        self.assertEqual(calls, [("audio", "w", True)])
        self.assertEqual(bot.dialogs[7], 7)

        group_event = {"update_type": "message_created", "message": {
            "sender": {"user_id": 9}, "recipient": {"chat_id": 101, "chat_type": "chat"},
            "body": {"mid": "new-audio", "text": "", "attachments": [{"type": "audio"}]}}}
        await bot.handle(group_event)
        self.assertEqual(len(calls), 1)
        await self.storage.request(101, 9)
        await self.storage.decide(101, "always")
        await self.storage.set_auto(101, True)
        await bot.handle(group_event)
        self.assertEqual(calls[-1], ("new-audio", "w", False))
        await bot.close()

    async def test_webhook_rejects_missing_or_wrong_secret(self):
        path = Path(self.tmp.name) / "config.yaml"
        path.write_text("max:\n  admin_user_ids: [7]\n  download_hosts: [vu.okcdn.ru]\n"
                        "stt:\n  default_model: w\n  models:\n    w:\n"
                        "      provider_model: example/model\n", encoding="utf-8")
        env = {"MAX_BOT_TOKEN": "placeholder", "MAX_WEBHOOK_SECRET": "secret-token",
               "ROUTERAI_API_KEY": "placeholder", "MAX_CONFIG_PATH": str(path),
               "MAX_DB_PATH": str(Path(self.tmp.name) / "webhook.sqlite3"),
               "ALL_PROXY": "", "all_proxy": "", "HTTPS_PROXY": "", "https_proxy": ""}
        with patch.dict(os.environ, env):
            app = create_app()
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
                body = {"update_type": "unknown", "timestamp": 1}
                missing = await client.post("/max/webhook", json=body)
                wrong = await client.post("/max/webhook", json=body, headers={"X-Max-Bot-Api-Secret": "wrong"})
                accepted = await client.post("/max/webhook", json=body, headers={"X-Max-Bot-Api-Secret": "secret-token"})
                self.assertEqual((missing.status_code, wrong.status_code, accepted.status_code), (403, 403, 200))


if __name__ == "__main__":
    unittest.main()
