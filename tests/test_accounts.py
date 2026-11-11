"""Tests for store accounts: sign-in links, one store never seeing another's
data, the saved master and the run history.

These need a Postgres database to run against. Point TEST_DATABASE_URL at
an EMPTY scratch database (everything in it is dropped):

    TEST_DATABASE_URL=postgresql://user@localhost/scratch \\
        python -m unittest discover -s tests -v

Without TEST_DATABASE_URL these tests are skipped and the rest still run.
All data is made up.
"""
import io
import os
import re
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(
    __file__))))

TEST_DB = os.environ.get('TEST_DATABASE_URL', '').strip()
OWNER = 'owner@example.com'


def row(memo, shp, ticket, part, amount, claim=None, req=None):
    return {'memo': memo, 'shipment': shp, 'ticket': ticket, 'part': part,
            'claim': claim, 'amount': amount, 'requested': req}


def master_bytes(rows):
    from recon import master as M
    d = tempfile.mkdtemp()
    try:
        p = os.path.join(d, 'm.xlsx')
        M.write_master(rows, p)
        with open(p, 'rb') as fh:
            return fh.read()
    finally:
        shutil.rmtree(d, ignore_errors=True)


def many_rows(n, start=0):
    return [row(None, '2026-09-01 17:00.01', f'C4100{start + i:05d}',
                '11111111AA', 50) for i in range(n)]


class TestOpenMode(unittest.TestCase):
    """With no database configured nothing about the app changes."""

    def setUp(self):
        self.saved = os.environ.pop('DATABASE_URL', None)
        import app as webapp
        self.client = webapp.app.test_client()

    def tearDown(self):
        if self.saved is not None:
            os.environ['DATABASE_URL'] = self.saved

    def test_pages_are_open_and_account_pages_do_not_exist(self):
        home = self.client.get('/')
        self.assertEqual(home.status_code, 200)
        self.assertIn(b'Quick Check', home.data)
        self.assertNotIn(b'Sign out', home.data)
        self.assertEqual(self.client.get('/reconcile').status_code, 200)
        self.assertNotIn(b'/terms', home.data)
        for path in ('/login', '/signup', '/terms', '/privacy', '/history',
                     '/users', '/owner', '/stores', '/master/download'):
            self.assertEqual(self.client.get(path).status_code, 404, path)
        # posting needs no token when accounts are off
        self.assertEqual(self.client.post('/api/reconcile',
                                          data={}).status_code, 400)
        self.assertNotIn('Set-Cookie', home.headers)

    def test_another_address_is_not_sent_anywhere(self):
        saved = os.environ.get('APP_BASE_URL')
        os.environ['APP_BASE_URL'] = 'https://site.example'
        try:
            r = self.client.get('/', base_url='https://other.example')
        finally:
            if saved is None:
                os.environ.pop('APP_BASE_URL', None)
            else:
                os.environ['APP_BASE_URL'] = saved
        self.assertEqual(r.status_code, 200)

    def test_memo_number_prefix_is_not_fixed(self):
        from app import parse_credits
        for prefix in ('03181000', '04220000', ''):
            text = (f'CREDIT MEMO NUMBER: {prefix}CC00900001\n'
                    f'REFERENCE/CONTROL NUMBER C400000001\n')
            self.assertEqual(parse_credits(text),
                             {'C400000001': 'CC00900001'}, prefix)


@unittest.skipUnless(TEST_DB, 'set TEST_DATABASE_URL to run account tests')
class AccountsCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.env = {k: os.environ.get(k) for k in (
            'DATABASE_URL', 'MAIL_BACKEND', 'OWNER_EMAIL', 'APP_BASE_URL',
            'RENDER_EXTERNAL_URL', 'RECON_JOB_DIR')}
        cls.tmp = tempfile.mkdtemp()
        os.environ.pop('RENDER_EXTERNAL_URL', None)
        os.environ.update(DATABASE_URL=TEST_DB, MAIL_BACKEND='memory',
                          OWNER_EMAIL=OWNER, RECON_JOB_DIR=cls.tmp,
                          APP_BASE_URL='http://localhost')
        from accounts import db
        db.close()
        import psycopg
        with psycopg.connect(TEST_DB, autocommit=True) as conn:
            conn.execute('DROP SCHEMA public CASCADE')
            conn.execute('CREATE SCHEMA public')
        from recon import jobs
        jobs.JOB_ROOT = cls.tmp
        import app as webapp
        cls.app = webapp.app

    @classmethod
    def tearDownClass(cls):
        from accounts import db
        db.close()
        for k, v in cls.env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        from accounts import db, mailer
        mailer.outbox.clear()
        # each test starts with no sign-in requests counted against it
        db.execute('DELETE FROM login_attempts')

    # ── helpers ─────────────────────────────────────────────────────────
    def client(self):
        return self.app.test_client()

    def guard(self, c):
        """What a freshly loaded page hands the browser: (page token,
        id of the store the page is showing)."""
        c.get('/login', follow_redirects=True)
        with c.session_transaction() as s:
            return s['csrf'], str(s.get('sid', ''))

    def post(self, c, path, data=None, **kw):
        data = dict(data or {})
        token, store = self.guard(c)
        data.setdefault('csrf_token', token)
        data.setdefault('store_check', store)
        return c.post(path, data=data, **kw)

    def last_link(self, to):
        from accounts import mailer
        mails = [m for m in mailer.outbox if m['to'] == to]
        self.assertTrue(mails, f'no mail to {to}')
        m = re.search(r'http://localhost(/login/link/\S+)',
                      mails[-1]['text'])
        self.assertTrue(m, mails[-1]['text'])
        return m.group(1)

    def use_link(self, c, link):
        self.assertEqual(c.get(link).status_code, 200)
        r = self.post(c, link)
        self.assertEqual(r.status_code, 302, r.get_data(as_text=True))
        return r

    def sign_in(self, email):
        c = self.client()
        r = self.post(c, '/login', {'email': email})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.use_link(c, self.last_link(email))
        return c

    def make_store(self, name, admin, dealer='12345'):
        owner = self.sign_in(OWNER)
        r = self.post(owner, '/owner/stores', {
            'name': name, 'dealer_code': dealer, 'admin_email': admin,
            'send_invite': '1'})
        self.assertEqual(r.status_code, 302)
        from accounts import db
        return db.one('SELECT * FROM stores WHERE name = %s ORDER BY id '
                      'DESC LIMIT 1', (name,))

    def start_recon(self, c, data):
        token, store = self.guard(c)
        return c.post('/api/reconcile', data=data,
                      content_type='multipart/form-data',
                      headers={'X-CSRF-Token': token, 'X-Store-Id': store})

    def run_recon(self, c, data):
        r = self.start_recon(c, data)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        job_id = r.get_json()['job_id']
        for _ in range(400):
            job = c.get(f'/api/jobs/{job_id}').get_json()
            if job['status'] in ('done', 'error'):
                break
            time.sleep(0.05)
        return job_id, job

    def first_run(self, c, ticket='C400000001'):
        content = master_bytes([row(None, '2026-07-01 17:00.01', ticket,
                                    '11111111AA', 50)])
        return self.run_recon(c, {
            'master': (io.BytesIO(content), 'Core_Returns_RECONCILED.xlsx'),
            'dc_text': f'{ticket}\t11111111AA\t\t50.00\t\t\t',
            'asof': '2026-10-05', 'threshold': '60'})


class TestSignIn(AccountsCase):
    def test_signed_out_visitors_see_the_front_page_only(self):
        c = self.client()
        home = c.get('/')
        self.assertEqual(home.status_code, 200)
        self.assertIn(b'Sign in', home.data)
        self.assertNotIn(b'Drop shipper PDF', home.data)
        r = c.get('/reconcile')
        self.assertEqual(r.status_code, 302)
        self.assertIn('/login', r.headers['Location'])
        for path in ('/history', '/users', '/owner', '/stores',
                     '/master/download'):
            self.assertEqual(c.get(path).status_code, 302, path)
        self.assertEqual(c.get('/api/jobs/' + '0' * 32).status_code, 401)
        # a signed-out upload is turned away without a page token check
        # (so before its body is read)
        r = c.post('/api/reconcile', data={
            'memos': (io.BytesIO(b'x' * 200000), 'big.pdf')},
            content_type='multipart/form-data')
        self.assertEqual(r.status_code, 401)
        self.assertEqual(c.post('/api/check-cores').status_code, 401)
        # A form is small. One that does not say how big it is (a proxy
        # may pass it on that way) still works...
        token, _ = self.guard(c)
        form = {'email': 'x@nowhere.test', 'csrf_token': token}
        r = c.post('/login', data=form, environ_overrides={
            'CONTENT_LENGTH': '', 'wsgi.input_terminated': True})
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'Check your email', r.data)
        # ...and a big one is cut off, stated size or not
        big = dict(form, pad='x' * 70000)
        r = c.post('/login', data=big)
        self.assertEqual(r.status_code, 413)
        self.assertIn(b'Too much at once', r.data)
        r = c.post('/login', data=big, environ_overrides={
            'CONTENT_LENGTH': '', 'wsgi.input_terminated': True})
        self.assertEqual(r.status_code, 413)

    def test_support_address_is_shown_only_when_set(self):
        self.make_store('Help Store', 'hana@help.test')
        hana = self.sign_in('hana@help.test')
        pages = ('/quick', '/reconcile', '/history', '/users')
        for path in pages:
            self.assertNotIn(b'Need help?', hana.get(path).data, path)
        os.environ['SUPPORT_EMAIL'] = 'help@partsmanagersolutions.com'
        try:
            for c, paths in ((hana, pages), (self.client(), ('/', '/login'))):
                for path in paths:
                    self.assertIn(
                        b'mailto:help@partsmanagersolutions.com',
                        c.get(path).data, path)
            # anything that is not an email address is ignored
            os.environ['SUPPORT_EMAIL'] = '"><script>alert(1)</script>'
            self.assertNotIn(b'Need help?', hana.get('/history').data)
        finally:
            os.environ.pop('SUPPORT_EMAIL', None)

    def test_link_signs_in_once(self):
        self.make_store('Link Store', 'pat@linkstore.test')
        c = self.client()
        self.post(c, '/login', {'email': 'Pat@LinkStore.test '})
        link = self.last_link('pat@linkstore.test')
        # opening the link (as a mail scanner would) does not use it up
        self.assertEqual(self.client().get(link).status_code, 200)
        self.use_link(c, link)
        page = c.get('/reconcile')
        self.assertEqual(page.status_code, 200)
        self.assertIn(b'Link Store', page.data)
        self.assertIn(b'pat@linkstore.test', page.data)
        # the same link is dead now, for everyone
        other = self.client()
        self.assertEqual(other.get(link).status_code, 410)
        self.assertEqual(self.post(other, link).status_code, 410)

    def test_a_scanner_probing_the_link_neither_uses_it_nor_signs_in(self):
        self.make_store('Probe Store', 'pru@probe.test')
        c = self.client()
        self.post(c, '/login', {'email': 'pru@probe.test'})
        link = self.last_link('pru@probe.test')
        probe = self.client()
        for _ in range(3):
            self.assertEqual(probe.head(link).status_code, 200)
            self.assertEqual(probe.open(link, method='OPTIONS').status_code,
                             200)
            with probe.session_transaction() as s:
                self.assertNotIn('sx', s)           # not signed in
        self.assertEqual(probe.get('/reconcile').status_code, 302)
        # a post without the page's token does nothing either
        self.assertEqual(probe.post(link).status_code, 400)
        # the real person can still use it
        self.use_link(c, link)
        self.assertEqual(c.get('/reconcile').status_code, 200)
        # HEAD on the sign-in form never sends mail
        from accounts import mailer
        mailer.outbox.clear()
        self.client().head('/login', data={'email': 'pru@probe.test'})
        self.assertEqual(mailer.outbox, [])

    def test_a_double_click_on_continue_still_signs_in(self):
        self.make_store('Twice Store', 'tia@twice.test')
        # both clicks leave before the first answer is back: same cookie
        c = self.client()
        self.post(c, '/login', {'email': 'tia@twice.test'})
        link = self.last_link('tia@twice.test')
        self.assertEqual(c.get(link).status_code, 200)
        token, _ = self.guard(c)
        before = c.get_cookie('pms_session').value
        first = c.post(link, data={'csrf_token': token})
        self.assertEqual(first.status_code, 302)
        after = c.get_cookie('pms_session').value
        c.set_cookie('pms_session', before)         # what click 2 carried
        second = c.post(link, data={'csrf_token': token})
        self.assertEqual(second.status_code, 302)
        self.assertEqual(c.get('/reconcile').status_code, 200)
        # whichever answer the browser keeps, it is signed in
        c.set_cookie('pms_session', after)
        self.assertEqual(c.get('/reconcile').status_code, 200)
        # the second click leaving after the first answer arrived: new
        # cookie, old page token. Already signed in, so just go on.
        late = c.post(link, data={'csrf_token': token})
        self.assertEqual(late.status_code, 302)
        self.assertTrue(late.headers['Location'].endswith('/reconcile'))
        # none of this lets another browser use the link
        other = self.client()
        self.assertEqual(other.get(link).status_code, 410)
        self.assertEqual(self.post(other, link).status_code, 410)
        # and the forgiveness is short
        from accounts import db
        db.execute("UPDATE login_tokens SET used_at = used_at - "
                   "interval '2 minutes'")
        c2 = self.client()
        c2.set_cookie('pms_session', before)
        self.assertEqual(c2.post(link, data={
            'csrf_token': token}).status_code, 410)

    def test_own_used_link_just_opens_the_app_when_signed_in(self):
        self.make_store('Again Store', 'ada@again.test')
        self.make_store('Other Again', 'oli@otheragain.test')
        c = self.client()
        self.post(c, '/login', {'email': 'ada@again.test'})
        link = self.last_link('ada@again.test')
        self.use_link(c, link)
        # the emailed link clicked again, or Back after signing in
        r = c.get(link)
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.headers['Location'].endswith('/reconcile'))
        # someone else's dead link is still a dead link
        oli = self.sign_in('oli@otheragain.test')
        self.assertEqual(oli.get(link).status_code, 410)
        self.assertEqual(self.client().get(link).status_code, 410)

    def test_using_one_link_retires_the_others(self):
        self.make_store('Retire Store', 'rex@retire.test')
        invite = self.last_link('rex@retire.test')
        c = self.client()
        self.post(c, '/login', {'email': 'rex@retire.test'})
        older = self.last_link('rex@retire.test')
        self.post(c, '/login', {'email': 'rex@retire.test'})
        newest = self.last_link('rex@retire.test')
        self.assertNotEqual(older, newest)
        self.use_link(c, newest)
        for dead in (older, invite):
            self.assertEqual(self.client().get(dead).status_code, 410)

    def test_unknown_email_gets_the_same_answer_and_no_mail(self):
        from accounts import mailer
        self.make_store('Known Store', 'kim@known.test')
        mailer.outbox.clear()
        known = self.post(self.client(), '/login',
                          {'email': 'kim@known.test'})
        unknown = self.post(self.client(), '/login',
                            {'email': 'nobody@nowhere.test'})
        self.assertEqual((known.status_code, unknown.status_code),
                         (200, 200))
        self.assertIn(b'Check your email', unknown.data)
        self.assertEqual(
            known.get_data(as_text=True).replace('kim@known.test', 'E'),
            unknown.get_data(as_text=True).replace('nobody@nowhere.test',
                                                    'E'))
        self.assertEqual([m['to'] for m in mailer.outbox],
                         ['kim@known.test'])
        for bad in ('', 'not-an-email', 'a@b', 'x\x00y@nowhere.test',
                    'a b@nowhere.test'):
            self.assertEqual(self.post(self.client(), '/login',
                                       {'email': bad}).status_code, 400, bad)

    def test_next_only_ever_leads_to_the_apps_own_pages(self):
        from accounts import web
        for good in ('/', '/reconcile', '/history', '/users', '/owner',
                     '/stores', '/history?x=1'):
            self.assertEqual(web._safe_next(good), good)
        for bad in ('/login/link/abc', '/login', '/logout', '/api/jobs/x',
                    '/switch-store', '/owner/open/3', '//evil.example',
                    'https://evil.example/', '/\\evil.example',
                    '/history\r\nX: y', 'reconcile', '', None,
                    '/master/1/restore'):
            self.assertEqual(web._safe_next(bad), '', bad)
        # end to end: a crafted sign-in address cannot chain the person
        # into someone else's sign-in link
        self.make_store('Next Store', 'nan@next.test')
        self.make_store('Trap Store', 'mal@trap.test')
        trap = self.last_link('mal@trap.test')
        c = self.client()
        page = c.get('/login?next=' + trap)
        self.assertNotIn(trap.encode(), page.data)
        self.post(c, '/login', {'email': 'nan@next.test', 'next': trap})
        r = self.use_link(c, self.last_link('nan@next.test'))
        self.assertTrue(r.headers['Location'].endswith('/reconcile'))
        # a real next is honoured once, and not reused by a later sign-in
        c2 = self.client()
        self.post(c2, '/login', {'email': 'nan@next.test',
                                 'next': '/history'})
        r = self.use_link(c2, self.last_link('nan@next.test'))
        self.assertTrue(r.headers['Location'].endswith('/history'))
        self.post(c2, '/logout')
        self.post(c2, '/login', {'email': 'nan@next.test'})
        r = self.use_link(c2, self.last_link('nan@next.test'))
        self.assertTrue(r.headers['Location'].endswith('/reconcile'))
        # opening another person's link while signed in says so plainly
        warn = c.get(trap).get_data(as_text=True)
        self.assertIn('signed in as', warn)
        self.assertIn('nan@next.test', warn)
        self.assertIn('mal@trap.test', warn)

    def test_expired_link_is_dead(self):
        from accounts import core, db
        self.make_store('Expiry Store', 'ed@expiry.test')
        user = core.user_by_email('ed@expiry.test')
        token = core.new_login_token(user['id'])
        db.execute("UPDATE login_tokens SET expires_at = now() - "
                   "interval '1 minute' WHERE user_id = %s", (user['id'],))
        c = self.client()
        self.assertEqual(c.get(f'/login/link/{token}').status_code, 410)
        self.assertEqual(self.post(c, f'/login/link/{token}').status_code,
                         410)

    def test_too_many_requests_are_refused(self):
        from accounts import core
        c = self.client()
        codes = [self.post(c, '/login',
                           {'email': 'flood@nowhere.test'}).status_code
                 for _ in range(core.MAX_PER_EMAIL_AND_IP + 1)]
        self.assertEqual(codes[:-1], [200] * core.MAX_PER_EMAIL_AND_IP)
        self.assertEqual(codes[-1], 429)
        # a refused flood does not run up the email's own allowance:
        # the real person, asking from their own address, still gets in
        for _ in range(40):
            self.post(c, '/login', {'email': 'flood@nowhere.test'})
        token, _ = self.guard(c)
        r = c.post('/login', data={'email': 'flood@nowhere.test',
                                   'csrf_token': token},
                   headers={'CF-Connecting-IP': '203.0.113.9'})
        self.assertEqual(r.status_code, 200)

    def test_posts_need_the_page_token(self):
        c = self.sign_in(OWNER)
        self.assertEqual(c.post('/logout').status_code, 400)
        self.assertEqual(c.post('/api/reconcile', data={}).status_code, 400)
        self.assertEqual(c.post('/owner/stores', data={
            'name': 'x', 'admin_email': 'x@x.test',
            'csrf_token': 'wrong'}).status_code, 400)
        # still signed in: a refused post does not sign anyone out
        self.assertEqual(c.get('/owner').status_code, 200)

    def test_sign_out_ends_the_session_on_the_server(self):
        self.make_store('Out Store', 'oz@out.test')
        c = self.sign_in('oz@out.test')
        self.assertEqual(c.get('/reconcile').status_code, 200)
        stolen = c.get_cookie('pms_session').value
        self.assertEqual(self.post(c, '/logout').status_code, 302)
        self.assertEqual(c.get('/reconcile').status_code, 302)
        # a copy of the old cookie is worthless now
        thief = self.client()
        thief.set_cookie('pms_session', stolen)
        self.assertEqual(thief.get('/reconcile').status_code, 302)
        self.assertEqual(thief.get('/api/jobs/' + '0' * 32).status_code,
                         401)

    def test_the_cookie_is_not_resent_on_every_answer(self):
        self.make_store('Quiet Store', 'quy@quiet.test')
        c = self.sign_in('quy@quiet.test')
        c.get('/reconcile')
        r = c.get('/api/jobs/' + '0' * 32)        # like the status poll
        self.assertEqual(r.status_code, 404)
        self.assertNotIn('Set-Cookie', r.headers)

    def test_emailed_links_ignore_a_forged_host_header(self):
        from accounts import mailer
        self.make_store('Host Store', 'hal@host.test')
        mailer.outbox.clear()
        c = self.client()
        evil = 'http://evil.example'     # the whole exchange is forged
        # the sign-in page is only ever served at the site's own address
        r = c.get('/login', base_url=evil)
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r.headers['Location'], 'http://localhost/login')
        token, _ = self.guard(c)
        r = c.post('/login', data={'email': 'hal@host.test',
                                   'csrf_token': token}, base_url=evil)
        self.assertEqual(r.status_code, 409)
        self.assertNotIn(b'evil.example', r.data)
        self.assertEqual(mailer.outbox, [])

    def test_another_address_of_the_site_leads_to_its_own_address(self):
        from accounts import mailer
        self.make_store('Moved Store', 'mo@moved.test')
        other = 'https://old-name.example'
        c = self.client()
        # a page: sent on to the same page, with what came after the ?
        for method in (c.get, c.head):
            r = method('/login?next=/history', base_url=other)
            self.assertEqual(r.status_code, 302)
            self.assertEqual(r.headers['Location'],
                             'http://localhost/login?next=/history')
            self.assertNotIn('Set-Cookie', r.headers)
        self.assertEqual(c.get('/', base_url=other).headers['Location'],
                         'http://localhost/')
        # an emailed link opened there is sent on too, and not used up
        self.post(c, '/login', {'email': 'mo@moved.test'})
        link = self.last_link('mo@moved.test')
        r = c.get(link, base_url=other)
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r.headers['Location'], 'http://localhost' + link)
        # a form posted from a page loaded there: told where the site is,
        # and nothing is done with it
        mailer.outbox.clear()
        token, _ = self.guard(c)
        r = c.post('/login', data={'email': 'mo@moved.test',
                                   'csrf_token': token}, base_url=other)
        self.assertEqual(r.status_code, 409)
        self.assertIn(b'This site is at http://localhost now', r.data)
        self.assertIn(b'href="http://localhost/login"', r.data)
        self.assertEqual(mailer.outbox, [])
        r = c.post(link, data={'csrf_token': token}, base_url=other)
        self.assertEqual(r.status_code, 409)
        self.assertIn(b'href="http://localhost' + link.encode(), r.data)
        # the app's own calls get the same answer in their own form, so a
        # page left open there shows it instead of waiting for ever
        for r in (c.get('/api/jobs/' + '0' * 32, base_url=other),
                  c.post('/api/check-cores', base_url=other)):
            self.assertEqual(r.status_code, 409)
            self.assertIn('http://localhost', r.get_json()['error'])
        # the health check answers at any address
        self.assertEqual(c.get('/healthz', base_url=other).data, b'ok')
        # the site's own address in capitals is still its own address
        self.assertEqual(c.get('/login',
                               base_url='http://LOCALHOST').status_code, 200)
        # The web host's proxy speaks plain http to the app. That is not
        # another address: sent on, the request would come back for ever.
        os.environ['APP_BASE_URL'] = 'https://localhost'
        try:
            self.assertEqual(c.get('/login').status_code, 200)
        finally:
            os.environ['APP_BASE_URL'] = 'http://localhost'
        # the link still works at the site's own address
        self.use_link(c, link)
        self.assertEqual(c.get('/reconcile').status_code, 200)

    def test_no_configured_address_means_no_links_in_production(self):
        from accounts import mailer
        self.make_store('Base Store', 'bo@base.test')
        saved = {k: os.environ.pop(k, None)
                 for k in ('APP_BASE_URL', 'MAIL_BACKEND')}
        os.environ['RESEND_API_KEY'] = 'test-key-never-used'
        try:
            mailer.outbox.clear()
            r = self.post(self.client(), '/login', {'email': 'bo@base.test'})
            self.assertEqual(r.status_code, 503)
        finally:
            os.environ.pop('RESEND_API_KEY', None)
            for k, v in saved.items():
                if v is not None:
                    os.environ[k] = v


class TestSignUp(AccountsCase):
    def sign_up(self, c, name, dealer, email):
        r = self.post(c, '/signup', {'name': name, 'dealer_code': dealer,
                                     'email': email})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertIn(b'Check your email', r.data)
        return r

    def stores_named(self, name):
        from accounts import db
        return db.one('SELECT count(*) AS n FROM stores WHERE name = %s',
                      (name,))['n']

    def test_a_store_signs_itself_up(self):
        from accounts import db, mailer
        c = self.client()
        self.assertIn(b'Sign up your store', c.get('/').data)
        self.assertIn(b'Sign up your store', c.get('/login').data)
        page = c.get('/signup')
        self.assertEqual(page.status_code, 200)
        self.assertIn(b'Dealer code', page.data)
        self.sign_up(c, ' Self  Serve Motors ', '4-1234',
                     'Sam@SelfServe.test')
        # nothing is made until the person shows the email is theirs
        self.assertEqual(self.stores_named('Self Serve Motors'), 0)
        self.assertEqual(c.get('/reconcile').status_code, 302)
        # the email carries nothing that was typed into the form
        mail = [m for m in mailer.outbox if m['to'] == 'sam@selfserve.test']
        self.assertEqual(len(mail), 1)
        self.assertNotIn('Self Serve',
                         mail[0]['subject'] + mail[0]['text'] +
                         mail[0]['html'])
        link = self.last_link('sam@selfserve.test')
        # a mail scanner that opens the link makes nothing
        look = self.client().get(link)
        self.assertEqual(look.status_code, 200)
        self.assertIn(b'Self Serve Motors', look.data)
        self.assertEqual(self.stores_named('Self Serve Motors'), 0)
        # and the sign-in page has nothing for them yet
        mailer.outbox.clear()
        self.post(self.client(), '/login', {'email': 'sam@selfserve.test'})
        self.assertEqual(mailer.outbox, [])
        self.assertIn(b'It opens when we approve it', look.data)
        # Continue: the store, its admin and the sign-in, in one step
        self.use_link(c, link)
        store = db.one("SELECT * FROM stores WHERE name = "
                       "'Self Serve Motors'")
        self.assertEqual(store['dealer_code'], '41234')
        who = db.rows('SELECT u.email, m.role FROM memberships m JOIN '
                      'users u ON u.id = m.user_id WHERE m.store_id = %s',
                      (store['id'],))
        self.assertEqual([(w['email'], w['role']) for w in who],
                         [('sam@selfserve.test', 'admin')])
        # the store waits for the owner: nobody works in it, and it
        # sends no invitations
        self.assertTrue(store['waiting'])
        for path in ('/reconcile', '/quick', '/history', '/users'):
            page = c.get(path)
            self.assertEqual(page.status_code, 403, path)
            self.assertIn(b'Waiting for approval', page.data)
            self.assertIn(b'Self Serve Motors', page.data)
        r = self.start_recon(c, {'dc_text': 'x', 'asof': '2026-10-05'})
        self.assertEqual(r.status_code, 403)
        self.assertIn('waiting for approval', r.get_json()['error'])
        self.assertEqual(self.post(c, '/users/add', {
            'email': 'pal@selfserve.test', 'role': 'user'}).status_code, 403)
        # the owner is told once, and the store is in the owner's list
        told = [m for m in mailer.outbox if m['to'] == OWNER]
        self.assertEqual(len(told), 1)
        self.assertIn('waiting for approval', told[0]['subject'])
        self.assertIn('Self Serve Motors', told[0]['subject'])
        self.assertIn('sam@selfserve.test', told[0]['text'])
        self.assertIn('41234', told[0]['text'])
        owner = self.sign_in(OWNER)
        page = owner.get('/owner')
        self.assertIn(b'Self Serve Motors', page.data)
        self.assertIn(b'Waiting', page.data)
        approve = f'/owner/approve/{store["id"]}'
        self.assertIn(approve.encode(), page.data)
        # only the owner approves
        self.assertEqual(self.post(c, approve).status_code, 404)
        mailer.outbox.clear()
        r = self.post(owner, approve, follow_redirects=True)
        self.assertIn(b'Self Serve Motors is approved', r.data)
        self.assertNotIn(approve.encode(), r.data)
        mail = [m for m in mailer.outbox if m['to'] == 'sam@selfserve.test']
        self.assertEqual(len(mail), 1)
        self.assertIn('is approved', mail[0]['subject'])
        self.assertIn('http://localhost/login', mail[0]['text'])
        # the store is open now
        page = c.get('/reconcile')
        self.assertEqual(page.status_code, 200)
        self.assertIn(b'Self Serve Motors', page.data)
        self.assertEqual(c.get('/users').status_code, 200)
        r = self.post(owner, approve, follow_redirects=True)
        self.assertIn(b'not waiting for approval', r.data)
        # the link is dead now, and sign-in works the usual way
        self.assertEqual(self.client().get(link).status_code, 410)
        sam = self.sign_in('sam@selfserve.test')
        self.assertIn(b'Self Serve Motors', sam.get('/reconcile').data)

    def test_a_store_opens_at_once_when_approval_is_off(self):
        from accounts import db, mailer
        os.environ['SIGNUP_APPROVAL'] = 'off'
        try:
            c = self.client()
            self.assertNotIn(b'approve each new store', c.get('/signup').data)
            self.sign_up(c, 'Open Now Motors', '41235', 'ona@opennow.test')
            self.use_link(c, self.last_link('ona@opennow.test'))
            page = c.get('/reconcile')
            self.assertEqual(page.status_code, 200)
            self.assertIn(b'Open Now Motors', page.data)
            self.assertFalse(db.one("SELECT waiting FROM stores WHERE name "
                                    "= 'Open Now Motors'")['waiting'])
            told = [m for m in mailer.outbox if m['to'] == OWNER]
            self.assertEqual(len(told), 1)
            self.assertIn('New store signed up', told[0]['subject'])
        finally:
            os.environ.pop('SIGNUP_APPROVAL', None)

    def test_a_dealer_code_that_has_a_store_is_answered_by_email(self):
        from accounts import core, db, mailer
        self.make_store('Taken Motors', 'tia@taken.test', dealer='55555')
        mailer.outbox.clear()
        c = self.client()
        new = self.sign_up(c, 'New Motors', '55556', 'ann@new.test')
        # the page says the same for a dealer code that has a store,
        # however the second person spells the name of the store
        for name in ('Taken Motors', 'taken motors inc'):
            mailer.outbox.clear()
            r = self.sign_up(c, name, '55555', 'ann@new.test')
            self.assertEqual(r.data, new.data)
            self.assertNotIn(b'already', r.data)
            # the email tells them, and has no link that makes a store
            mail = [m for m in mailer.outbox if m['to'] == 'ann@new.test']
            self.assertEqual(len(mail), 1)
            self.assertIn('already set up', mail[0]['text'])
            self.assertNotIn('/login/link/', mail[0]['text'])
            self.assertNotIn('aken', mail[0]['subject'] + mail[0]['text'] +
                             mail[0]['html'])
        self.assertEqual(db.one("SELECT count(*) AS n FROM stores WHERE "
                                "dealer_code = '55555'")['n'], 1)
        # these emails count toward the limit for the day too
        asked = db.one('SELECT count(*) AS n FROM login_tokens WHERE '
                       'signup_name IS NOT NULL')['n']
        self.assertGreaterEqual(asked, 3)
        saved, core.MAX_SIGNUPS_PER_DAY = core.MAX_SIGNUPS_PER_DAY, asked
        try:
            mailer.outbox.clear()
            r = self.post(self.client(), '/signup', {
                'name': 'Taken Motors', 'dealer_code': '55555',
                'email': 'bob@new.test'})
            self.assertEqual(r.status_code, 400)
            self.assertIn(b'paused for today', r.data)
            self.assertEqual(mailer.outbox, [])
        finally:
            core.MAX_SIGNUPS_PER_DAY = saved

    def test_what_the_sign_up_page_refuses(self):
        from accounts import mailer
        c = self.client()
        new = {'name': 'New Motors', 'dealer_code': '11111',
               'email': 'ann@new.test'}
        for change, said in (
                ({'name': ' '}, b'Enter the store name.'),
                ({'dealer_code': 'none'}, b'Enter your dealer code'),
                ({'email': 'not-an-email'}, b'Enter a valid work email')):
            r = self.post(c, '/signup', dict(new, **change))
            self.assertEqual(r.status_code, 400, change)
            self.assertIn(said, r.data)
        # what was typed is still in the form
        self.assertIn(b'value="New Motors"', r.data)
        self.assertIn(b'value="11111"', r.data)
        # a post without the page token does nothing
        self.assertEqual(c.post('/signup', data=new).status_code, 400)
        self.assertEqual(mailer.outbox, [])
        self.assertEqual(self.stores_named('New Motors'), 0)

    def test_sign_up_emails_are_limited(self):
        from accounts import core, db, mailer
        c = self.client()
        new = {'dealer_code': '22222', 'email': 'flood@signup.test'}
        codes = [self.post(c, '/signup',
                           dict(new, name=f'Flood {i}')).status_code
                 for i in range(core.MAX_PER_EMAIL_AND_IP + 1)]
        self.assertEqual(codes[:-1], [200] * core.MAX_PER_EMAIL_AND_IP)
        self.assertEqual(codes[-1], 429)
        # and there is a ceiling for the day, whoever asks
        asked = db.one('SELECT count(*) AS n FROM login_tokens WHERE '
                       'signup_name IS NOT NULL')['n']
        saved, core.MAX_SIGNUPS_PER_DAY = core.MAX_SIGNUPS_PER_DAY, asked
        try:
            mailer.outbox.clear()
            r = self.post(self.client(), '/signup', {
                'name': 'One Too Many', 'dealer_code': '33333',
                'email': 'late@signup.test'})
            self.assertEqual(r.status_code, 400)
            self.assertIn(b'paused for today', r.data)
            self.assertEqual(mailer.outbox, [])
        finally:
            core.MAX_SIGNUPS_PER_DAY = saved

    def test_two_links_or_two_clicks_make_one_store(self):
        c = self.client()
        self.sign_up(c, 'Twice Motors', '44441', 'tom@twice.test')
        first = self.last_link('tom@twice.test')
        self.sign_up(c, 'Twice Motors', '44441', 'tom@twice.test')
        second = self.last_link('tom@twice.test')
        self.assertNotEqual(first, second)
        # both clicks of a double click leave with the same cookie
        self.assertEqual(c.get(second).status_code, 200)
        token, _ = self.guard(c)
        before = c.get_cookie('pms_session').value
        self.assertEqual(c.post(second, data={
            'csrf_token': token}).status_code, 302)
        c.set_cookie('pms_session', before)
        self.assertEqual(c.post(second, data={
            'csrf_token': token}).status_code, 302)
        page = c.get('/reconcile')
        self.assertEqual(page.status_code, 403)
        self.assertIn(b'Twice Motors', page.data)
        # the other link died with the one that was used
        self.assertEqual(self.client().get(first).status_code, 410)
        self.assertEqual(self.stores_named('Twice Motors'), 1)

    def test_a_person_on_a_store_can_sign_up_another(self):
        self.make_store('First Of Two', 'gil@group.test')
        # signed in or not: a person with a second store need not sign out
        c = self.sign_in('gil@group.test')
        self.assertEqual(c.get('/signup').status_code, 200)
        self.sign_up(c, 'Second Of Two', '44443', 'gil@group.test')
        r = self.use_link(c, self.last_link('gil@group.test'))
        # two stores now: the person picks for themselves
        self.assertTrue(r.headers['Location'].endswith('/stores'))
        page = c.get('/stores')
        self.assertIn(b'First Of Two', page.data)
        self.assertIn(b'Second Of Two', page.data)
        # the first store works as before, and the new one waits
        self.assertEqual(c.get('/reconcile').status_code, 200)
        from accounts import db
        second = db.one("SELECT id FROM stores WHERE name = "
                        "'Second Of Two'")['id']
        self.post(c, '/switch-store', {'store_id': second})
        page = c.get('/reconcile')
        self.assertEqual(page.status_code, 403)
        self.assertIn(b'Second Of Two', page.data)
        self.assertIn(b'Your other stores', page.data)

    def test_a_store_set_up_while_the_link_waited_is_not_made_twice(self):
        c = self.client()
        self.sign_up(c, 'Race Motors', '44444', 'rae@race.test')
        link = self.last_link('rae@race.test')
        self.make_store('Race Motors Inc', 'other@race.test', dealer='44444')
        self.assertEqual(c.get(link).status_code, 200)
        r = self.post(c, link)
        self.assertEqual(r.status_code, 409)
        self.assertIn(b'already set up', r.data)
        self.assertEqual(self.stores_named('Race Motors'), 0)
        self.assertEqual(c.get('/reconcile').status_code, 302)

    def test_terms_and_privacy_are_open_to_all(self):
        c = self.client()
        for path, title in (('/terms', b'Terms of Service'),
                            ('/privacy', b'Privacy Policy')):
            page = c.get(path)
            self.assertEqual(page.status_code, 200, path)
            self.assertIn(title, page.data)
            self.assertIn(b'Zero Redundant LLC', page.data)
            # every page links to them at its foot
            self.assertIn(f'href="{path}"'.encode(), c.get('/').data)
            self.assertIn(f'href="{path}"'.encode(), c.get('/login').data)
            # and the sign-up form says that signing up agrees to them
            self.assertIn(f'href="{path}"'.encode(), c.get('/signup').data)
        self.assertIn(b'you agree to the', c.get('/signup').data)
        # signed in, they are still there
        self.make_store('Reader Motors', 'rea@reader.test')
        rea = self.sign_in('rea@reader.test')
        self.assertEqual(rea.get('/terms').status_code, 200)
        self.assertIn(b'href="/privacy"', rea.get('/reconcile').data)

    def test_the_sign_up_page_can_be_closed(self):
        os.environ['SIGNUPS'] = 'off'
        try:
            c = self.client()
            self.assertEqual(c.get('/signup').status_code, 404)
            r = self.post(c, '/signup', {
                'name': 'Closed Motors', 'dealer_code': '44445',
                'email': 'cy@closed.test'})
            self.assertEqual(r.status_code, 404)
            self.assertNotIn(b'Sign up', c.get('/').data)
            self.assertNotIn(b'Sign up', c.get('/login').data)
        finally:
            os.environ.pop('SIGNUPS', None)


class TestStores(AccountsCase):
    def test_the_owner_deletes_a_store(self):
        from accounts import db, mailer, web
        gone = self.make_store('Gone Motors', 'gus@gone.test', '66661')
        kept = self.make_store('Kept Motors', 'kim@kept.test', '66662')
        gus = self.sign_in('gus@gone.test')
        kim = self.sign_in('kim@kept.test')
        # kim is on both stores, and gus only on the one that goes
        self.assertEqual(self.post(gus, '/users/add', {
            'email': 'kim@kept.test', 'role': 'user'}).status_code, 302)
        _, job = self.first_run(gus)
        self.assertEqual(job['status'], 'done', job)

        def saved(store):
            return [db.one(f'SELECT count(*) AS n FROM {t} WHERE store_id '
                           f'= %s', (store['id'],))['n']
                    for t in ('memberships', 'masters', 'runs')]
        self.assertEqual(saved(gone), [2, 1, 1])
        files = db.one('SELECT count(*) AS n FROM run_files')['n']
        self.assertGreater(files, 0)

        page = f'/owner/delete/{gone["id"]}'
        # only the owner sees the page or uses it
        self.assertEqual(gus.get(page).status_code, 404)
        self.assertEqual(self.post(gus, page, {
            'confirm': 'Gone Motors'}).status_code, 404)
        owner = self.sign_in(OWNER)
        self.assertIn(page.encode(), owner.get('/owner').data)
        r = owner.get(page)
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'Delete Gone Motors?', r.data)
        self.assertIn(b'66661', r.data)
        # the name has to be typed
        for typed in ('', 'Kept Motors', 'Gone'):
            r = self.post(owner, page, {'confirm': typed})
            self.assertEqual(r.status_code, 400, typed)
            self.assertIn(b'Type the name of the store', r.data)
        self.assertEqual(saved(gone), [2, 1, 1])
        # a run that is working stops the delete
        was = web.recon_jobs.is_live
        db.execute("UPDATE runs SET status = 'running' WHERE store_id = %s",
                   (gone['id'],))
        web.recon_jobs.is_live = lambda job_id: True
        try:
            r = self.post(owner, page, {'confirm': 'Gone Motors'})
        finally:
            web.recon_jobs.is_live = was
        self.assertEqual(r.status_code, 400)
        self.assertIn(b'A run is working in that store now', r.data)
        self.assertEqual(saved(gone), [2, 1, 1])
        # a run that the server lost (marked running, not working) does
        # not: the store and all that it saved go
        r = self.post(owner, page, {'confirm': ' gone  motors '},
                      follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'Gone Motors was deleted.', r.data)
        self.assertNotIn(page.encode(), r.data)
        self.assertEqual(saved(gone), [0, 0, 0])
        self.assertEqual(db.one('SELECT count(*) AS n FROM run_files')['n'],
                         0)
        self.assertIsNone(db.one('SELECT 1 AS x FROM stores WHERE id = %s',
                                 (gone['id'],)))
        self.assertEqual(owner.get(page).status_code, 404)
        told = db.one("SELECT detail, user_email FROM audit_log WHERE "
                      "action = 'store_deleted' AND store_id = %s",
                      (gone['id'],))
        self.assertEqual(told['user_email'], OWNER)
        self.assertEqual(told['detail']['name'], 'Gone Motors')
        # the other store is as it was
        self.assertEqual(saved(kept), [1, 0, 0])
        # gus has no store left: signed out everywhere, and no new link
        r = gus.get('/reconcile')
        self.assertEqual(r.status_code, 302)
        self.assertIn('/login', r.headers['Location'])
        mailer.outbox.clear()
        self.post(self.client(), '/login', {'email': 'gus@gone.test'})
        self.assertEqual(mailer.outbox, [])
        # kim keeps the other store
        page = kim.get('/reconcile')
        self.assertEqual(page.status_code, 200)
        self.assertIn(b'Kept Motors', page.data)
        # and the dealer code is free for a new store
        self.make_store('Gone Motors', 'new@gone.test', '66661')

    def test_owner_pages_are_for_the_owner_only(self):
        self.make_store('Owner Test Store', 'amy@ots.test')
        amy = self.sign_in('amy@ots.test')
        self.assertEqual(amy.get('/owner').status_code, 404)
        self.assertEqual(self.post(amy, '/owner/stores', {
            'name': 'Sneaky', 'admin_email': 'amy@ots.test'}).status_code,
            404)
        owner = self.sign_in(OWNER)
        page = owner.get('/owner')
        self.assertEqual(page.status_code, 200)
        self.assertIn(b'Owner Test Store', page.data)
        # how much is stored, so a filling disk is seen before it is full
        self.assertRegex(page.get_data(as_text=True),
                         r'The database is using [\d,.]+ (KB|MB|GB)')
        from accounts import web
        self.assertEqual([web._size(n) for n in (0, 2048, 5 * 1024 ** 2,
                                                 3 * 1024 ** 3, None)],
                         ['0 KB', '2 KB', '5.0 MB', '3.00 GB', ''])
        # odd characters in a name are cleaned, never a server error
        r = self.post(owner, '/owner/stores', {
            'name': 'Odd\x00Name\tStore', 'admin_email': 'odd@odd.test'})
        self.assertEqual(r.status_code, 302)
        self.assertIn(b'Odd Name Store', owner.get('/owner').data)

    def test_errors_get_the_apps_own_page_with_a_way_on(self):
        self.make_store('Page Store', 'pam@page.test')
        pam = self.sign_in('pam@page.test')
        self.post(pam, '/users/add', {'email': 'ulf@page.test',
                                      'role': 'user'})
        ulf = self.sign_in('ulf@page.test')
        r = ulf.get('/users')                       # not an admin
        self.assertEqual(r.status_code, 403)
        self.assertIn(b'Only a store admin can do that', r.data)
        self.assertIn(b'href="/reconcile"', r.data)
        for path in ('/reconcile/', '/History', '/no/such/page'):
            r = ulf.get(path)
            self.assertEqual(r.status_code, 404, path)
            self.assertIn(b'That page does not exist', r.data)
        r = ulf.get('/users/add')                   # a form's address
        self.assertEqual(r.status_code, 405)
        self.assertIn(b'Go to the start', r.data)
        self.assertEqual(ulf.get('/api/nope').get_json()['error'][:9],
                         'That page')
        # a refused click is sent back to the page its form is on, not to
        # the form's own address
        for path, back in (('/users/add', '/users'),
                           ('/master/1/restore', '/history'),
                           ('/logout', '/')):
            r = pam.post(path, data={'csrf_token': 'stale'})
            self.assertEqual(r.status_code, 400, path)
            self.assertIn(f'href="{back}"'.encode(), r.data, path)
        # signed out, an unknown address is still just "no such page"
        self.assertEqual(self.client().get('/nope').status_code, 404)

    def test_users_page_rules(self):
        from accounts import core, mailer
        store = self.make_store('Users Store', 'ann@users.test')
        ann = self.sign_in('ann@users.test')
        mailer.outbox.clear()
        r = self.post(ann, '/users/add', {'email': 'bob@users.test',
                                          'role': 'user'})
        self.assertEqual(r.status_code, 302)
        self.assertEqual([m['to'] for m in mailer.outbox],
                         ['bob@users.test'])
        self.assertIn('Users Store', mailer.outbox[0]['subject'])
        bob_user = core.user_by_email('bob@users.test')
        # the invitation link signs Bob in, straight into his one store
        bob = self.client()
        self.use_link(bob, self.last_link('bob@users.test'))
        self.assertEqual(bob.get('/reconcile').status_code, 200)
        # a plain user cannot manage people
        self.assertEqual(bob.get('/users').status_code, 403)
        self.assertEqual(self.post(bob, '/users/add', {
            'email': 'eve@users.test', 'role': 'admin'}).status_code, 403)
        ann_user = core.user_by_email('ann@users.test')
        self.assertEqual(self.post(
            bob, f'/users/{ann_user["id"]}/remove').status_code, 403)
        self.assertEqual(self.post(bob, '/store/settings', {
            'name': 'Taken Over', 'threshold_days': '1'}).status_code, 403)
        # the only admin cannot be removed or demoted
        self.post(ann, f'/users/{ann_user["id"]}/remove')
        self.post(ann, '/users/add', {'email': 'ann@users.test',
                                      'role': 'user'})
        self.assertEqual(core.role_in(ann_user['id'], store['id']), 'admin')
        # an admin sets the threshold but not the store's name or code
        self.post(ann, '/store/settings', {
            'name': 'Renamed', 'dealer_code': '99999',
            'threshold_days': '45'})
        now = core.store_by_id(store['id'])
        self.assertEqual((now['name'], now['dealer_code'],
                          now['threshold_days']), ('Users Store', '12345',
                                                   45))
        # removing Bob signs him out on his very next click
        stolen = bob.get_cookie('pms_session').value
        self.assertEqual(self.post(
            ann, f'/users/{bob_user["id"]}/remove').status_code, 302)
        self.assertIsNone(core.role_in(bob_user['id'], store['id']))
        self.assertEqual(bob.get('/reconcile').status_code, 302)
        self.assertEqual(bob.get('/api/jobs/' + '0' * 32).status_code, 401)
        # and he cannot get a new link
        mailer.outbox.clear()
        self.post(self.client(), '/login', {'email': 'bob@users.test'})
        self.assertEqual(mailer.outbox, [])
        # added back later: his old browser session stays dead
        self.post(ann, '/users/add', {'email': 'bob@users.test',
                                      'role': 'user'})
        old = self.client()
        old.set_cookie('pms_session', stolen)
        self.assertEqual(old.get('/reconcile').status_code, 302)

    def test_small_slips_on_the_users_and_stores_pages(self):
        from accounts import core, mailer
        store = self.make_store('Slip Store', 'sid@slip.test', '45454')
        sid = self.sign_in('sid@slip.test')
        self.post(sid, '/users/add', {'email': 'tess@slip.test',
                                      'role': 'user'})
        # adding someone who is already on the store: said, nothing sent
        mailer.outbox.clear()
        r = self.post(sid, '/users/add', {'email': 'tess@slip.test',
                                          'role': 'user'},
                      follow_redirects=True)
        self.assertIn(b'is already on this store', r.data)
        self.assertEqual(mailer.outbox, [])
        # an admin giving up their own admin rights lands on a page they
        # can still use (the Users page is no longer theirs)
        self.post(sid, '/users/add', {'email': 'tess@slip.test',
                                      'role': 'admin'})
        r = self.post(sid, '/users/add', {'email': 'sid@slip.test',
                                          'role': 'user'})
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'You are now a user', r.data)
        self.assertEqual(core.role_in(
            core.user_by_email('sid@slip.test')['id'], store['id']), 'user')
        # the same store made twice (a double click) is refused
        owner = self.sign_in(OWNER)
        r = self.post(owner, '/owner/stores', {
            'name': 'slip store', 'dealer_code': '45454',
            'admin_email': 'sid@slip.test'}, follow_redirects=True)
        self.assertIn(b'There is already a store named', r.data)
        from accounts import db
        self.assertEqual(db.one(
            "SELECT count(*) AS n FROM stores WHERE lower(name) = "
            "'slip store'")['n'], 1)
        # a mistyped address is told apart from an empty one
        r = self.post(self.client(), '/login', {'email': 'sid@slip'})
        self.assertIn(b'does not look like an email address', r.data)

    def test_the_owner_never_lands_in_a_store_they_only_looked_into(self):
        mine = self.make_store('Owners Own Store', OWNER, '10101')
        theirs = self.make_store('Friends Store', 'fred@friend.test',
                                 '20202')
        owner = self.sign_in(OWNER)
        self.assertEqual(owner.get('/reconcile').status_code, 200)
        with owner.session_transaction() as s:
            self.assertEqual(s['sid'], mine['id'])
        # looks into the friend's store
        self.post(owner, f'/owner/open/{theirs["id"]}')
        page = owner.get('/reconcile')
        self.assertIn(b'Friends Store', page.data)
        self.assertIn(b'Viewing as owner', page.data)
        # the next sign-in is back in the owner's own store
        again = self.sign_in(OWNER)
        page = again.get('/reconcile')
        self.assertEqual(page.status_code, 200)
        self.assertIn(b'Owners Own Store', page.data)
        self.assertNotIn(b'Viewing as owner', page.data)

    def test_the_site_address_opens_the_weekly_run_when_signed_in(self):
        self.make_store('Home Store', 'hal@home.test')
        hal = self.sign_in('hal@home.test')
        r = hal.get('/')
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.headers['Location'].endswith('/reconcile'))
        quick = hal.get('/quick')
        self.assertEqual(quick.status_code, 200)
        self.assertIn(b'does not use or update your store', quick.data)
        self.assertIn(b'href="/quick"', hal.get('/reconcile').data)
        # signed out, Quick Check asks for sign-in and comes back to it
        r = self.client().get('/quick')
        self.assertEqual(r.status_code, 302)
        self.assertIn('next=/quick', r.headers['Location'].replace(
            '%2F', '/'))

    def test_link_sent_before_removal_stops_working(self):
        from accounts import core
        self.make_store('Removal Store', 'rae@removal.test')
        rae = self.sign_in('rae@removal.test')
        self.post(rae, '/users/add', {'email': 'sam@removal.test',
                                      'role': 'user'})
        link = self.last_link('sam@removal.test')
        sam = core.user_by_email('sam@removal.test')
        self.post(rae, f'/users/{sam["id"]}/remove')
        c = self.client()
        self.assertEqual(c.get(link).status_code, 410)
        self.assertEqual(self.post(c, link).status_code, 410)

    def test_one_person_two_stores_always_picks_for_themselves(self):
        a = self.make_store('Group Store A', 'gm@group.test', '11111')
        b = self.make_store('Group Store B', 'gm@group.test', '22222')
        gm = self.sign_in('gm@group.test')
        # on two stores with none picked yet: asked, never guessed
        for path in ('/reconcile', '/quick', '/history'):
            r = gm.get(path)
            self.assertEqual(r.status_code, 302, path)
            self.assertIn('/stores', r.headers['Location'])
        self.assertEqual(gm.get('/', follow_redirects=True).request.path,
                         '/stores')
        page = gm.get('/stores')
        self.assertIn(b'Group Store A', page.data)
        self.assertIn(b'Group Store B', page.data)
        self.assertIn(b'owner@example.com', page.data)      # who added them
        r = self.start_recon(gm, {'dc_text': 'x'})
        self.assertEqual(r.status_code, 302)                # no store, no run
        self.post(gm, '/switch-store', {'store_id': str(b['id'])})
        page = gm.get('/reconcile')
        self.assertEqual(page.status_code, 200)
        with gm.session_transaction() as s:
            self.assertEqual(s['sid'], b['id'])
        # a store the person is not on is ignored
        other = self.make_store('Not Mine', 'x@notmine.test')
        self.post(gm, '/switch-store', {'store_id': str(other['id'])})
        with gm.session_transaction() as s:
            self.assertEqual(s['sid'], b['id'])
        # the next sign-in lands in the store worked in last
        again = self.sign_in('gm@group.test')
        self.assertEqual(again.get('/reconcile').status_code, 200)
        with again.session_transaction() as s:
            self.assertEqual(s['sid'], b['id'])
        self.assertNotEqual(a['id'], b['id'])

    def test_being_added_elsewhere_never_moves_where_work_lands(self):
        from accounts import core
        mine = self.make_store('Victim Store', 'vic@victim.test', '11111')
        self.make_store('Rival Store', 'rival@rival.test', '22222')
        vic = self.sign_in('vic@victim.test')
        self.assertEqual(vic.get('/reconcile').status_code, 200)
        # the rival's admin adds the victim's email to the rival store
        rival = self.sign_in('rival@rival.test')
        self.post(rival, '/users/add', {'email': 'vic@victim.test',
                                        'role': 'user'})
        invite = self.last_link('vic@victim.test')
        # an already signed-in browser keeps working in its own store
        with vic.session_transaction() as s:
            self.assertEqual(s['sid'], mine['id'])
        # a fresh sign-in still lands in the victim's own store
        fresh = self.sign_in('vic@victim.test')
        self.assertEqual(fresh.get('/reconcile').status_code, 200)
        with fresh.session_transaction() as s:
            self.assertEqual(s['sid'], mine['id'])
        # even the rival's invitation link does not drop them in the
        # rival store: they are shown both stores and who added them
        self.post(rival, '/users/add', {'email': 'vic@victim.test',
                                        'role': 'user'})   # no new mail
        clicked = self.client()
        self.assertEqual(clicked.get(invite).status_code, 410)  # retired
        vic_user = core.user_by_email('vic@victim.test')
        self.post(rival, f'/users/{vic_user["id"]}/invite')
        invite = self.last_link('vic@victim.test')
        r = self.use_link(clicked, invite)
        self.assertIn('/stores', r.headers['Location'])
        page = clicked.get('/stores').data
        self.assertIn(b'rival@rival.test', page)
        # the rival's Users page shows only activity in the rival store
        users_page = rival.get('/users').get_data(as_text=True)
        self.assertIn('vic@victim.test', users_page)
        self.assertIn('Not yet', users_page)
        # removing the victim from the rival store does not touch their
        # own store, session or links
        self.post(fresh, '/logout')
        self.post(self.client(), '/login', {'email': 'vic@victim.test'})
        own_link = self.last_link('vic@victim.test')
        self.post(rival, f'/users/{vic_user["id"]}/remove')
        self.assertEqual(self.client().get(own_link).status_code, 200)
        self.assertEqual(vic.get('/reconcile').status_code, 200)

    def test_a_stale_tab_cannot_act_on_another_store(self):
        from accounts import core, storage
        a = self.make_store('Tab Store A', 'al@taba.test', '11111')
        b = self.make_store('Tab Store B', 'bo@tabb.test', '22222')
        owner = self.sign_in(OWNER)
        self.post(owner, f'/owner/open/{a["id"]}')
        token, page_store = self.guard(owner)       # tab 1 shows store A
        self.assertEqual(page_store, str(a['id']))
        self.post(owner, f'/owner/open/{b["id"]}')  # tab 2 opens store B
        stale = {'csrf_token': token, 'store_check': page_store}
        al = core.user_by_email('al@taba.test')
        # every click from the stale tab is refused, nothing changes
        r = owner.post('/users/add', data=dict(
            stale, email='al@taba.test', role='admin'))
        self.assertEqual(r.status_code, 409)
        self.assertIsNone(core.role_in(al['id'], b['id']))
        r = owner.post('/store/settings', data=dict(
            stale, name='Tab Store A', dealer_code='11111',
            threshold_days='60'))
        self.assertEqual(r.status_code, 409)
        self.assertEqual(core.store_by_id(b['id'])['name'], 'Tab Store B')
        r = owner.post('/api/reconcile', data={'dc_text': 'x'},
                       content_type='multipart/form-data',
                       headers={'X-CSRF-Token': token,
                                'X-Store-Id': page_store})
        self.assertEqual(r.status_code, 409)
        self.assertIsNone(storage.current_master(b['id']))
        # a post that names no store at all is refused too
        r = owner.post('/users/add', data={
            'csrf_token': token, 'email': 'al@taba.test', 'role': 'admin'})
        self.assertEqual(r.status_code, 409)
        # after a reload the page matches and the click goes through
        r = self.post(owner, '/users/add', {'email': 'new@tabb.test',
                                            'role': 'user'})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(core.role_in(
            core.user_by_email('new@tabb.test')['id'], b['id']), 'user')


class TestSavedWork(AccountsCase):
    def test_run_is_saved_and_next_run_starts_from_the_saved_master(self):
        from accounts import db, storage
        from recon import jobs
        store = self.make_store('Saved Store', 'sue@saved.test')
        sue = self.sign_in('sue@saved.test')
        self.assertIsNone(storage.current_master(store['id']))

        job1, job = self.first_run(sue)
        self.assertEqual(job['status'], 'done', job.get('error'))
        self.assertTrue(job['result']['saved'])
        self.assertEqual(job['result']['master_held'], '')
        self.assertNotIn('store_id', job)        # ownership stays server-side
        m1 = storage.current_master(store['id'])
        self.assertEqual((m1['row_count'], m1['unpaid_count']), (1, 1))
        self.assertEqual(float(m1['unpaid_amount']), 50.0)
        self.assertEqual(m1['created_email'], 'sue@saved.test')
        self.assertEqual(m1['source'], 'run')

        # second run: no workbook uploaded, so it starts from the saved one
        page = sue.get('/reconcile')
        self.assertIn(b"Saved on your store", page.data)
        job2, job = self.run_recon(sue, {
            'dc_text': 'C400000001\t11111111AA\t\t50.00\t\t\t',
            'asof': '2026-10-06', 'threshold': '60'})
        self.assertEqual(job['status'], 'done', job.get('error'))
        self.assertEqual(job['result']['inputs']['master_rows'], 1)
        run2 = storage.get_run(job2, store['id'])
        self.assertEqual(run2['master_in'], m1['id'])
        m2 = storage.current_master(store['id'])
        self.assertEqual(run2['master_out'], m2['id'])
        self.assertNotEqual(m1['id'], m2['id'])
        self.assertEqual(len(storage.master_versions(store['id'])), 2)
        self.assertEqual(run2['user_email'], 'sue@saved.test')
        self.assertEqual(run2['summary']['credit_request'], 0)  # asked 10/05

        # history lists both, and survives a server restart
        hist = sue.get('/history')
        self.assertEqual(hist.status_code, 200)
        self.assertEqual(hist.data.count(b'/reconcile#job='), 2)
        with jobs._lock:
            jobs._jobs.clear()
            jobs._meta.clear()
        shutil.rmtree(jobs.job_dir(job1), ignore_errors=True)
        again = sue.get(f'/api/jobs/{job1}').get_json()
        self.assertEqual(again['status'], 'done')
        self.assertEqual([x['ticket'] for x in
                          again['result']['credit_request']], ['C400000001'])
        names = {f['name']: f for f in again['result']['files']}
        dl = sue.get(f'/api/jobs/{job1}/files/Credit_Request.xlsx')
        self.assertEqual(dl.status_code, 200)
        from openpyxl import load_workbook
        ws = load_workbook(io.BytesIO(dl.data))['Credit Request']
        self.assertEqual([c.value for c in ws[2]],
                         [12345, '2026-07-01', 'C400000001'])
        zname = next(n for n, f in names.items() if f['kind'] == 'zip')
        z = sue.get(f'/api/jobs/{job1}/files/{zname}')
        self.assertEqual(z.status_code, 200)
        import zipfile
        inside = set(zipfile.ZipFile(io.BytesIO(z.data)).namelist())
        self.assertIn('Credit_Request.xlsx', inside)
        self.assertIn('Core_Returns_RECONCILED.xlsx', inside)
        self.assertEqual(sue.get(
            f'/api/jobs/{job1}/files/../app.py').status_code, 404)
        self.assertEqual(sue.get(
            f'/api/jobs/{job1}/files/nope.xlsx').status_code, 404)
        self.assertEqual(sue.get('/master/download').status_code, 200)
        self.assertEqual(
            db.one('SELECT count(*) AS n FROM runs WHERE store_id = %s',
                   (store['id'],))['n'], 2)

    def test_one_store_never_sees_another_stores_work(self):
        from accounts import storage
        a = self.make_store('Iso Store A', 'al@isoa.test')
        b = self.make_store('Iso Store B', 'bea@isob.test')
        al = self.sign_in('al@isoa.test')
        bea = self.sign_in('bea@isob.test')
        job_a, job = self.first_run(al, 'C400000011')
        self.assertEqual(job['status'], 'done', job.get('error'))
        job_b, job = self.first_run(bea, 'C400000022')
        self.assertEqual(job['status'], 'done', job.get('error'))
        ma = storage.current_master(a['id'])
        mb = storage.current_master(b['id'])

        # live (still in the server's memory)
        self.assertEqual(bea.get(f'/api/jobs/{job_a}').status_code, 404)
        self.assertEqual(bea.get(
            f'/api/jobs/{job_a}/files/Credit_Request.xlsx').status_code, 404)
        # from history
        from recon import jobs
        with jobs._lock:
            jobs._jobs.clear()
            jobs._meta.clear()
        self.assertEqual(bea.get(f'/api/jobs/{job_a}').status_code, 404)
        self.assertEqual(bea.get(
            f'/api/jobs/{job_a}/files/Credit_Request.xlsx').status_code, 404)
        self.assertEqual(bea.get(f'/api/jobs/{job_b}').status_code, 200)
        # masters
        self.assertEqual(bea.get(
            f'/master/{ma["id"]}/download').status_code, 404)
        self.assertEqual(self.post(
            bea, f'/master/{ma["id"]}/restore').status_code, 404)
        self.assertEqual(bea.get(
            f'/master/{mb["id"]}/download').status_code, 200)
        # history and the current master are the store's own
        hist = bea.get('/history').data
        self.assertIn(job_b.encode(), hist)
        self.assertNotIn(job_a.encode(), hist)
        from openpyxl import load_workbook
        ws = load_workbook(io.BytesIO(
            bea.get('/master/download').data))['Core Returns']
        self.assertEqual(ws['C2'].value, 'C400000022')
        # the owner sees a store only after opening it
        owner = self.sign_in(OWNER)
        self.post(owner, f'/owner/open/{a["id"]}')
        self.assertEqual(owner.get(f'/api/jobs/{job_a}').status_code, 200)
        self.assertEqual(owner.get(f'/api/jobs/{job_b}').status_code, 404)

    def test_a_failed_run_leaves_the_master_alone(self):
        from accounts import storage
        store = self.make_store('Fail Store', 'fay@fail.test')
        fay = self.sign_in('fay@fail.test')
        self.first_run(fay)
        before = storage.current_master(store['id'])
        job_id, job = self.run_recon(fay, {
            'master': (io.BytesIO(b'not a workbook'), 'broken.xlsx'),
            'asof': '2026-10-06'})
        self.assertEqual(job['status'], 'error')
        self.assertEqual(storage.current_master(store['id'])['id'],
                         before['id'])
        run = storage.get_run(job_id, store['id'])
        self.assertEqual(run['status'], 'error')
        self.assertIn(b'Stopped', fay.get('/history').data)

    def test_only_a_real_master_from_an_admin_replaces_the_saved_one(self):
        from accounts import storage
        from openpyxl import Workbook
        store = self.make_store('Swap Store', 'sal@swap.test')
        sal = self.sign_in('sal@swap.test')
        self.first_run(sal)
        before = storage.current_master(store['id'])
        self.post(sal, '/users/add', {'email': 'tom@swap.test',
                                      'role': 'user'})
        tom = self.sign_in('tom@swap.test')
        other = master_bytes([row(None, '2026-07-02 17:00.01', 'C400000077',
                                  '22222222AA', 75)])
        # a plain user cannot swap the saved master for another workbook
        self.assertNotIn(b'Use a different workbook',
                         tom.get('/reconcile').data)
        r = self.start_recon(tom, {
            'master': (io.BytesIO(other), 'Core_Returns_RECONCILED.xlsx'),
            'asof': '2026-10-06'})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(storage.current_master(store['id'])['id'],
                         before['id'])
        # but runs fine from the saved one
        _, job = self.run_recon(tom, {
            'dc_text': 'C400000001\t11111111AA\t\t50.00\t\t\t',
            'asof': '2026-10-06'})
        self.assertEqual(job['status'], 'done', job.get('error'))
        middle = storage.current_master(store['id'])
        # an admin picking one of the RESULT workbooks by mistake: refused
        wb = Workbook()
        ws = wb.active
        ws.title = 'Uncredited'
        ws.append(['Shipment ID', 'Control Ticket', 'Core Part #'])
        buf = io.BytesIO()
        wb.save(buf)
        _, job = self.run_recon(sal, {
            'master': (io.BytesIO(buf.getvalue()), 'Core_Recon_Report.xlsx'),
            'asof': '2026-10-06'})
        self.assertEqual(job['status'], 'error')
        self.assertIn('not a Core Returns master', job['error'])
        self.assertEqual(storage.current_master(store['id'])['id'],
                         middle['id'])
        # an admin with a real master: replaced
        _, job = self.run_recon(sal, {
            'master': (io.BytesIO(other), 'Core_Returns_RECONCILED.xlsx'),
            'asof': '2026-10-06'})
        self.assertEqual(job['status'], 'done', job.get('error'))
        self.assertNotEqual(storage.current_master(store['id'])['id'],
                            middle['id'])

    def test_first_master_can_be_the_stores_own_workbook_layout(self):
        from accounts import storage
        from openpyxl import Workbook
        store = self.make_store('Layout Store', 'lee@layout.test')
        lee = self.sign_in('lee@layout.test')
        wb = Workbook()
        ws = wb.active                       # tab is "Sheet", not Core Returns
        ws.append(['Credit Memo #', 'Shipment ID', 'Control Ticket',
                   'Core Part #', 'Claim #', 'Credit Amount'])
        ws.append([None, '2026-09-01 17:00.01', 'C400000051', '11111111AA',
                   None, 50])
        buf = io.BytesIO()
        wb.save(buf)
        _, job = self.run_recon(lee, {
            'master': (io.BytesIO(buf.getvalue()), 'our cores.xlsx'),
            'asof': '2026-10-06'})
        self.assertEqual(job['status'], 'done', job.get('error'))
        self.assertEqual(storage.current_master(store['id'])['row_count'], 1)

    def test_a_much_smaller_master_is_held_for_an_admin_to_confirm(self):
        from accounts import storage
        store = self.make_store('Shrink Store', 'shay@shrink.test')
        shay = self.sign_in('shay@shrink.test')
        _, job = self.run_recon(shay, {
            'master': (io.BytesIO(master_bytes(many_rows(20))),
                       'Core_Returns_RECONCILED.xlsx'),
            'asof': '2026-10-06'})
        self.assertEqual(job['status'], 'done', job.get('error'))
        big = storage.current_master(store['id'])
        self.assertEqual(big['row_count'], 20)
        # the wrong (tiny) workbook
        job_id, job = self.run_recon(shay, {
            'master': (io.BytesIO(master_bytes(many_rows(2, 500))),
                       'Core_Returns_RECONCILED.xlsx'),
            'asof': '2026-10-06'})
        self.assertEqual(job['status'], 'done', job.get('error'))
        self.assertTrue(job['result']['saved'])
        self.assertIn('far fewer rows', job['result']['master_held'])
        self.assertEqual(storage.current_master(store['id'])['id'],
                         big['id'])                      # untouched
        held = storage.master_versions(store['id'])[0]
        self.assertFalse(held['is_current'])
        self.assertIn('far fewer rows', held['held_reason'])
        page = shay.get('/history').data
        self.assertIn(b'Not made current', page)
        self.assertIn(b'Master held', page)
        # the warning is still there when the run is opened from History
        from recon import jobs
        with jobs._lock:
            jobs._jobs.clear()
            jobs._meta.clear()
        again = shay.get(f'/api/jobs/{job_id}').get_json()['result']
        self.assertIn('far fewer rows', again['master_held'])
        self.assertTrue(again['from_history'])
        # the admin decides it was right after all
        self.assertEqual(self.post(
            shay, f'/master/{held["id"]}/restore').status_code, 302)
        self.assertEqual(storage.current_master(store['id'])['row_count'],
                         2)

    def test_a_run_never_overwrites_a_master_changed_under_it(self):
        from accounts import storage
        from recon import jobs
        store = self.make_store('Race Store', 'ray@race.test')
        ray = self.sign_in('ray@race.test')
        job1, _ = self.first_run(ray, 'C400000041')
        v1 = storage.current_master(store['id'])
        self.run_recon(ray, {
            'dc_text': 'C400000041\t11111111AA\t\t50.00\t\t\t',
            'asof': '2026-10-06'})
        v2 = storage.current_master(store['id'])
        # a run started from v2 is still working when an admin puts v1
        # back; when the run finishes it must not undo that
        actor = {'id': None, 'email': 'ray@race.test'}
        storage.begin_run('b' * 32, store['id'], actor, 'reconcile')
        v3 = storage.restore_master(store['id'], v1['id'], actor)
        out = os.path.join(jobs.job_dir(job1), 'out')
        result = ray.get(f'/api/jobs/{job1}').get_json()['result']
        saved = storage.finish_run('b' * 32, store['id'], actor, result,
                                   out, master_in=v2['id'])
        self.assertIn('changed by someone else', saved['held'])
        self.assertEqual(storage.current_master(store['id'])['id'], v3)
        kept = storage.master_file(store['id'], saved['master_out'])[0]
        self.assertFalse(kept['is_current'])
        # a plain user's uploaded workbook that finishes after a master
        # was saved (the first-run race) is held as well
        storage.begin_run('c' * 32, store['id'], actor, 'reconcile')
        saved = storage.finish_run('c' * 32, store['id'], actor, result,
                                   out, master_in=None, may_replace=False)
        self.assertIn('only a store admin', saved['held'])
        self.assertEqual(storage.current_master(store['id'])['id'], v3)
        # an admin's uploaded workbook: the admin pressed Run looking at
        # v2, but v3 is current by the time it saves. Held, not swapped in
        # over a master the admin never saw.
        storage.begin_run('d' * 32, store['id'], actor, 'reconcile')
        saved = storage.finish_run('d' * 32, store['id'], actor, result,
                                   out, master_in=None, may_replace=True,
                                   seen_master=v2['id'])
        self.assertIn('changed by someone else', saved['held'])
        self.assertEqual(storage.current_master(store['id'])['id'], v3)
        # ...and when nothing changed since they pressed Run, it replaces
        storage.begin_run('e' * 32, store['id'], actor, 'reconcile')
        saved = storage.finish_run('e' * 32, store['id'], actor, result,
                                   out, master_in=None, may_replace=True,
                                   seen_master=v3)
        self.assertEqual(saved['held'], '')
        self.assertEqual(storage.current_master(store['id'])['id'],
                         saved['master_out'])

    def test_an_admin_can_put_an_earlier_master_back(self):
        from accounts import storage
        store = self.make_store('Undo Store', 'una@undo.test')
        una = self.sign_in('una@undo.test')
        self.first_run(una, 'C400000031')
        v1 = storage.current_master(store['id'])
        # a run with the wrong workbook replaces the master...
        wrong = master_bytes([row(None, '2026-07-02 17:00.01', 'C400000099',
                                  '22222222AA', 75)])
        self.run_recon(una, {
            'master': (io.BytesIO(wrong), 'Core_Returns_RECONCILED.xlsx'),
            'asof': '2026-10-06'})
        v2 = storage.current_master(store['id'])
        self.assertNotEqual(v1['id'], v2['id'])
        # ...a plain user cannot undo it, an admin can
        self.post(una, '/users/add', {'email': 'uri@undo.test',
                                      'role': 'user'})
        uri = self.sign_in('uri@undo.test')
        self.assertEqual(self.post(
            uri, f'/master/{v1["id"]}/restore').status_code, 403)
        self.assertEqual(self.post(
            una, f'/master/{v1["id"]}/restore').status_code, 302)
        v3 = storage.current_master(store['id'])
        self.assertEqual(v3['source'], 'restore')
        _, now = storage.master_file(store['id'])
        _, first = storage.master_file(store['id'], v1['id'])
        self.assertEqual(now, first)
        self.assertEqual(len(storage.master_versions(store['id'])), 3)

    def test_a_run_that_cannot_be_saved_says_so(self):
        from accounts import storage
        store = self.make_store('Unsaved Store', 'uma@unsaved.test')
        uma = self.sign_in('uma@unsaved.test')
        real = storage.finish_run

        def boom(*a, **k):
            raise RuntimeError('database went away')

        storage.finish_run = boom
        try:
            job_id, job = self.first_run(uma)
        finally:
            storage.finish_run = real
        self.assertEqual(job['status'], 'done')
        self.assertFalse(job['result']['saved'])
        self.assertIn('could not be saved', job['result']['warnings'][0])
        self.assertIsNone(storage.current_master(store['id']))
        self.assertEqual(storage.get_run(job_id, store['id'])['status'],
                         'error')                    # not stuck on Running

    def test_two_first_uploads_in_a_row_do_not_overwrite_each_other(self):
        from accounts import storage
        from recon import jobs
        store = self.make_store('Queue Store', 'quy@queue.test')
        quy = self.sign_in('quy@queue.test')
        # both submitted while the store has no master yet; the second is
        # held because a master was saved while it waited its turn
        jobs._one_at_a_time.acquire()           # nothing starts yet
        try:
            a = self.start_recon(quy, {
                'master': (io.BytesIO(master_bytes(many_rows(12))),
                           'Core_Returns_RECONCILED.xlsx'),
                'asof': '2026-10-06'})
            b = self.start_recon(quy, {
                'master': (io.BytesIO(master_bytes(many_rows(15, 300))),
                           'other.xlsx'), 'asof': '2026-10-06'})
        finally:
            jobs._one_at_a_time.release()
        results = []
        for r in (a, b):
            self.assertEqual(r.status_code, 200)
            job_id = r.get_json()['job_id']
            for _ in range(400):
                job = quy.get(f'/api/jobs/{job_id}').get_json()
                if job['status'] in ('done', 'error'):
                    break
                time.sleep(0.05)
            self.assertEqual(job['status'], 'done', job.get('error'))
            results.append(job['result'])
        held = [r['master_held'] for r in results]
        self.assertEqual(sum(1 for h in held if h), 1, held)
        self.assertIn('changed by someone else', ''.join(held))
        self.assertIn(storage.current_master(store['id'])['row_count'],
                      (12, 15))
        self.assertEqual(len(storage.master_versions(store['id'])), 2)

    def test_nothing_is_left_waiting_when_a_submission_is_refused(self):
        import app as webapp
        from recon import jobs
        self.make_store('Tidy Store', 'ty@tidy.test')
        ty = self.sign_in('ty@tidy.test')
        with jobs._lock:
            before = set(jobs._jobs)
        folders = set(os.listdir(jobs.JOB_ROOT))
        # more than the server accepts at once: a plain answer, no job
        limit = webapp.app.config['MAX_CONTENT_LENGTH']
        webapp.app.config['MAX_CONTENT_LENGTH'] = 200000
        try:
            r = self.start_recon(ty, {
                'memos': (io.BytesIO(b'x' * 300000), 'big.pdf')})
        finally:
            webapp.app.config['MAX_CONTENT_LENGTH'] = limit
        self.assertEqual(r.status_code, 413)
        self.assertIn('too large to send together', r.get_json()['error'])
        # the server cannot keep the upload (disk full, say)
        real = webapp._save_uploads

        def full(*a, **k):
            raise OSError(28, 'No space left on device')

        webapp._save_uploads = full
        try:
            webapp.app.config['PROPAGATE_EXCEPTIONS'] = False
            r = self.start_recon(ty, {'dc_text': 'x'})
        finally:
            webapp._save_uploads = real
            webapp.app.config.pop('PROPAGATE_EXCEPTIONS', None)
        self.assertEqual(r.status_code, 500)
        for data in ({}, {'master': (io.BytesIO(b'x'), 'm.pdf')}):
            self.assertEqual(self.start_recon(ty, data).status_code, 400)
        with jobs._lock:
            self.assertEqual(set(jobs._jobs), before)
        self.assertEqual(set(os.listdir(jobs.JOB_ROOT)), folders)

    def test_a_run_left_marked_running_shows_as_stopped(self):
        from accounts import storage
        store = self.make_store('Stale Store', 'stu@stale.test')
        stu = self.sign_in('stu@stale.test')
        storage.begin_run('f' * 32, store['id'],
                          {'id': None, 'email': 'stu@stale.test'},
                          'reconcile')
        page = stu.get('/history').get_data(as_text=True)
        self.assertIn('Stopped', page)
        self.assertIn('stopped before it could be saved', page)
        self.assertNotIn('In progress', page)

    def test_a_large_result_file_is_kept_in_pieces_and_comes_back_whole(self):
        from accounts import db, storage
        from recon import jobs
        store = self.make_store('Pieces Store', 'pip@pieces.test')
        pip = self.sign_in('pip@pieces.test')
        job_id, job = self.first_run(pip)
        self.assertEqual(job['status'], 'done', job.get('error'))
        actor = {'id': None, 'email': 'pip@pieces.test'}
        out = os.path.join(jobs.job_dir(job_id), 'out')
        # a scan-heavy PDF: bigger than one piece, not an even multiple
        big = os.urandom(storage.PART_BYTES * 2 + 12345)
        huge = b'x' * 2048
        with open(os.path.join(out, 'Credit_Request.pdf'), 'wb') as fh:
            fh.write(big)
        with open(os.path.join(out, 'Huge.pdf'), 'wb') as fh:
            fh.write(huge)
        result = dict(job['result'])
        result['files'] = [f for f in result['files']
                           if f['kind'] != 'zip'] + [
            {'name': 'Credit_Request.pdf', 'label': 'Signed pages',
             'kind': 'request', 'size': len(big)},
            {'name': 'Huge.pdf', 'label': 'Too big', 'kind': 'request',
             'size': len(huge)},
            {'name': 'All.zip', 'label': 'Everything', 'kind': 'zip',
             'size': 1}]
        limit = storage.MAX_FILE_BYTES
        storage.MAX_FILE_BYTES = storage.PART_BYTES * 3
        try:
            with open(os.path.join(out, 'Huge.pdf'), 'wb') as fh:
                fh.write(b'x' * (storage.MAX_FILE_BYTES + 1))
            storage.begin_run('9' * 32, store['id'], actor, 'reconcile')
            storage.finish_run('9' * 32, store['id'], actor, result, out,
                               master_in=storage.current_master(
                                   store['id'])['id'])
        finally:
            storage.MAX_FILE_BYTES = limit
        self.assertEqual(db.one(
            "SELECT count(*) AS n FROM run_file_parts WHERE run_id = %s "
            "AND name = 'Credit_Request.pdf'", ('9' * 32,))['n'], 3)
        self.assertEqual(db.one(
            'SELECT max(octet_length(content)) AS n FROM run_file_parts '
            'WHERE run_id = %s', ('9' * 32,))['n'], storage.PART_BYTES)
        got = pip.get('/api/jobs/' + '9' * 32 + '/files/Credit_Request.pdf')
        self.assertEqual(got.status_code, 200)
        self.assertEqual(got.data, big)
        import zipfile
        z = zipfile.ZipFile(io.BytesIO(pip.get(
            '/api/jobs/' + '9' * 32 + '/files/All.zip').data))
        self.assertEqual(z.read('Credit_Request.pdf'), big)
        # the one over the limit is not kept, and the saved run says so
        saved = pip.get('/api/jobs/' + '9' * 32).get_json()['result']
        kept = {f['name']: f.get('kept') for f in saved['files']}
        self.assertIs(kept['Huge.pdf'], False)
        self.assertIs(kept['Credit_Request.pdf'], True)
        self.assertEqual(pip.get(
            '/api/jobs/' + '9' * 32 + '/files/Huge.pdf').status_code, 404)
        # what the Stores page adds up is the real size
        owner = self.sign_in(OWNER)
        self.assertIn(b'MB', owner.get('/owner').data)

    def test_quick_check_is_logged(self):
        from accounts import storage
        store = self.make_store('Quick Store', 'quin@quick.test')
        storage.log_quick_check('a' * 32, store['id'],
                                {'id': None, 'email': 'quin@quick.test'},
                                {'shipper_count': 25, 'credited_count': 4,
                                 'unclaimed_count': 21,
                                 'unclaimed_total': 4740.0})
        quin = self.sign_in('quin@quick.test')
        page = quin.get('/history').data
        self.assertIn(b'Quick Check', page)
        self.assertIn(b'$4,740.00', page)
        # a quick check is a history line only: nothing to open
        self.assertEqual(quin.get('/api/jobs/' + 'a' * 32).status_code, 404)


class TestWhenTheDatabaseIsAway(AccountsCase):
    """A database that cannot be reached gives a plain "back in a minute"
    answer, quickly, and everything works again by itself afterwards."""

    def away(self):
        """Make every database call fail, as an outage does. Returns the
        function that ends the outage."""
        from accounts import db
        real = db._checkout

        def gone():
            raise db.Unavailable('The database could not be reached.')

        db._checkout = gone
        return lambda: setattr(db, '_checkout', real)

    def test_pages_say_back_in_a_minute_and_recover(self):
        self.make_store('Away Store', 'ava@away.test')
        ava = self.sign_in('ava@away.test')
        token, store = self.guard(ava)
        back = self.away()
        try:
            page = ava.get('/reconcile')
            self.assertEqual(page.status_code, 503)
            self.assertIn(b'Back in a minute', page.data)
            self.assertNotIn(b'Traceback', page.data)
            self.assertEqual(page.headers['Retry-After'], '30')
            api = ava.get('/api/jobs/' + '0' * 32)
            self.assertEqual(api.status_code, 503)
            self.assertIn('Wait a minute', api.get_json()['error'])
            for path in ('/history', '/users', '/master/download'):
                self.assertEqual(ava.get(path).status_code, 503, path)
            r = ava.post('/users/add', data={
                'csrf_token': token, 'store_check': store,
                'email': 'new@away.test', 'role': 'user'})
            self.assertEqual(r.status_code, 503)
            # the server itself still says it is alive
            self.assertEqual(ava.get('/healthz').status_code, 200)
            # a visitor who is not signed in still gets the front page
            self.assertEqual(self.client().get('/').status_code, 200)
        finally:
            back()
        # nothing to do afterwards: still signed in, same store
        page = ava.get('/reconcile')
        self.assertEqual(page.status_code, 200)
        self.assertIn(b'Away Store', page.data)

    def test_a_run_submitted_during_an_outage_leaves_nothing_behind(self):
        from accounts import db, storage
        from recon import jobs
        self.make_store('Hiccup Store', 'hy@hiccup.test')
        hy = self.sign_in('hy@hiccup.test')
        token, store = self.guard(hy)
        real = storage.current_master

        def gone(store_id):
            raise db.Unavailable('The database could not be reached.')

        with jobs._lock:
            before = set(jobs._jobs)
        storage.current_master = gone
        try:
            r = hy.post('/api/reconcile', data={'dc_text': 'x'},
                        content_type='multipart/form-data',
                        headers={'X-CSRF-Token': token, 'X-Store-Id': store})
        finally:
            storage.current_master = real
        self.assertEqual(r.status_code, 503)
        self.assertIn('error', r.get_json())
        with jobs._lock:
            self.assertEqual(set(jobs._jobs), before)

    def test_a_run_that_ends_during_a_short_outage_is_still_saved(self):
        import app as webapp
        from accounts import db, storage
        store = self.make_store('Patient Store', 'pia@patient.test')
        pia = self.sign_in('pia@patient.test')
        real, waits = storage.finish_run, webapp.SAVE_RETRY_WAITS
        calls = []

        def away_twice(*a, **k):
            calls.append(1)
            if len(calls) <= 2:
                raise db.Unavailable('The database could not be reached.')
            return real(*a, **k)

        def away_for_good(*a, **k):
            calls.append(1)
            raise db.Unavailable('The database could not be reached.')

        webapp.SAVE_RETRY_WAITS = (0, 0, 0)
        try:
            storage.finish_run = away_twice
            job_id, job = self.first_run(pia)
            self.assertEqual(job['status'], 'done', job.get('error'))
            self.assertTrue(job['result']['saved'])
            self.assertEqual(len(calls), 3)
            self.assertEqual(storage.current_master(store['id'])['row_count'],
                             1)
            # an outage that outlasts the wait: the run says so, files stay
            del calls[:]
            storage.finish_run = away_for_good
            job_id, job = self.run_recon(pia, {
                'dc_text': 'C400000001\t11111111AA\t\t50.00\t\t\t',
                'asof': '2026-10-06'})
            self.assertEqual(job['status'], 'done')
            self.assertFalse(job['result']['saved'])
            self.assertIn('could not be saved', job['result']['warnings'][0])
            self.assertEqual(len(calls), 4)
        finally:
            storage.finish_run, webapp.SAVE_RETRY_WAITS = real, waits
        self.assertEqual(len(storage.master_versions(store['id'])), 1)

    def test_a_database_error_in_the_middle_of_a_page_is_a_503_too(self):
        import psycopg
        from accounts import storage
        self.make_store('Midway Store', 'mia@midway.test')
        mia = self.sign_in('mia@midway.test')
        real = storage.list_runs

        def dropped(*a, **k):
            raise psycopg.OperationalError('server closed the connection '
                                           'unexpectedly')

        storage.list_runs = dropped
        try:
            page = mia.get('/history')
        finally:
            storage.list_runs = real
        self.assertEqual(page.status_code, 503)
        self.assertIn(b'Back in a minute', page.data)
        self.assertEqual(mia.get('/history').status_code, 200)

    def test_a_server_that_starts_without_its_database_does_not_crash(self):
        from accounts import web
        key, booted = self.app.secret_key, web._booted
        self.app.secret_key = None          # as on a fresh start
        web._booted = None
        back = self.away()
        try:
            c = self.client()
            for path in ('/', '/login', '/reconcile'):
                page = c.get(path)
                self.assertEqual(page.status_code, 503, path)
                self.assertIn(b'Back in a minute', page.data)
            self.assertEqual(c.get('/api/jobs/' + '0' * 32).status_code, 503)
            self.assertEqual(c.post('/login', data={
                'email': 'x@nowhere.test'}).status_code, 503)
            self.assertEqual(c.get('/healthz').status_code, 200)
            # the health check never waits on the database, even now
            from accounts import db
            asked = []
            real = db._checkout
            db._checkout = lambda: asked.append(1) or real()
            try:
                for _ in range(3):
                    self.assertEqual(c.get('/healthz').status_code, 200)
            finally:
                db._checkout = real
            self.assertEqual(asked, [])
        finally:
            back()
            self.app.secret_key, web._booted = key, booted
        self.assertEqual(self.client().get('/').status_code, 200)

    def test_an_unreachable_address_fails_fast_and_keeps_its_secret(self):
        import contextlib
        from accounts import db
        good = os.environ['DATABASE_URL']
        saved = (db.WAIT_SECONDS, db.RETRY_AFTER)
        db.WAIT_SECONDS, db.RETRY_AFTER = 1, 60
        out = io.StringIO()
        try:
            # nothing listens on port 1
            os.environ['DATABASE_URL'] = \
                'postgresql://nobody:hunter2secret@127.0.0.1:1/none'
            c = self.client()
            with contextlib.redirect_stdout(out), \
                    self.assertLogs('psycopg.pool', 'WARNING') as logs:
                t0 = time.monotonic()
                self.assertEqual(c.get('/reconcile').status_code, 503)
                first = time.monotonic() - t0
                # while it is known to be down nobody waits in line
                t0 = time.monotonic()
                for _ in range(5):
                    self.assertEqual(c.get('/reconcile').status_code, 503)
                rest = time.monotonic() - t0
            self.assertLess(first, 4)
            self.assertLess(rest, 0.5)
            said = out.getvalue() + ' '.join(r.getMessage()
                                             for r in logs.records)
            self.assertIn('cannot reach the database', said)
            self.assertNotIn('hunter2secret', said)
        finally:
            db.WAIT_SECONDS, db.RETRY_AFTER = saved
            os.environ['DATABASE_URL'] = good
            db.close()
        self.assertEqual(self.client().get('/').status_code, 200)

    def test_only_one_request_at_a_time_retries_after_an_outage(self):
        from accounts import db
        self.assertEqual(db.one('SELECT 1 AS x')['x'], 1)
        try:
            db._down, db._down_until = True, time.monotonic() + 60
            with self.assertRaises(db.Unavailable):     # too soon to retry
                db.one('SELECT 1 AS x')
            db._down_until = 0.0
            db._probe.acquire()             # someone else is retrying now
            try:
                with self.assertRaises(db.Unavailable):
                    db.one('SELECT 1 AS x')
            finally:
                db._probe.release()
            # our turn: it is back, and the outage is over for everyone
            self.assertEqual(db.one('SELECT 1 AS x')['x'], 1)
            self.assertFalse(db._down)
            self.assertFalse(db._probe.locked())
        finally:
            db._down, db._down_until = False, 0.0

    def test_a_malformed_address_is_refused_without_being_echoed(self):
        from accounts import db
        for bad in ('PGPASSWORD=hunter2secret psql -h dpg-x-a -U u db',
                    'postgres//u:hunter2secret@dpg-x-a/db',
                    'dpg-x-a', 'https://u:hunter2secret@example.com/db',
                    'postgresql://u:hunter2secret@dpg-x-a/db with a space',
                    'postgresql://u:hunter2secret@[::1/db'):
            with self.assertRaises(db.Unavailable) as caught:
                db.check_url(bad)
            self.assertNotIn('hunter2secret', str(caught.exception), bad)
            self.assertIn('postgresql://', str(caught.exception))
        for good in ('postgresql://u:p@dpg-x-a/db',
                     'postgres://u:p@dpg-x-a.oregon-postgres.render.com/db'):
            db.check_url(good)
        # whatever the driver says about a failed connection, the address
        # and its password are taken out before it is logged
        u = 'postgresql://u:hunter2secret@dpg-x-a/db'
        line = db.brief(Exception(f'bad thing at {u} (hunter2secret)'), u)
        self.assertNotIn('hunter2secret', line)


class TestStartUp(AccountsCase):
    """The check that runs before the web server starts."""

    def check(self, **env):
        """Run the start-up check with these settings. Returns (exit
        code, everything it printed)."""
        import contextlib
        from accounts import db, preflight
        keys = ('DATABASE_URL', 'OWNER_EMAIL', 'MAIL_BACKEND',
                'RESEND_API_KEY', 'APP_BASE_URL', 'RENDER_EXTERNAL_URL',
                'RENDER')
        saved = {k: os.environ.get(k) for k in keys}
        wait = preflight.WAIT_SECONDS
        preflight.WAIT_SECONDS = 0
        for k, v in env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        out = io.StringIO()
        try:
            db.close()
            with contextlib.redirect_stdout(out):
                code = preflight.main()
        finally:
            preflight.WAIT_SECONDS = wait
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            db.close()
        return code, out.getvalue()

    def empty_database(self):
        import psycopg
        from accounts import db
        db.close()
        with psycopg.connect(TEST_DB, autocommit=True) as conn:
            conn.execute('DROP SCHEMA public CASCADE')
            conn.execute('CREATE SCHEMA public')

    def test_accounts_off_starts_without_touching_anything(self):
        code, said = self.check(DATABASE_URL=None)
        self.assertEqual(code, 0)
        self.assertIn('Accounts are off', said)

    def test_a_wrong_address_stops_the_start_with_a_plain_reason(self):
        cases = {
            'PGPASSWORD=hunter2secret psql -h dpg-x-a -U u db':
                'not a database address',
            'postgresql://nobody:hunter2secret@127.0.0.1:1/none':
                'refused the connection',
            'postgresql://nobody:hunter2secret@no-such-host.invalid/none':
                'was not found',
            TEST_DB.rsplit('/', 1)[0] + '/no_such_database_here':
                'does not exist',
        }
        for address, expected in cases.items():
            code, said = self.check(DATABASE_URL=address)
            self.assertEqual(code, 1, said)
            self.assertIn('NOT STARTED', said)
            self.assertIn(expected, said)
            self.assertNotIn('hunter2secret', said)

    def test_accounts_are_not_turned_on_until_sign_in_can_work(self):
        from accounts import core, db
        self.empty_database()
        # a brand-new database with the set-up half done: refuse to start
        code, said = self.check(OWNER_EMAIL=None)
        self.assertEqual(code, 1, said)
        self.assertIn('OWNER_EMAIL', said)
        code, said = self.check(MAIL_BACKEND=None, RESEND_API_KEY=None)
        self.assertEqual(code, 1, said)
        self.assertIn('RESEND_API_KEY', said)
        self.assertNotIn('OWNER_EMAIL', said)
        code, said = self.check(MAIL_BACKEND=None, APP_BASE_URL=None,
                                RESEND_API_KEY='test-key-never-used')
        self.assertEqual(code, 1, said)
        self.assertIn('APP_BASE_URL', said)
        self.assertNotIn('test-key-never-used', said)
        # set, but not usable: an address that is not one, or the
        # development mail setting left on the live host
        for bad in ('partsmanagersolutions.com', 'yes',
                    'https://partsmanagersolutions.com/app?x=1'):
            code, said = self.check(APP_BASE_URL=bad)
            self.assertEqual(code, 1, bad)
            self.assertIn('APP_BASE_URL has to be', said)
        code, said = self.check(RENDER='true')          # MAIL_BACKEND=memory
        self.assertEqual(code, 1, said)
        self.assertIn('Remove MAIL_BACKEND', said)
        # everything set: starts, the tables are there, the owner exists
        code, said = self.check()
        self.assertEqual(code, 0, said)
        self.assertIn('Ready.', said)
        self.assertTrue(core.user_by_email(OWNER)['is_owner'])
        self.assertEqual(
            db.one("SELECT value FROM settings WHERE key = "
                   "'schema_version'")['value'], db.schema_version())
        # once somebody has signed in, a missing setting is a warning:
        # people who are signed in keep working
        self.sign_in(OWNER)
        code, said = self.check(MAIL_BACKEND=None, RESEND_API_KEY=None)
        self.assertEqual(code, 0, said)
        self.assertIn('WARNING: RESEND_API_KEY', said)

    def test_a_held_table_lock_delays_the_start_but_never_hangs_it(self):
        import threading
        import psycopg
        from accounts import db, preflight
        self.empty_database()
        code, said = self.check()               # tables in place
        self.assertEqual(code, 0, said)
        # something else (a report tool, a backup) holds a table, and the
        # tables need changing: the check waits its few seconds per try,
        # gives up cleanly, and goes through once the lock is gone
        db.execute("UPDATE settings SET value = 'older' WHERE key = "
                   "'schema_version'")
        db.close()
        holder = psycopg.connect(TEST_DB)
        holder.execute('LOCK TABLE users IN ACCESS SHARE MODE')
        saved = (preflight.PAUSE_SECONDS,)
        preflight.PAUSE_SECONDS = 0.2
        try:
            t0 = time.monotonic()
            code, said = self.check()
            took = time.monotonic() - t0
            self.assertEqual(code, 1, said)
            self.assertIn('could not be made ready', said)
            self.assertLess(took, 20)
            threading.Timer(1.0, holder.rollback).start()
            wait = preflight.WAIT_SECONDS
            real_check = self.check

            def patient():
                # like self.check, but with time to wait the lock out
                import contextlib
                out = io.StringIO()
                preflight.WAIT_SECONDS = 30
                try:
                    db.close()
                    with contextlib.redirect_stdout(out):
                        return preflight.main(), out.getvalue()
                finally:
                    preflight.WAIT_SECONDS = wait
                    db.close()

            code, said = patient()
            self.assertEqual(code, 0, said)
        finally:
            preflight.PAUSE_SECONDS, = saved
            holder.close()
        self.assertEqual(
            db.one("SELECT value FROM settings WHERE key = "
                   "'schema_version'")['value'], db.schema_version())

    def test_tables_are_set_up_once_and_older_ones_are_brought_up(self):
        from accounts import db
        p = db.pool()
        self.assertFalse(db._apply_schema(p, 10))    # nothing to do
        # tables left by an earlier build: columns added since are missing
        with db.connect() as conn:
            conn.execute('ALTER TABLE users DROP COLUMN last_store_id')
            conn.execute('ALTER TABLE memberships DROP COLUMN added_email')
            conn.execute('ALTER TABLE memberships DROP COLUMN last_used_at')
            conn.execute('ALTER TABLE login_tokens DROP COLUMN store_id')
            conn.execute('ALTER TABLE masters DROP COLUMN held_reason')
            conn.execute("DELETE FROM settings WHERE key = 'schema_version'")
        db.close()
        self.make_store('Upgrade Store', 'uli@upgrade.test')
        uli = self.sign_in('uli@upgrade.test')
        self.assertEqual(uli.get('/users').status_code, 200)
        self.assertEqual(uli.get('/history').status_code, 200)
        _, job = self.first_run(uli)
        self.assertEqual(job['status'], 'done', job.get('error'))


if __name__ == '__main__':
    unittest.main()
