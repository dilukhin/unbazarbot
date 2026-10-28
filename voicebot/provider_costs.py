"""Bounded, exact RouterAI metadata; no inference or account balance requests."""
import asyncio
import json
import re
from dataclasses import dataclass
from decimal import Decimal

import httpx

from .money import MoneyError, decimal_amount


def exact_cost(value):
    try:
        return decimal_amount(value)
    except (MoneyError, TypeError):
        return None


def decode_response(response):
    if len(response.content) > 2*1024*1024:
        raise ValueError('Response oversized')
    def unique(pairs):
        result = {}
        for key,value in pairs:
            if key in result:
                raise ValueError('Duplicate response key')
            result[key] = value
        return result
    def invalid(value):
        raise ValueError('Nonfinite response number')
    value = json.loads(response.content, parse_float=Decimal, parse_constant=invalid, object_pairs_hook=unique)
    if not isinstance(value,dict):
        raise ValueError('Invalid response shape')
    return value


def usage_cost(data):
    usage = data.get('usage') if isinstance(data.get('usage'),dict) else {}
    values = [exact_cost(v) for v in (usage.get('cost'),usage.get('total_cost'),data.get('cost')) if v is not None]
    if not values or any(v is None or v != values[0] for v in values):
        return None
    return values[0]


def generation_id(value):
    return value if isinstance(value,str) and re.fullmatch(r'[A-Za-z0-9_:-]{1,200}',value) else None


@dataclass(frozen=True)
class GenerationCost:
    status: str
    cost: Decimal | None = None


async def generation_cost(api_key, base_url, identity, model):
    if generation_id(identity) is None:
        return GenerationCost('unavailable')
    try:
        async with httpx.AsyncClient(timeout=15,follow_redirects=False) as client:
            for attempt in range(2):
                response = await client.get(base_url.rstrip('/')+'/generation',params={'id':identity},
                                            headers={'Authorization':'Bearer '+api_key})
                if response.status_code == 200:
                    data=decode_response(response).get('data')
                    if not isinstance(data,dict) or data.get('id')!=identity or data.get('model')!=model:
                        return GenerationCost('mismatch')
                    cost=exact_cost(data.get('total_cost'))
                    return GenerationCost('verified',cost) if cost is not None else GenerationCost('unavailable')
                if response.status_code not in {404,429,500,502,503,504}:
                    break
                if attempt==0:
                    await asyncio.sleep(0.2)
    except (httpx.HTTPError,OSError,ValueError,RuntimeError,ImportError):
        pass
    return GenerationCost('unavailable')
