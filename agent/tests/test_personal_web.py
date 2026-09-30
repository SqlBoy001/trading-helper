"""Synthetic-only tests for the single-user screenshot Web surface."""
import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from src.personal_web.__main__ import PersonalServer, trade_draft


class PersonalWebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.server = PersonalServer(('127.0.0.1', 0), Path(self.temp.name))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)
        self.cookie = ''

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def call(self, path, data=None, headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        body = data if isinstance(data, bytes) else json.dumps(data) if data is not None else None
        hdr = {'Cookie': self.cookie, 'Content-Type': 'application/json'}
        hdr.update(headers or {})
        conn.request('POST' if data is not None else 'GET', path, body, hdr)
        response = conn.getresponse()
        cookie = response.getheader('Set-Cookie')
        if cookie:
            self.cookie = cookie.split(';')[0]
        raw = response.read()
        conn.close()
        return response.status, json.loads(raw) if 'application/json' in response.getheader('Content-Type','') else raw

    def login(self):
        self.assertEqual(self.call('/api/login', {'key':self.server.key})[0], 200)

    def trade(self, **overrides):
        value = dict(kind='execution', request_key='test-1', confirmed=True,
                     occurred_at='2026-01-01T08:00:00+00:00',
                     data=dict(symbol='SYNTHETIC', side='buy', quantity=100, price=10, currency='CNY'))
        value.update(overrides)
        return value

    def test_auth_origin_and_host(self):
        self.assertEqual(self.call('/api/history')[0],401)
        self.assertEqual(self.call('/api/login',{'key':'wrong'})[0],401)
        self.login()
        self.assertEqual(self.call('/api/history', headers={'Host':'evil.test'})[0],403)
        self.assertEqual(self.call('/api/events',self.trade(),{'Origin':'https://evil.test'})[0],403)
        self.call('/api/logout',{})
        self.assertEqual(self.call('/api/history')[0],401)

    def test_confirm_required_and_no_invented_intent(self):
        self.login()
        self.assertEqual(self.call('/api/events',self.trade(confirmed=False))[0],400)
        code, value=self.call('/api/events',self.trade())
        self.assertEqual(code,200)
        self.assertIsNone(value['event']['parent_id'])
        self.assertEqual(value['event']['intent_status'],'not_recorded')
        self.assertTrue(self.call('/api/events',self.trade())[1]['replayed'])
        events=self.call('/api/history')[1]['events']
        self.assertEqual(len(events),1)
        self.assertEqual(events[0]['kind'],'execution')

    def test_screenshot_duplicate_and_input_validation(self):
        self.login()
        self.assertEqual(self.call('/api/upload',b'not an image')[0],400)
        # Signature fixture tests storage/dedup, not image decoding quality.
        png=b'\x89PNG\r\n\x1a\nSYNTHETIC'
        code,image=self.call('/api/upload',png)
        self.assertEqual(code,200)
        self.assertTrue(self.call('/api/upload',png)[1]['duplicate'])
        first=self.call('/api/events',self.trade(image_id=image['image_id']))
        second=self.call('/api/events',self.trade(image_id=image['image_id'],request_key='new'))
        self.assertEqual(first[1]['event']['id'],second[1]['event']['id'])
        self.assertTrue(second[1]['replayed'])
        self.assertEqual(self.call('/api/events',self.trade(image_id='../bad'))[0],400)
        self.assertEqual(self.call('/api/events',self.trade(source='assistant'))[0],400)

    def test_snapshot_unknown_cash_and_correction(self):
        self.login()
        data=dict(positions=[dict(symbol='SYNTHETIC',quantity=100)],cash=None,complete=False,notes='局部截图')
        payload=dict(kind='snapshot',request_key='snapshot1',confirmed=True,data=data)
        code,snapshot=self.call('/api/events',payload)
        self.assertEqual(code,200)
        self.assertIsNone(snapshot['event']['data']['cash'])
        corrected=dict(data,positions=[dict(symbol='SYNTHETIC',quantity=80)])
        code,amendment=self.call('/api/events',dict(kind='correction',request_key='correct1',confirmed=True,parent_id=snapshot['event']['id'],data=dict(reason='录入错误',replacement=corrected)))
        self.assertEqual(code,200)
        original=self.server.journal.get(snapshot['event']['id'])
        self.assertEqual(original['event']['data']['positions'][0]['quantity'],100)
        self.assertEqual(original['corrections'][0]['data']['replacement']['positions'][0]['quantity'],80)
        self.assertIn(amendment['event']['id'],self.call('/api/report')[1]['text'])
        payload['request_key']='bad'
        payload['data']=dict(data,cash=-1)
        self.assertEqual(self.call('/api/events',payload)[0],400)

    def test_statement_activity_and_latest_overview(self):
        self.login()
        data = dict(activity="股份转入", currency="CNY", cash_change=0,
                    external_source="synthetic-statement", external_id="page1:row1")
        event = self.server.journal.append(journal_id="personal:gtht", kind="account_activity",
                   request_key="activity1", source="import", source_ref="synthetic", data=data)
        self.assertEqual(event["event"]["kind"], "account_activity")
        with self.assertRaises(ValueError):
            self.server.journal.append(journal_id="personal:gtht", kind="account_activity",
                   request_key="activity2", source="assistant", source_ref="synthetic", data=data)
        with self.assertRaises(ValueError):
            self.server.journal.append(journal_id="personal:gtht", kind="account_activity",
                   request_key="activity3", source="import", source_ref="synthetic", data=data)
        self.assertEqual(self.call('/api/overview')[1]['activity_count'], 1)
        self.assertEqual(self.call('/api/statement/'+'a'*64)[0],404)
        self.assertEqual(self.call('/api/statement/../../access-key.txt')[0],404)

    def test_broker_identity_duplicate_across_images(self):
        self.login()
        trade=self.trade()
        trade['data'].update(external_source='gtht:personal',external_id='synthetic-fill')
        self.assertEqual(self.call('/api/events',trade)[0],200)
        trade['request_key']='new'
        self.assertEqual(self.call('/api/events',trade)[0],400)


class OCRDraftTests(unittest.TestCase):
    def test_single_trade_and_ambiguous_rows(self):
        rows = [{"text": v} for v in ["模拟成交", "2026-09-22 14:30", "600519.SH", "买入", "100", "1500.00"]]
        draft = trade_draft(rows)
        self.assertEqual(draft["quantity"], 100)
        self.assertEqual(draft["price"], 1500)
        self.assertIsNone(trade_draft(rows + [{"text": "50"}]))
        self.assertIsNone(trade_draft(rows[1:]))
        self.assertIsNone(trade_draft(rows + [{"text": "000001.SZ"}]))


if __name__=='__main__':
    unittest.main()
