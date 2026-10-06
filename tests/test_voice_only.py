import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from voicebot.handlers import cmd_transcribe, media_auto_or_private, transcribe_message_media
from voicebot.media import extract_media


def message(*, voice=None, audio=None, document=None, chat_type="private", reply=None):
    return SimpleNamespace(
        voice=voice, audio=audio, document=document,
        chat=SimpleNamespace(type=chat_type, id=1),
        reply_to_message=reply, answer=AsyncMock(),
    )


class VoiceOnlyTests(unittest.IsolatedAsyncioTestCase):
    def test_telegram_voice_is_the_only_media_source(self):
        voice = SimpleNamespace(file_id="voice", file_unique_id="unique", duration=3,
                                file_size=100, mime_type="audio/ogg")
        self.assertEqual(extract_media(message(voice=voice)).kind, "voice")
        self.assertIsNone(extract_media(message(audio=SimpleNamespace(file_id="music"))))
        self.assertIsNone(extract_media(message(document=SimpleNamespace(
            file_name="speech.ogg", mime_type="audio/ogg"))))

    async def test_command_rejects_audio_reply_before_access_or_job(self):
        for target in (message(audio=object()), message(document=object()), None):
            with self.subTest(target=target):
                request = message(reply=target)
                with patch("voicebot.handlers.check_access_for_transcription", new_callable=AsyncMock) as access, \
                     patch("voicebot.handlers.transcribe_message_media", new_callable=AsyncMock) as transcribe:
                    await cmd_transcribe(request, None, object(), object())
                request.answer.assert_awaited_once()
                access.assert_not_awaited()
                transcribe.assert_not_awaited()

    async def test_private_and_group_auto_skip_music_without_access_or_job(self):
        for chat_type in ("private", "group"):
            for payload in ({"audio": object()}, {"document": object()}):
                with self.subTest(chat_type=chat_type, payload=payload):
                    request = message(chat_type=chat_type, **payload)
                    with patch("voicebot.handlers.check_access_for_transcription", new_callable=AsyncMock) as access, \
                         patch("voicebot.handlers.transcribe_message_media", new_callable=AsyncMock) as transcribe:
                        await media_auto_or_private(request, object(), object())
                    access.assert_not_awaited()
                    transcribe.assert_not_awaited()
                    request.answer.assert_not_awaited()

    async def test_direct_transcription_path_refuses_non_voice(self):
        request = message()
        for target in (message(audio=object()), message(document=object())):
            with self.subTest(target=target):
                request.answer.reset_mock()
                ctx = SimpleNamespace(db=AsyncMock(), transcriber=AsyncMock())
                await transcribe_message_media(request, target, ctx, object(), "test")
                request.answer.assert_awaited_once()
                ctx.db.create_job.assert_not_awaited()
                ctx.transcriber.transcribe.assert_not_awaited()

    async def test_voice_still_reaches_transcription(self):
        request = message(voice=object())
        with patch("voicebot.handlers.check_access_for_transcription", new_callable=AsyncMock, return_value=True), \
             patch("voicebot.handlers.transcribe_message_media", new_callable=AsyncMock) as transcribe:
            await media_auto_or_private(request, SimpleNamespace(config=SimpleNamespace(default_model="test")), object())
        transcribe.assert_awaited_once()
