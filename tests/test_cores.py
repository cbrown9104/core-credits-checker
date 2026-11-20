"""Tests for the Unpaid Cores page: the unpaid tickets of a store's saved
master, the status and note typed on a ticket, and writing the date a
credit request was sent.

These need the scratch database of test_accounts (TEST_DATABASE_URL).
All data is made up.
"""
import datetime
import io
import json
import os
import re
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(
    __file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_accounts import AccountsCase, master_bytes, row  # noqa: E402


def ago(days):
    """A Shipment ID that many days before today (the app's today)."""
    from accounts import web
    return (web._today() - datetime.timedelta(days=days)).isoformat() + \
        ' 17:00.01'


def week():
    """A master with one ticket of each kind."""
    from accounts import web
    asked = (web._today() - datetime.timedelta(days=20)).isoformat()
    return [
        row('CC00900001', ago(200), 'C400000001', '11111111AA', 50),
        row(None, ago(70), 'C400000002', '22222222AB', 125, claim=123456),
        row(None, ago(10), 'C400000003', '33333333AC', 50),
        row(None, ago(90), 'C400000004', '44444444AD', 300, req=asked),
        row(None, None, 'C400000005', '55555555AE', 75),
    ]


class TestUnpaidCores(AccountsCase):
    def save_master(self, c, rows):
        r = self.post(c, '/master/replace', {
            'master': (io.BytesIO(master_bytes(rows)),
                       'Core_Returns_RECONCILED.xlsx')},
            content_type='multipart/form-data')
        self.assertEqual(r.status_code, 302)

    def call(self, c, path, **body):
        token, store = self.guard(c)
        return c.post(path, json=body, headers={
            'X-CSRF-Token': token, 'X-Store-Id': store})

    def note(self, c, **body):
        return self.call(c, '/api/cores/notes', **body)

    def ask(self, c, **body):
        return self.call(c, '/api/cores/requested', **body)

    def listed(self, c):
        """The tickets the page was given, by ticket."""
        page = c.get('/cores')
        self.assertEqual(page.status_code, 200)
        m = re.search(r'var ROWS = (\[.*?\]);\n',
                      page.get_data(as_text=True))
        self.assertTrue(m, 'the page has no ticket list')
        return {t['t']: t for t in json.loads(m.group(1))}

    def master_rows(self, store):
        from accounts import storage
        from recon import master as M
        _, content = storage.master_file(store['id'])
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'm.xlsx')
            with open(path, 'wb') as fh:
                fh.write(content)
            return {r['ticket']: r for r in M.read_master(path)[0]}

    def test_the_list_says_where_each_unpaid_ticket_stands(self):
        from accounts import web
        self.make_store('Core Motors', 'cora@core.test')
        cora = self.sign_in('cora@core.test')
        # nothing saved yet: the page says how to start, and has no list
        empty = cora.get('/cores')
        self.assertEqual(empty.status_code, 200)
        self.assertIn(b'No master is saved', empty.data)
        self.assertIn(b'href="/cores">Unpaid Cores', empty.data)
        self.assertEqual(cora.get('/cores/export').status_code, 404)
        self.assertEqual(cora.get('/api/cores/credited').get_json(),
                         {'master': None, 'rows': []})

        self.save_master(cora, week())
        got = self.listed(cora)
        # the credited ticket is not sent with the page
        self.assertEqual(sorted(got), ['C400000002', 'C400000003',
                                       'C400000004', 'C400000005'])
        self.assertEqual({t: got[t]['st'] for t in got}, {
            'C400000002': 'ready', 'C400000003': 'wait',
            'C400000004': 'asked', 'C400000005': 'nodate'})
        today = web._today()
        ready = got['C400000002']
        self.assertEqual((ready['age'], ready['a'], ready['c'], ready['p']),
                         (70, 125.0, 123456, '22222222AB'))
        # ready from the day it is 60 days past its shipment
        self.assertEqual(ready['due'], (today - datetime.timedelta(
            days=10)).isoformat())
        self.assertEqual(got['C400000003']['due'], (
            today + datetime.timedelta(days=50)).isoformat())
        self.assertEqual((got['C400000005']['age'],
                          got['C400000005']['due']), (None, ''))
        self.assertEqual(got['C400000004']['rq'], (
            today - datetime.timedelta(days=20)).isoformat())

        # the credited one comes when the page asks for it
        paid = cora.get('/api/cores/credited').get_json()['rows']
        self.assertEqual([(t['t'], t['st'], t['m']) for t in paid],
                         [('C400000001', 'paid', 'CC00900001')])

        # the store's own number of days decides what is ready
        self.assertEqual(self.post(cora, '/store/settings', {
            'threshold_days': '5'}).status_code, 302)
        self.assertEqual(self.listed(cora)['C400000003']['st'], 'ready')

        book = cora.get('/cores/export')
        self.assertEqual(book.status_code, 200)
        from openpyxl import load_workbook
        ws = load_workbook(io.BytesIO(book.data))['Unpaid Cores']
        rows = list(ws.iter_rows(values_only=True))
        self.assertEqual(rows[0][:3], ('Shipment ID', 'Control Ticket',
                                       'Core Part #'))
        self.assertEqual(len(rows), 5)
        by = {r[1]: r for r in rows[1:]}
        self.assertEqual(by['C400000002'][3:7],
                         (123456, 125, 70, 'Ready to request'))
        self.assertEqual(by['C400000005'][6], 'No ship date')

    def test_a_status_and_a_note_stay_on_the_ticket(self):
        from accounts import db
        store = self.make_store('Note Motors', 'nan@note.test')
        nan = self.sign_in('nan@note.test')
        self.save_master(nan, week())
        r = self.note(nan, tickets=['c400000002'], status='denied',
                      note='  Core Group:   not  received ')
        self.assertEqual(r.get_json(), {'updated': 1})
        got = self.listed(nan)['C400000002']
        self.assertEqual((got['s'], got['n']),
                         ('denied', 'Core Group: not received'))
        kept = db.one('SELECT * FROM core_notes WHERE store_id = %s',
                      (store['id'],))
        self.assertEqual((kept['ticket'], kept['changed_email']),
                         ('C400000002', 'nan@note.test'))
        # one of the two leaves the other as it was
        self.note(nan, tickets=['C400000002'], status='review')
        got = self.listed(nan)['C400000002']
        self.assertEqual((got['s'], got['n']),
                         ('review', 'Core Group: not received'))
        for bad in ({'tickets': ['C400000002'], 'status': 'paid'},
                    {'tickets': [], 'status': 'review'},
                    {'tickets': ['C400000002']},
                    {'tickets': [7], 'note': 'x'},
                    {'tickets': 'C400000002', 'note': 'x'}):
            self.assertEqual(self.note(nan, **bad).status_code, 400, bad)
        many = self.note(nan, tickets=['C4%08d' % i for i in range(2001)],
                         status='review')
        self.assertEqual(many.status_code, 400)
        self.assertIn('2,000 tickets or fewer', many.get_json()['error'])
        self.note(nan, tickets=['C400000003'], note='x' * 600)
        self.assertEqual(len(self.listed(nan)['C400000003']['n']), 500)
        # many at once, and written off is one of the choices
        r = self.note(nan, tickets=['C400000003', 'C400000004'],
                      status='closed')
        self.assertEqual(r.get_json(), {'updated': 2})
        got = self.listed(nan)
        self.assertEqual([got[t]['s'] for t in ('C400000003',
                                                'C400000004')],
                         ['closed', 'closed'])
        # a new version of the master keeps what was typed
        self.save_master(nan, week() + [row(None, ago(3), 'C400000006',
                                            '66666666AF', 10)])
        got = self.listed(nan)
        self.assertEqual(len(got), 5)
        self.assertEqual((got['C400000002']['s'], got['C400000002']['n']),
                         ('review', 'Core Group: not received'))
        # it is in the Excel file too
        from openpyxl import load_workbook
        ws = load_workbook(io.BytesIO(
            nan.get('/cores/export').data))['Unpaid Cores']
        by = {r[1]: r for r in ws.iter_rows(min_row=2, values_only=True)}
        self.assertEqual(by['C400000002'][9:11],
                         ('Review', 'Core Group: not received'))
        self.assertEqual(by['C400000004'][9], 'Written off')
        # nothing typed any more: no row is kept for the ticket
        self.note(nan, tickets=['C400000002'], status='', note='')
        self.assertFalse(db.one(
            "SELECT 1 AS x FROM core_notes WHERE store_id = %s AND "
            "ticket = 'C400000002'", (store['id'],)))

    def test_marking_tickets_requested_writes_the_date_in_the_master(self):
        from accounts import db, storage, web
        store = self.make_store('Ask Motors', 'ada@ask.test')
        ada = self.sign_in('ada@ask.test')
        self.save_master(ada, week())
        v1 = storage.current_master(store['id'])
        today = web._today()
        sent = (today - datetime.timedelta(days=2)).isoformat()

        # dates that cannot be right, and a page that names no tickets
        for bad in ((today + datetime.timedelta(days=1)).isoformat(),
                    (today - datetime.timedelta(days=500)).isoformat(),
                    'last Tuesday', 20261120):
            r = self.ask(ada, tickets=['C400000002'], date=bad,
                         master=v1['id'])
            self.assertEqual(r.status_code, 400, bad)
        self.assertEqual(self.ask(ada, tickets=[], date=sent,
                                  master=v1['id']).status_code, 400)
        self.assertEqual(storage.current_master(store['id'])['id'],
                         v1['id'])

        # a plain user marks tickets, as a plain user's run would. The
        # credited ticket and one that is not on the master are left out.
        self.post(ada, '/users/add', {'email': 'uma@ask.test',
                                      'role': 'user'})
        uma = self.sign_in('uma@ask.test')
        r = self.ask(uma, tickets=['C400000002', 'c400000003',
                                   'C400000001', 'C499999999'],
                     date=sent, master=v1['id'])
        self.assertEqual(r.get_json(), {'changed': 2})
        v2 = storage.current_master(store['id'])
        self.assertNotEqual(v2['id'], v1['id'])
        self.assertEqual((v2['source'], v2['row_count'], v2['unpaid_count'],
                          v2['created_email'], v2['filename']),
                         ('edit', 5, 4, 'uma@ask.test',
                          'Core_Returns_RECONCILED.xlsx'))
        rows = self.master_rows(store)
        self.assertEqual([rows[t]['requested'] for t in (
            'C400000001', 'C400000002', 'C400000003', 'C400000005')],
            [None, sent, sent, None])
        self.assertEqual((rows['C400000001']['memo'],
                          rows['C400000002']['amount'],
                          rows['C400000002']['claim'],
                          rows['C400000002']['shipment']),
                         ('CC00900001', 125, 123456, ago(70)))
        got = self.listed(ada)
        self.assertEqual((got['C400000002']['st'], got['C400000002']['rq']),
                         ('asked', sent))
        audit = db.one("SELECT detail FROM audit_log WHERE store_id = %s "
                       "AND action = 'requested_marked'", (store['id'],))
        self.assertEqual((audit['detail']['tickets'],
                          audit['detail']['date'],
                          audit['detail']['replaced_version']),
                         (2, sent, v1['id']))
        history = ada.get('/history').get_data(as_text=True)
        self.assertIn('Request dates', history)
        self.assertEqual(len(storage.master_versions(store['id'])), 2)

        # the same again changes nothing and saves nothing
        r = self.ask(ada, tickets=['C400000002'], date=sent,
                     master=v2['id'])
        self.assertEqual(r.get_json(), {'changed': 0})
        self.assertEqual(storage.current_master(store['id'])['id'],
                         v2['id'])
        # a page loaded before the master changed is told to reload
        r = self.ask(ada, tickets=['C400000005'], date=sent,
                     master=v1['id'])
        self.assertEqual(r.status_code, 400)
        self.assertIn('Reload the page', r.get_json()['error'])
        # not while a run is working for the store
        was = web.recon_jobs.store_busy
        web.recon_jobs.store_busy = lambda store_id: True
        try:
            r = self.ask(ada, tickets=['C400000005'], date=sent,
                         master=v2['id'])
        finally:
            web.recon_jobs.store_busy = was
        self.assertEqual(r.status_code, 409)
        self.assertIn('A run is working', r.get_json()['error'])
        self.assertEqual(storage.current_master(store['id'])['id'],
                         v2['id'])

        # the next run leaves the marked tickets off its credit request
        _, job = self.run_recon(ada, {
            'dc_text': 'C400000002\t22222222AB\t\t125.00\t\t\t',
            'asof': today.isoformat(), 'threshold': '60'})
        self.assertEqual(job['status'], 'done', job.get('error'))
        result = job['result']
        self.assertEqual(result['inputs']['master_rows'], 5)
        self.assertEqual([x['ticket'] for x in result['credit_request']],
                         [])
        self.assertEqual(sorted(x['ticket']
                                for x in result['prev_requested']),
                         ['C400000002', 'C400000004'])

        # clearing the date puts the ticket back in line
        v3 = storage.current_master(store['id'])
        r = self.ask(ada, tickets=['C400000002'], date='',
                     master=v3['id'])
        self.assertEqual(r.get_json(), {'changed': 1})
        self.assertIsNone(self.master_rows(store)['C400000002']['requested'])
        self.assertEqual(self.listed(ada)['C400000002']['st'], 'ready')
        self.assertTrue(db.one(
            "SELECT 1 AS x FROM audit_log WHERE store_id = %s AND "
            "action = 'requested_cleared'", (store['id'],)))
        # and the version before it can still be put back
        self.assertEqual(self.post(
            ada, f'/master/{v3["id"]}/restore').status_code, 302)
        self.assertEqual(self.master_rows(store)['C400000002']['requested'],
                         sent)

    def test_one_store_never_sees_or_changes_another_stores_tickets(self):
        from accounts import storage, web
        mine = self.make_store('Mine Cores', 'me@minecores.test')
        other = self.make_store('Other Cores', 'you@othercores.test')
        me = self.sign_in('me@minecores.test')
        you = self.sign_in('you@othercores.test')
        # the same ticket numbers in both stores
        self.save_master(me, week())
        self.save_master(you, week())
        self.note(me, tickets=['C400000002'], status='denied',
                  note='mine only')
        got = self.listed(you)['C400000002']
        self.assertEqual((got['s'], got['n']), ('', ''))
        mine_v = storage.current_master(mine['id'])
        other_v = storage.current_master(other['id'])
        # the other store's page cannot name my version of the master
        r = self.ask(you, tickets=['C400000002'],
                     date=web._today().isoformat(), master=mine_v['id'])
        self.assertEqual(r.status_code, 400)
        r = self.ask(you, tickets=['C400000002'],
                     date=web._today().isoformat(), master=other_v['id'])
        self.assertEqual(r.get_json(), {'changed': 1})
        self.assertEqual(storage.current_master(mine['id'])['id'],
                         mine_v['id'])
        self.assertIsNone(self.master_rows(mine)['C400000002']['requested'])
        self.assertEqual(self.listed(me)['C400000002']['n'], 'mine only')

        # signed out: sent to sign in, and nothing is taken
        out = self.client()
        r = out.get('/cores')
        self.assertEqual(r.status_code, 302)
        self.assertIn('/login', r.headers['Location'])
        self.assertEqual(out.post('/api/cores/notes', json={
            'tickets': ['C400000002'], 'note': 'x'}).status_code, 401)
        self.assertEqual(out.get('/cores/export').status_code, 302)
        # signed in, but no page token: refused before anything is read
        self.assertEqual(me.post('/api/cores/requested', json={
            'tickets': ['C400000002'], 'date': ''}).status_code, 400)
        self.assertEqual(me.post('/api/cores/notes', json={
            'tickets': ['C400000002'], 'note': 'x'}).status_code, 400)
        self.assertEqual(self.listed(me)['C400000002']['n'], 'mine only')


class TestAccountsOff(unittest.TestCase):
    def test_the_page_is_not_there_without_accounts(self):
        saved = os.environ.pop('DATABASE_URL', None)
        try:
            import app as webapp
            c = webapp.app.test_client()
            for path in ('/cores', '/cores/export', '/api/cores/credited'):
                self.assertEqual(c.get(path).status_code, 404, path)
            for path in ('/api/cores/notes', '/api/cores/requested'):
                self.assertEqual(c.post(path, json={}).status_code, 404,
                                 path)
            self.assertNotIn(b'Unpaid Cores', c.get('/').data)
        finally:
            if saved is not None:
                os.environ['DATABASE_URL'] = saved


if __name__ == '__main__':
    unittest.main()
