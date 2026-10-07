"""Tests for the Full Reconciliation engine.

All data here is made up (fake tickets, parts and amounts) so nothing from
a real dealer ever lives in the repo.

Run:  python -m unittest discover -s tests -v
"""
import datetime
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(
    __file__))))

from openpyxl import load_workbook  # noqa: E402

from recon import engine, master as M  # noqa: E402
from recon.common import ocr_ticket  # noqa: E402
from recon.dealerconnect import read_dc_text  # noqa: E402
from recon.memo import parse_memo_text  # noqa: E402
from recon.shipper import (_shipment_from_header, ocr_part,  # noqa: E402
                           parse_line)

MEMO_HEADER = """FCA US LLC
               MOPAR WEEKLY GLOBAL CORE RETURN CREDIT MEMO
                                              BILL TO: 12345000
CREDIT MEMO NUMBER: 03181000CC00900001        ANYTOWN
CREDIT MEMO DATE : OCTOBER 03, 2026           TX 70000
PAGE              :   {page}                       SD NO: 01
LINE PART     DESCRIPTN QTY UNIT         GROSS     EXT   D   DISCNT     NET    P R
"""

MEMO_TEXT = MEMO_HEADER.format(page=1) + """
0318100-1000001 ORD#: GCRS    O/T:M
  1 U1111111AB TRANS       1 2500.00- 2500.00- 99     .0       .00    2500.00- N 1
     REFERENCE/CONTROL NUMBER C400000001
     GCRS PART RETURN CREDIT RM:C400000001 CORE:U1111111AB
                           SUB-TOTAL     2500.00-                     2500.00-

0318100-1000002 ORD#: GCRS    O/T:M
001 ARC31101   DEPOSIT PART VALUES       150.00-                       150.00-
     REFERENCE/CONTROL NUMBER C400000002
     GCRS PART RETURN CREDIT RM:C400000002 CORE:22222222AI
     CLAIM:123456 VIN:1ABCDEFGH12345678 REPAIR:09/09/2026
""" + MEMO_HEADER.format(page=2) + """
                           SUB-TOTAL     150.00-                       150.00-

0318100-1000003 ORD#: GCRS    O/T:M
001 ARC31101   DEPOSIT PART VALUES        75.00-                        75.00-
     REFERENCE/CONTROL NUMBER C400000099
     GCRS PART RETURN CREDIT RM:C400000099 CORE:33333333AA
                           SUB-TOTAL      75.00-                        75.00-
SUMMARY:
    TOTAL GROSS AMOUNT                             2500.00-
    ARC31101 DEPOSIT PART VALUES                    225.00-
    NET CREDIT AMOUNT                              2725.00-
"""


def W(*tokens):
    """Fake OCR words for parse_line."""
    return [{'text': t, 'left': 100 + i * 60, 'top': 400, 'width': 50,
             'height': 20, 'conf': 90} for i, t in enumerate(tokens)]


class TestMemoParser(unittest.TestCase):
    def test_blocks_amounts_and_tie_out(self):
        r = parse_memo_text(MEMO_TEXT, source='t')
        self.assertEqual(r['dealer'], '12345')
        memo = r['memos']['CC00900001']
        self.assertEqual(memo['date'], datetime.date(2026, 10, 3))
        self.assertEqual(memo['net_total'], 2725.0)
        self.assertAlmostEqual(memo['lines_total'], 2725.0)
        self.assertEqual(r['warnings'], [])
        by = {c['ticket']: c for c in r['credits']}
        self.assertEqual(by['C400000001']['amount'], 2500.0)  # > $999
        self.assertEqual(by['C400000001']['part'], 'U1111111AB')
        # sub-total on the next page still belongs to this block
        self.assertEqual(by['C400000002']['amount'], 150.0)
        self.assertEqual(by['C400000002']['claim'], 123456)
        self.assertEqual(by['C400000002']['part'], '22222222AI')  # AI kept
        self.assertEqual(by['C400000099']['amount'], 75.0)

    def test_mismatch_is_reported(self):
        bad = MEMO_TEXT.replace('NET CREDIT AMOUNT                              2725.00-',
                                'NET CREDIT AMOUNT                              2800.00-')
        r = parse_memo_text(bad)
        self.assertTrue(any('NET CREDIT AMOUNT' in w for w in r['warnings']))


class TestOcrHelpers(unittest.TestCase):
    def test_ticket_lookalikes(self):
        self.assertEqual(ocr_ticket('0411100011')[0], 'C411100011')
        self.assertEqual(ocr_ticket('©411100022')[0], 'C411100022')
        self.assertEqual(ocr_ticket('6411100033')[0], 'C411100033')
        self.assertEqual(ocr_ticket('CBO0111108')[0], 'CB00111108')
        self.assertIsNone(ocr_ticket('68520294AB'))     # part, not ticket
        self.assertIsNone(ocr_ticket('04801313AE'))

    def test_part_cleanup(self):
        self.assertEqual(ocr_part('U8i56209AH'), 'U8156209AH')
        self.assertEqual(ocr_part('U8090720AI'), 'U8090720AI')
        self.assertEqual(ocr_part('O5151705AF'), '05151705AF')
        self.assertEqual(ocr_part('$3022263AF'), '53022263AF')
        self.assertEqual(ocr_part('vU8052759AA'), 'U8052759AA')
        self.assertEqual(ocr_part('CEZGU902AB'), 'CEZGU902AB')

    def test_shipper_lines(self):
        r = parse_line(W('411100044', '68474094AA', 'SHAFT', '1-75.00'))
        self.assertEqual((r['ticket'], r['qty'], r['amount']),
                         ('C411100044', 1, 75.0))
        r = parse_line(W('C411100055', 'U8444855AB', 'CORE', '1125.00'))
        self.assertEqual((r['qty'], r['amount']), (1, 125.0))
        r = parse_line(W('UnPaid', 'C411100066', '123987', 'CEZGU902AB',
                         'MANIFOLD', '1', '500.00'))
        self.assertEqual((r['ticket'], r['claim'], r['part'], r['amount']),
                         ('C411100066', 123987, 'CEZGU902AB', 500.0))
        r = parse_line(W('C411100077', 'som', '57009571AA', 'COMPRESSOR',
                         '1', '50.00', ';'))
        self.assertEqual((r['part'], r['amount']), ('57009571AA', 50.0))
        r = parse_line(W('C411100088', 'U8429324AA', 'CORE', '1',
                         '2,000.00'))
        self.assertEqual(r['amount'], 2000.0)
        self.assertIsNone(parse_line(W('PART', 'CONTROL', 'CLAIM')))

    def test_shp_header(self):
        f = _shipment_from_header
        self.assertEqual(f('GCRS DEALER SHIPPER DOCUMENT GCRS SHP#2026-09-25 '
                           '16:30.01'), '2026-09-25 16:30.01')
        self.assertEqual(f('GCRS SHP#42026-08-2I1 18:00.01'),
                         '2026-08-21 18:00.01')
        self.assertEqual(f('GCRS SHP#2026-~-09-21 17:00.01'),
                         '2026-09-21 17:00.01')
        self.assertEqual(f('GCRS SHP#2026-08-03'), '2026-08-03')
        self.assertIsNone(f('GCRS SHP#2026-13-03'))


def make_master(path, rows):
    M.write_master(rows, path)


def row(memo, shp, ticket, part, amount, claim=None, req=None):
    return {'memo': memo, 'shipment': shp, 'ticket': ticket, 'part': part,
            'claim': claim, 'amount': amount, 'requested': req}


class TestEngine(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.asof = datetime.date(2026, 10, 5)
        self.rows = [
            row('CC00800000', None, 'C300000001', '11111111AA', 50),
            row('#000900001', '2026-07-01 17:00.01', 'C400000099',
                '33333333AA', 75),                     # paid via DC doc #
            row(None, '2026-08-03 17:00.01', 'C400000001', 'U1111111AB',
                2500),
            row(None, '2026-08-03 17:00.01', 'C400000002', '22222222AI',
                200),                                   # credited at 150
            row(None, '2026-08-05 17:00.01', 'C400000003', '44444444AA',
                100),                                   # 61 days, unpaid
            row(None, '2026-08-06 17:00.01', 'C400000004', '55555555AA',
                125, req='2026-09-28'),                 # requested before
            row(None, '2026-08-07 17:00.01', 'C400000005', '66666666AA',
                90),                                    # 59 days
            row(None, '2026-10-01 17:00.01', 'C400000006', '77777777AA',
                40),                                    # recent
        ]
        self.master = os.path.join(self.tmp, 'Core_Returns_RECONCILED.xlsx')
        make_master(self.master, [dict(r) for r in self.rows])

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _rec(self, **opts):
        o = {'asof': self.asof, 'threshold': 60, 'dealer': '12345',
             'mark_requested': True, 'apply_dc': True,
             'include_prev': False}
        o.update(opts)
        rec = engine.Reconciler(o)
        rec.load_master(self.master, 'Core_Returns_RECONCILED.xlsx')
        rec.load_dc([], '')
        rec.pages = []
        return rec

    def test_credits(self):
        rec = self._rec()
        credits = parse_memo_text(MEMO_TEXT)['credits']
        for c in credits:
            rec._apply_credit(c, False)
        led = rec.ledger
        self.assertEqual(led.find('C400000001')[0]['memo'], 'CC00900001')
        # partial credit: master now shows the credited amount
        self.assertEqual(led.find('C400000002')[0]['amount'], 150)
        self.assertEqual(len(rec.r['partial']), 1)
        self.assertEqual(rec.r['partial'][0]['diff'], -50.0)
        # #000900001 on the master is the same memo as CC00900001
        self.assertEqual(rec.r['already_applied'], 1)
        self.assertEqual(len(led.find('C400000099')), 1)
        # applying the same memo again changes nothing
        before = len(led.rows)
        for c in credits:
            rec._apply_credit(c, False)
        self.assertEqual(len(led.rows), before)
        self.assertEqual(rec.r['already_applied'], 1 + 3)

    def test_unmatched_credit_is_added_as_paid(self):
        rec = self._rec()
        rec._apply_credit({'ticket': 'C499999999', 'memo': 'CC00900002',
                           'memo_date': self.asof, 'part': '99999999AA',
                           'claim': None, 'amount': 60.0}, False)
        added = rec.ledger.find('C499999999')[0]
        self.assertEqual((added['memo'], added['shipment'], added['amount']),
                         ('CC00900002', None, 60))
        self.assertEqual(len(rec.r['unmatched_credits']), 1)

    def test_credit_request_rules(self):
        rec = self._rec()
        rec.build_request()
        req = [x['ticket'] for x in rec.r['credit_request']]
        # 63 and 61 days old -> requested; 59 days -> not yet;
        # requested 9/28 -> listed separately, not repeated
        self.assertEqual(req, ['C400000001', 'C400000002', 'C400000003'])
        self.assertEqual([x['ticket'] for x in rec.r['prev_requested']],
                         ['C400000004'])
        self.assertEqual(rec.ledger.find('C400000003')[0]['requested'],
                         '2026-10-05')
        # oldest shipment first, no pages uploaded -> all need a re-scan
        self.assertEqual(rec.r['rescan'][0]['date'], '2026-08-03')

    def test_same_day_rerun_keeps_request(self):
        rec = self._rec()
        rec.build_request()
        rec2 = engine.Reconciler(rec.o)
        rec2.ledger = rec.ledger
        rec2.page_refs = {}
        rec2.build_request()
        self.assertEqual(len(rec2.r['credit_request']), 3)

    def test_include_previously_requested(self):
        rec = self._rec(include_prev=True)
        rec.build_request()
        self.assertIn('C400000004',
                      [x['ticket'] for x in rec.r['credit_request']])

    def test_dealerconnect(self):
        text = ('Tracking Number\tPart Number\tWarranty Claim\tAmount\t'
                'Received Date\tPayment Type\tDocument\n'
                'C400000003\t44444444AB\t\t100.00\t\t\t\n'      # part fix
                'C400000005\t66666666AA\t\t90.00\t9/30/2026\tCR\t#000900003\n'
                'C400000777\t88888888AA\t\t25.00\t\t\t\n')       # not on ours
        from recon.dealerconnect import merge
        rec = self._rec()
        rec.dc_rows = read_dc_text(text)
        rec.dc = merge(rec.dc_rows)
        rec.dc_has_paid = rec.dc_has_unpaid = True
        rec.dc_pull = datetime.date(2026, 10, 2)
        rec.apply_dc()
        status = {x['ticket']: x['status'] for x in rec.r['comparison']}
        self.assertEqual(status['C400000003'], 'FIXED FROM DEALERCONNECT')
        self.assertEqual(rec.ledger.find('C400000003')[0]['part'],
                         '44444444AB')
        self.assertEqual(status['C400000005'], 'PAID PER MOPAR')
        self.assertEqual(rec.ledger.find('C400000005')[0]['memo'],
                         '#000900003')
        self.assertEqual(status['C400000777'], 'ADD TO MASTER')
        self.assertEqual(status['C400000001'], 'MOPAR HAS NO RECORD')
        # shipped 10/01, pull dated 10/02 from the file name: 10/01 is
        # before the pull so it is checked; anything on/after is skipped
        self.assertIn('C400000006', status)


class TestScanDedupe(unittest.TestCase):
    def test_copies_vs_consecutive_tickets(self):
        def ln(t):
            return {'ticket': t, 'ticket_raw': t, 'ticket_fixed': False,
                    'claim': None, 'part': '11111111AA', 'part_ok': True,
                    'description': 'X', 'qty': 1, 'amount': 50.0,
                    'box': (10, 10, 20, 20), 'line_box': (0, 0, 1, 1),
                    'conf': 90}

        def page(f, n, sid, tickets):
            return {'file': f, 'path': f, 'page': n, 'width': 1700,
                    'height': 2200, 'dpi': 200, 'shipment_id': sid,
                    'dealer': None, 'lines': [ln(t) for t in tickets],
                    'checkoff_x': None, 'printed_total': None}
        rec = engine.Reconciler({'asof': datetime.date(2026, 10, 5),
                                 'threshold': 60})
        rec.load_master(None, None)
        rec.load_dc([], '')
        s1, s2 = '2026-10-01 17:00.01', '2026-10-03 17:00.01'
        rec.pages = [
            page('a.pdf', 1, s1, ['C412200212', 'C412200213', 'C412200300']),
            # second copy of the same page, 13 misread as 18
            page('b.pdf', 1, s1, ['C412200212', 'C412200218', 'C412200300']),
            # two-page shipper: consecutive tickets run onto page 2
            page('d.pdf', 1, s2, ['C413300001', 'C413300002']),
            page('d.pdf', 2, s2, ['C413300003', 'C413300004'])]
        rec.parts = engine.PartCorrector([])
        rec._resolve_lines()
        rec._apply_scans()
        added = [x['ticket'] for x in rec.r['new_shipments']]
        self.assertEqual(added, ['C412200212', 'C412200213', 'C412200300',
                                 'C413300001', 'C413300002', 'C413300003',
                                 'C413300004'])
        self.assertIn('C412200213', rec.r['ocr_review'][0]['issue'])


class TestMasterWorkbook(unittest.TestCase):
    def test_round_trip_and_layout(self):
        tmp = tempfile.mkdtemp()
        try:
            p = os.path.join(tmp, 'm.xlsx')
            rows = [row(None, '2026-08-03 17:00.01', 'C400000001', 'A1', 5),
                    row('CC1', None, 'C400000002', 'B1', 7),
                    row(None, '2026-07-01 17:00.01', 'C400000003', 'C1', 9)]
            M.write_master(rows, p)
            back, info = M.read_master(p)
            # blank Shipment ID first, then oldest shipment first
            self.assertEqual([r['ticket'] for r in back],
                             ['C400000002', 'C400000003', 'C400000001'])
            wb = load_workbook(p)
            self.assertEqual(wb.sheetnames, ['Core Returns', 'Unpaid Cores'])
            ws = wb['Core Returns']
            self.assertEqual([c.value for c in ws[1]][:8], M.HEADERS)
            self.assertEqual(ws['J1'].value, '=TODAY()')
            self.assertTrue(ws['G2'].value.startswith('=IF(B2=""'))
            self.assertEqual(ws.freeze_panes, 'A2')
            self.assertEqual(
                [c.value for c in wb['Unpaid Cores']['C']][1:],
                ['C400000003', 'C400000001'])
            cf = list(ws.conditional_formatting)
            self.assertEqual(cf[0].rules[0].formula,
                             ['AND($A2="",ISNUMBER($G2),$G2>60)'])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestWebApp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ['RECON_JOB_DIR'] = self.tmp
        from recon import jobs
        jobs.JOB_ROOT = self.tmp
        import app as webapp
        self.client = webapp.app.test_client()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_pages(self):
        self.assertEqual(self.client.get('/').status_code, 200)
        self.assertEqual(self.client.get('/reconcile').status_code, 200)

    def test_needs_input(self):
        r = self.client.post('/api/reconcile', data={})
        self.assertEqual(r.status_code, 400)

    def test_wrong_file_type(self):
        import io
        r = self.client.post('/api/reconcile', data={
            'master': (io.BytesIO(b'x'), 'master.pdf')},
            content_type='multipart/form-data')
        self.assertEqual(r.status_code, 400)

    def test_full_run_with_pasted_dealerconnect(self):
        import io
        p = os.path.join(self.tmp, 'Core_Returns_RECONCILED.xlsx')
        M.write_master([row(None, '2026-07-01 17:00.01', 'C400000001',
                            '11111111AA', 50)], p)
        with open(p, 'rb') as fh:
            data = {'master': (io.BytesIO(fh.read()),
                               'Core_Returns_RECONCILED.xlsx'),
                    'dc_text': 'C400000001\t11111111AA\t\t50.00\t\t\t',
                    'asof': '2026-10-05', 'threshold': '60'}
        r = self.client.post('/api/reconcile', data=data,
                             content_type='multipart/form-data')
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        job_id = r.get_json()['job_id']
        for _ in range(100):
            job = self.client.get(f'/api/jobs/{job_id}').get_json()
            if job['status'] in ('done', 'error'):
                break
            time.sleep(0.1)
        self.assertEqual(job['status'], 'done', job.get('error'))
        res = job['result']
        self.assertEqual([x['ticket'] for x in res['credit_request']],
                         ['C400000001'])
        names = [f['name'] for f in res['files']]
        self.assertIn('Credit_Request.xlsx', names)
        dl = self.client.get(f'/api/jobs/{job_id}/files/Credit_Request.xlsx')
        self.assertEqual(dl.status_code, 200)
        wb = load_workbook(io.BytesIO(dl.data))
        ws = wb['Credit Request']
        self.assertEqual([c.value for c in ws[2]],
                         [None, '2026-07-01', 'C400000001'])
        self.assertEqual(
            self.client.get(f'/api/jobs/{job_id}/files/../app.py')
            .status_code, 404)


if __name__ == '__main__':
    unittest.main()
