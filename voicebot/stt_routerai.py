from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
import base64
import math
import json
from decimal import Decimal

import httpx
from .provider_costs import decode_response, usage_cost, generation_id, generation_cost

MAX_STT_REQUEST_BYTES=32*1024*1024


@dataclass
class TranscriptionResult:
    text: str
    raw: dict[str, Any]
    cost: Decimal | None = None
    duration_seconds: float | None = None
    generation_id: str | None = None


class ProviderCallError(RuntimeError):
    def __init__(self, message, *, sent, identity=None):
        super().__init__(message)
        self.sent, self.generation_id = sent, generation_id(identity)


class RouterAITranscriber:
    def __init__(self, api_key: str, base_url: str = "https://routerai.ru/api/v1", timeout: float = 120.0):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    async def transcribe(
        self,
        audio_path: str | Path,
        *,
        model: str,
        audio_format: str,
        language: str = "ru",
        temperature: float | None = None,
        before_send=None,
    ) -> TranscriptionResult:
        audio_path = Path(audio_path)
        try:
            encoded = base64.b64encode(audio_path.read_bytes()).decode("utf-8")
        except (OSError,ValueError):
            raise ProviderCallError('Audio preparation failed',sent=False) from None
        payload: dict[str, Any] = {
            "model": model,
            "input_audio": {
                "data": encoded,
                "format": audio_format,
            },
            "language": language,
        }
        if temperature is not None:
            payload["temperature"] = temperature
        try:
            body=json.dumps(payload,ensure_ascii=False,allow_nan=False,separators=(',',':')).encode('utf-8')
        except (ValueError,UnicodeError):
            raise ProviderCallError('Invalid RouterAI request',sent=False) from None
        if len(body)>MAX_STT_REQUEST_BYTES:
            raise ProviderCallError('RouterAI request body exceeds limit',sent=False)

        try:
            client_context = httpx.AsyncClient(timeout=self.timeout,follow_redirects=False)
        except (ValueError,RuntimeError,ImportError) as exc:
            raise ProviderCallError('RouterAI client setup failed',sent=False) from None
        async with client_context as client:
            if before_send is not None:
                try:
                    await before_send()
                except Exception:
                    raise ProviderCallError('Pre-send checks refused',sent=False) from None
            response = await client.post(
                f"{self.base_url}/audio/transcriptions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                content=body,
            )

        if response.status_code >= 400:
            raise ProviderCallError(f"RouterAI HTTP {response.status_code}",sent=True,identity=response.headers.get('X-Generation-Id'))

        try:
            data = decode_response(response)
        except (ValueError,UnicodeError):
            raise ProviderCallError('RouterAI returned invalid JSON',sent=True,identity=response.headers.get('X-Generation-Id')) from None
        if not isinstance(data, dict):
            raise RuntimeError('RouterAI returned an invalid response')
        text = self._extract_text(data)
        if not text:
            raise ProviderCallError('RouterAI returned no text',sent=True,identity=response.headers.get('X-Generation-Id'))

        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        cost = usage_cost(data)
        duration = self._as_float(
            (usage or {}).get("seconds")
            or (usage or {}).get("duration_seconds")
            or (usage or {}).get("duration")
            or data.get("duration_seconds")
            or data.get("duration")
        )
        return TranscriptionResult(text=text.strip(), raw=data, cost=cost, duration_seconds=duration,
                                   generation_id=generation_id(response.headers.get('X-Generation-Id')))

    async def generation_cost(self, identity, model):
        return await generation_cost(self.api_key,self.base_url,identity,model)

    @staticmethod
    def _extract_text(data: dict[str, Any]) -> str:
        if isinstance(data.get("text"), str):
            return data["text"]
        if isinstance(data.get("transcription"), str):
            return data["transcription"]
        # Compatibility fallback for OpenAI-like chat-shaped responses.
        choices = data.get("choices")
        if isinstance(choices, list) and choices:
            first = choices[0] or {}
            message = first.get("message") if isinstance(first, dict) else None
            if isinstance(message, dict):
                content = message.get("content")
                if isinstance(content, str):
                    return content
            if isinstance(first.get("text"), str):
                return first["text"]
        return ""

    @staticmethod
    def _as_float(value: Any) -> float | None:
        if value is None:
            return None
        try:
            result = float(value)
            return result if math.isfinite(result) and result >= 0 else None
        except (TypeError, ValueError):
            return None
