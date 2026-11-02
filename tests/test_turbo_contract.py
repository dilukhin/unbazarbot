import copy
from datetime import datetime,timezone
from decimal import Decimal,localcontext
import json
from pathlib import Path
import tempfile
import unittest
from voicebot.tariffs import PriceCatalog,Quantity,TariffError,content_hash,parse_document

NOW=datetime(2026,11,2,10,2,tzinfo=timezone.utc)
MODEL='openai/whisper-large-v3-turbo'
FIXTURE=Path(__file__).parent/'fixtures/routerai_turbo_contract.json'


def document():return json.loads(FIXTURE.read_text())


def service(value=None):
    s=PriceCatalog('unused',clock=lambda:NOW)
    s.snapshot=parse_document(json.dumps(value or document()).encode(),now=NOW)
    return s


class TurboContractTests(unittest.TestCase):
    def test_shared_published_contract_is_usable_with_both_routes(self):
        s=service();q=s.estimate_audio(MODEL,Decimal('1.25'))
        self.assertTrue(q.available);self.assertEqual(q.max_rub,Decimal('0.078'))
        self.assertEqual(q.bazariki,Decimal('7.8'));self.assertEqual(q.observed_rub,Decimal('0.06'))
        self.assertEqual(len(s.snapshot.models[MODEL].endpoints),2)

    def test_floor_and_ceiling_apply_even_to_generic_quote_without_float_context(self):
        with localcontext() as ctx:
            ctx.prec=3
            s=service()
            for seconds,expected in (('0','0.078'),('59.999','0.078'),('60','0.078'),('60.01','0.0793'),('121.01','0.1586')):
                with self.subTest(seconds=seconds):
                    q=s.estimate_audio(MODEL,Decimal(seconds));self.assertEqual(q.max_rub,Decimal(expected))
                    generic=s.estimate(MODEL,{'seconds':Quantity(Decimal(seconds),'second')},api='audio/transcriptions')
                    self.assertEqual(generic.max_rub,q.max_rub)

    def test_rehashed_contract_mutations_are_rejected(self):
        for field in ('reservation','sources','ceiling','profile','openapi_sha256','routes'):
            v=document();v['content']['models'][MODEL]['billing_contract'][field]='forged'
            v['snapshot_id']=content_hash(v['content'])
            with self.subTest(field=field),self.assertRaises(TariffError):service(v)

    def test_wrong_route_unit_api_component_price_and_review_never_authorize(self):
        for mutation in ('route','unit','api','component','price','review','id'):
            v=document();m=v['content']['models'][MODEL];ep=m['endpoints'][0]
            if mutation=='route':ep['provider']='unknown'
            elif mutation=='unit':ep['pricing_units']['seconds']='token'
            elif mutation=='api':ep['supported_apis']=['chat']
            elif mutation=='component':ep['pricing']['extra']='0'
            elif mutation=='price':ep['pricing']['seconds']='0.1'
            elif mutation=='review':m['billing_review']={'financial_authorization':True}
            else:m['id']='synthetic/other'
            v['snapshot_id']=content_hash(v['content'])
            with self.subTest(mutation=mutation):
                try:s=service(v)
                except TariffError:continue
                self.assertFalse(s.estimate_audio(MODEL,60).available)

    def test_old_cached_document_is_not_made_fresh_by_new_contract(self):
        from datetime import timedelta
        s=service();s.clock=lambda:NOW+timedelta(days=3)
        self.assertEqual(s.estimate_audio(MODEL,60).status,'stale')
        with tempfile.TemporaryDirectory() as t:
            s=PriceCatalog(Path(t)/'cache',clock=lambda:NOW)
            raw=json.dumps(document()).encode();s._accept(raw)
            broken=document();broken['content']['models'][MODEL]['billing_contract']['reservation']['floor_seconds']='0'
            broken['snapshot_id']=content_hash(broken['content'])
            with self.assertRaises(TariffError):s._accept(json.dumps(broken).encode())
            self.assertEqual(s.path.read_bytes(),raw)
