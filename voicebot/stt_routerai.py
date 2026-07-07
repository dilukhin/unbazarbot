from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
import base64

import httpx


@dataclass
class TranscriptionResult:
    text: str
    raw: dict[str, Any]
    cost: float | None = None
    duration_seconds: float | None = None


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
    ) -> TranscriptionResult:
        audio_path = Path(audio_path)
        encoded = base64.b64encode(audio_path.read_bytes()).decode("utf-8")
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

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                f"{self.base_url}/audio/transcriptions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )

        if response.status_code >= 400:
            raise RuntimeError(f"RouterAI HTTP {response.status_code}: {response.text[:1000]}")

        data = response.json()
        text = self._extract_text(data)
        if not text:
            raise RuntimeError(f"RouterAI returned no text: {data!r}")

        usage = data.get("usage") if isinstance(data, dict) else None
        cost = self._as_float((usage or {}).get("cost") or (usage or {}).get("total_cost") or data.get("cost"))
        duration = self._as_float(
            (usage or {}).get("duration_seconds")
            or (usage or {}).get("duration")
            or data.get("duration_seconds")
            or data.get("duration")
        )
        return TranscriptionResult(text=text.strip(), raw=data, cost=cost, duration_seconds=duration)

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
            return float(value)
        except (TypeError, ValueError):
            return None
