"""Offline media preflight: synthetic silence through the production limits."""
import asyncio
import json
from pathlib import Path
import sys
import tempfile
import wave

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from voicebot.audio_validation import AudioError, prepare_audio


async def check_media():
    formats = []
    with tempfile.TemporaryDirectory(prefix='unbazar_media_check_') as tmp:
        source = Path(tmp) / 'synthetic.wav'
        with wave.open(str(source), 'wb') as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(16000)
            output.writeframes(b'\0\0' * 16000)
        source.chmod(0o600)
        for fmt in ('ogg', 'mp3', 'wav', 'flac'):
            prepared = await prepare_audio(source, max_seconds=2, max_bytes=1024*1024, audio_format=fmt)
            try:
                if not 0 < prepared.duration < 2:
                    raise AudioError('Некорректная длительность самопроверки')
                formats.append(fmt)
            finally:
                prepared.close()
    return {'media': 'ok', 'formats': formats, 'network': False}


def main():
    try:
        print(json.dumps(asyncio.run(check_media())))
    except (AudioError, OSError, asyncio.TimeoutError) as exc:
        print(json.dumps({'media': 'unavailable', 'reason': getattr(exc, 'code', type(exc).__name__)}))
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
