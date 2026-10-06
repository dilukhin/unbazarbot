from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from voicebot.stt_routerai import RouterAITranscriber


class RouterAITests(unittest.IsolatedAsyncioTestCase):
    async def call(self,payload,status=200):
        original=httpx.AsyncClient
        captured=[]
        def handle(request):
            captured.append(request)
            return httpx.Response(status,json=payload)
        def client(**kwargs):
            return original(transport=httpx.MockTransport(handle),**kwargs)
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'voice.ogg';path.write_bytes(b'test')
            with patch('voicebot.stt_routerai.httpx.AsyncClient',side_effect=client):
                result=await RouterAITranscriber('TEST_ONLY').transcribe(path,model='m',audio_format='ogg')
        self.assertEqual(len(captured),1)
        self.assertTrue(captured[0].url.path.endswith('/audio/transcriptions'))
        return result

    async def test_success_zero_cost_and_duration(self):
        result=await self.call({'text':' Текст. ','usage':{'cost':0,'duration_seconds':3}})
        self.assertEqual((result.text,result.cost,result.duration_seconds),('Текст.',0,3))

    async def test_alternate_response_and_missing_usage(self):
        result=await self.call({'choices':[{'message':{'content':'Текст'}}],'usage':'unknown'})
        self.assertEqual(result.text,'Текст')
        self.assertIsNone(result.cost)

    async def test_http_error_does_not_expose_payload_or_retry(self):
        with self.assertRaisesRegex(RuntimeError,'HTTP 429') as caught:
            await self.call({'private':'SECRET'},429)
        self.assertNotIn('SECRET',str(caught.exception))

    async def test_empty_and_invalid_responses_are_rejected(self):
        for payload in ({'private':'SECRET'},['SECRET']):
            with self.subTest(payload=payload),self.assertRaises(RuntimeError) as caught:
                await self.call(payload)
            self.assertNotIn('SECRET',str(caught.exception))

    def test_nonfinite_and_negative_usage_is_not_counted(self):
        for value in ('nan','inf',-1,'invalid'):
            self.assertIsNone(RouterAITranscriber._as_float(value))
