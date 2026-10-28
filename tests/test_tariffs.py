from __future__ import annotations

import asyncio
import copy
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from voicebot.tariffs import (MAX_BYTES, URL, PriceCatalog, Quantity, TariffError,
                              TariffSettings, content_hash, parse_document)
from tests.tariff_fixtures import GOLDEN, NOW, catalog, document


class ContractTests(unittest.TestCase):
    def test_virtual_model_is_preserved_as_unpriced(self):
        value = document()
        model = copy.deepcopy(value['content']['models']['synthetic/asr'])
        model.update(id='~synthetic/asr-latest', status='unpriced',
                     reason='endpoints-not-checked', api=None, endpoints=[], price_ceilings={})
        value['content']['models'][model['id']] = model
        value['snapshot_id'] = content_hash(value['content'])
        service = PriceCatalog('unused', clock=lambda: NOW)
        service.snapshot = parse_document(json.dumps(value).encode(), now=NOW)
        self.assertFalse(service.estimate_audio(model['id'], 60).available)
        self.assertTrue(service.estimate_audio('synthetic/asr', 60).available)

    def test_shared_provider_golden_contract_and_exact_fraction(self):
        value = json.loads(GOLDEN.read_bytes())
        service = PriceCatalog('unused', clock=lambda: NOW)
        service.snapshot = parse_document(GOLDEN.read_bytes(), now=NOW)
        estimate = service.estimate_audio('synthetic/asr', 60)
        self.assertTrue(estimate.available)
        self.assertEqual(estimate.max_rub, Decimal('0.6'))
        self.assertEqual(estimate.bazariki, Decimal('60'))
        self.assertEqual(estimate.observed_rub, Decimal('0.00740740734074074073407407407340'))
        self.assertEqual(estimate.snapshot_id, value['snapshot_id'])

    def test_token_request_and_second_units_are_not_interchangeable(self):
        for component, unit, count in (('prompt','token',100), ('seconds','second',60), ('invoke','request',1)):
            value = document()
            model = value['content']['models']['synthetic/asr']
            model['price_ceilings'] = {component: {'unit': unit, 'rub':'0.00123'}}
            ep = model['endpoints'][0]
            ep['pricing'], ep['pricing_units'] = {component:'0.0000123'}, {component:unit}
            value['snapshot_id'] = content_hash(value['content'])
            service = PriceCatalog('unused', clock=lambda:NOW)
            service.snapshot = parse_document(json.dumps(value).encode(), now=NOW)
            estimate = service.estimate('synthetic/asr', {component:Quantity(Decimal(count),unit)},api='audio/transcriptions')
            self.assertEqual(estimate.bazariki, Decimal(count)*Decimal('0.123'))
            self.assertFalse(service.estimate('synthetic/asr', {component:Quantity(Decimal(count),'image')},api='audio/transcriptions').available)
            if unit == 'token': self.assertFalse(service.estimate_audio('synthetic/asr',60).available)

    def test_all_positive_components_and_all_fallbacks_are_bounded(self):
        value = document()
        model=value['content']['models']['synthetic/asr']
        ep=copy.deepcopy(model['endpoints'][0]);ep['provider']='expensive';ep['pricing']['seconds']='0.0009'
        model['endpoints'].append(ep)
        model['price_ceilings']['operation']={'unit':'request','rub':'0.003'}
        value['snapshot_id']=content_hash(value['content'])
        service=PriceCatalog('unused',clock=lambda:NOW);service.snapshot=parse_document(json.dumps(value).encode(),now=NOW)
        estimate=service.estimate_audio('synthetic/asr',60)
        self.assertEqual(estimate.max_rub,Decimal('0.063'))
        self.assertEqual(estimate.observed_rub,Decimal('0.054'))
        self.assertEqual(service.estimate('synthetic/asr',{'seconds':Quantity(Decimal(60),'second')},api='audio/transcriptions').status,'unsafe')

    def test_publisher_unpriced_does_not_disable_a_valid_other_model(self):
        value=document(('synthetic/asr','synthetic/missing'))
        value['content']['models']['synthetic/missing']['status']='unpriced'
        value['content']['models']['synthetic/missing']['reason']='api-not-confirmed'
        value['snapshot_id']=content_hash(value['content'])
        service=PriceCatalog('unused',clock=lambda:NOW);service.snapshot=parse_document(json.dumps(value).encode(),now=NOW)
        self.assertTrue(service.estimate_audio('synthetic/asr',60).available)
        self.assertEqual(service.estimate_audio('synthetic/missing',60).status,'unpriced')
        self.assertFalse(service.estimate_audio('synthetic/unknown',60).available)

    def test_candidate_foreign_hash_currency_validation_future_and_float_rejected(self):
        for mutation in ('candidate','owner','currency','hash','validation','future','float','bool-schema'):
            value=document()
            if mutation=='candidate':value['state']='candidate'
            elif mutation=='owner':value['owner']='foreign'
            elif mutation=='currency':value['content']['currency']='USD'
            elif mutation=='hash':value['snapshot_id']='sha256:broken'
            elif mutation=='validation':value['validation']['status']='pending'
            elif mutation=='future':value['source_checked_at']='2099-01-01T00:00:00Z'
            elif mutation=='bool-schema':value['schema']=True
            else:value['content']['models']['synthetic/asr']['endpoints'][0]['pricing']['seconds']=0.001
            with self.subTest(mutation=mutation), self.assertRaises(TariffError):parse_document(json.dumps(value).encode(),now=NOW)
        for raw in (b'',b'{"schema":1,"schema":1}',b'x'*(MAX_BYTES+1)):
            with self.assertRaises(TariffError):parse_document(raw,now=NOW)

    def test_unsafe_variable_or_incomplete_route_never_authorizes(self):
        for mutation in ('variable','units','api','ceiling'):
            value=document();ep=value['content']['models']['synthetic/asr']['endpoints'][0]
            if mutation=='variable':ep['variable_pricing']=True
            elif mutation=='units':ep['pricing_units']={}
            elif mutation=='api':ep['supported_apis']=[]
            else:ep['pricing']['seconds']='100'
            value['snapshot_id']=content_hash(value['content'])
            service=PriceCatalog('unused',clock=lambda:NOW);service.snapshot=parse_document(json.dumps(value).encode(),now=NOW)
            self.assertFalse(service.estimate_audio('synthetic/asr',60).available)

    def test_decimal_quantity_rejects_float_and_wrong_api(self):
        for amount in (0.1,True,Decimal('NaN'),Decimal('Infinity'),Decimal('-1'),Decimal('1e-1000')):
            with self.assertRaises(TariffError):Quantity(amount,'second')
        service=catalog('unused')
        self.assertFalse(service.estimate_audio('synthetic/asr',0.1).available)
        self.assertFalse(service.estimate('synthetic/asr',{},api='chat').available)
        with self.assertRaises(ValueError):TariffSettings(refresh_seconds=0)


class RefreshTests(unittest.IsolatedAsyncioTestCase):
    async def test_client_setup_failure_preserves_cache_and_next_refresh_recovers(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = catalog(Path(tmp)/'prices.json')
            before = service.snapshot.snapshot_id
            with patch('voicebot.tariffs.httpx.AsyncClient', side_effect=ImportError('synthetic missing proxy dependency')):
                self.assertFalse(await service.refresh())
            self.assertEqual(service.snapshot.snapshot_id, before)
            self.assertEqual(service.last_error, 'refresh-failed')
            service.transport = httpx.MockTransport(lambda request: httpx.Response(200, content=json.dumps(document()).encode()))
            self.assertTrue(await service.refresh())
            self.assertIsNone(service.last_error)

    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'runtime'/'prices.json'

    async def asyncTearDown(self):
        self.tmp.cleanup()

    def client(self,handler,clock=lambda:NOW):
        return PriceCatalog(self.path,clock=clock,transport=httpx.MockTransport(handler))

    async def test_refresh_restart_network_and_bad_update_preserve_last_good(self):
        calls=[]
        async def handler(request):
            calls.append(request)
            return httpx.Response(200,json=document(),headers={'ETag':'"test"'})
        service=self.client(handler)
        self.assertTrue(await service.refresh());original=self.path.read_bytes()
        restored=self.client(lambda request:httpx.Response(500))
        self.assertTrue(restored.load());self.assertTrue(restored.estimate_audio('synthetic/asr',60).available)
        self.assertFalse(await restored.refresh());self.assertEqual(self.path.read_bytes(),original)
        bad=self.client(lambda request:httpx.Response(200,json={'state':'candidate'}))
        bad.load();self.assertFalse(await bad.refresh());self.assertEqual(self.path.read_bytes(),original)
        self.assertEqual(str(calls[0].url),URL)
        self.assertNotIn('authorization',calls[0].headers)

    async def test_304_is_not_a_freshness_heartbeat(self):
        clock=[NOW]
        service=self.client(lambda request:httpx.Response(200,json=document(),headers={'ETag':'"v1"'}),clock=lambda:clock[0])
        self.assertTrue(await service.refresh());raw=self.path.read_bytes()
        clock[0]+=timedelta(days=3)
        seen=[]
        def unchanged(request):
            seen.append(request.headers.get('If-None-Match'));return httpx.Response(304)
        service.transport=httpx.MockTransport(unchanged)
        self.assertTrue(await service.refresh());self.assertEqual(service.status(),'stale')
        self.assertFalse(service.estimate_audio('synthetic/asr',60).available)
        self.assertEqual(self.path.read_bytes(),raw);self.assertEqual(seen,['"v1"'])

    async def test_replayed_and_older_publications_rejected(self):
        service=self.client(lambda request:httpx.Response(200,json=document()))
        await service.refresh();raw=self.path.read_bytes()
        for mutation in ('different-content','older'):
            value=document()
            if mutation=='older':value['source_checked_at']='2026-10-27T12:00:00Z'
            else:
                value['content']['models']['synthetic/asr']['price_ceilings']['seconds']['rub']='0.002'
                value['snapshot_id']=content_hash(value['content'])
            service.transport=httpx.MockTransport(lambda request:httpx.Response(200,json=value))
            self.assertFalse(await service.refresh());self.assertEqual(self.path.read_bytes(),raw)

    async def test_atomic_failure_and_concurrent_refresh_do_not_corrupt_cache(self):
        entered=asyncio.Event();release=asyncio.Event();calls=[]
        async def handler(request):
            calls.append(1);entered.set();await release.wait();return httpx.Response(200,json=document())
        service=self.client(handler);first=asyncio.create_task(service.refresh());await entered.wait()
        self.assertFalse(await service.refresh());release.set();self.assertTrue(await first);self.assertEqual(len(calls),1)
        raw=self.path.read_bytes()
        service.transport=httpx.MockTransport(lambda request:httpx.Response(200,json=document()))
        with patch('voicebot.tariffs.os.replace',side_effect=OSError('synthetic disk failure')):
            self.assertFalse(await service.refresh())
        self.assertEqual(self.path.read_bytes(),raw)
        self.assertEqual(list(self.path.parent.glob('.routerai-prices-*')),[])

    async def test_missing_invalid_cache_can_recover_from_verified_remote(self):
        self.path.parent.mkdir();self.path.write_bytes(b'corrupt')
        service=self.client(lambda request:httpx.Response(200,json=document()))
        self.assertFalse(service.load());self.assertTrue(await service.refresh())
        self.assertTrue(service.estimate_audio('synthetic/asr',60).available)

    async def test_pause_marker_stops_paid_estimate_without_restart(self):
        service=self.client(lambda request:httpx.Response(200,json=document()))
        await service.refresh();self.assertTrue(service.estimate_audio('synthetic/asr',60).available)
        service.pause_path.touch();self.assertEqual(service.status(),'unsafe')
        self.assertEqual(service.estimate_audio('synthetic/asr',60).reason,'paid-requests-paused')
        service.pause_path.unlink();self.assertTrue(service.estimate_audio('synthetic/asr',60).available)

    async def test_two_instances_cannot_overwrite_newer_disk_observation(self):
        fresh=document();fresh['source_checked_at']='2026-10-28T12:01:00Z';fresh['published_at']='2026-10-28T12:02:00Z'
        first=self.client(lambda request:httpx.Response(200,json=fresh));await first.refresh();raw=self.path.read_bytes()
        second=self.client(lambda request:httpx.Response(200,json=document()))
        self.assertFalse(await second.refresh());self.assertEqual(self.path.read_bytes(),raw)

    async def test_redirect_malformed_oversized_and_timeout_never_replace(self):
        good=self.client(lambda request:httpx.Response(200,json=document()));await good.refresh();raw=self.path.read_bytes()
        responses=[httpx.Response(302,headers={'Location':'https://example.invalid'}),httpx.Response(200,content=b'not-json'),httpx.Response(200,content=b'x'*(MAX_BYTES+1))]
        for response in responses:
            good.transport=httpx.MockTransport(lambda request:response)
            self.assertFalse(await good.refresh());self.assertEqual(self.path.read_bytes(),raw)
        def timeout(request):raise httpx.ReadTimeout('synthetic timeout')
        good.transport=httpx.MockTransport(timeout);self.assertFalse(await good.refresh());self.assertEqual(self.path.read_bytes(),raw)


if __name__=='__main__':unittest.main()
