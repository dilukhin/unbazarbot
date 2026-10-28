"""Synthetic published prices. No RouterAI or Telegram requests."""
import copy
import json
from datetime import datetime, timezone
from pathlib import Path

from voicebot.tariffs import PriceCatalog, content_hash, parse_document
from voicebot.audio_validation import PreparedAudio
from decimal import Decimal

NOW = datetime(2026, 10, 28, 12, 2, tzinfo=timezone.utc)


async def prepare_synthetic_audio(path, **kwargs):
    return PreparedAudio(Path(path),Decimal('3'),kwargs.get('audio_format','ogg'))
GOLDEN = Path(__file__).parent / 'fixtures/routerai_prices_published.json'


def document(model_ids=('synthetic/asr',)):
    value = json.loads(GOLDEN.read_bytes())
    base = value['content']['models']['synthetic/asr']
    value['content']['models'] = {}
    for model_id in model_ids:
        model = copy.deepcopy(base)
        model['id'] = model_id
        model['price_ceilings']['seconds']['rub'] = '0.001'
        for ep in model['endpoints']:
            ep['source_url'] = f'https://routerai.ru/api/v1/models/{model_id}/endpoints'
        value['content']['models'][model_id] = model
    value['snapshot_id'] = content_hash(value['content'])
    return value


def catalog(path, model_ids=('synthetic/asr',)):
    service = PriceCatalog(path, clock=lambda: NOW)
    service.snapshot = parse_document(json.dumps(document(model_ids)).encode(), now=NOW)
    return service
