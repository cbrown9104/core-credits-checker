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


def scan_line(ticket, part='11111111AA', amount=50.0, y=0):
    return {'ticket': ticket, 'ticket_raw': ticket, 'ticket_fixed': False,
            'claim': None, 'part': part, 'part_ok': True,
            'description': 'X', 'qty': 1, 'amount': amount,
            'box': (10, y, 20, y + 10), 'line_box': (0, y, 1, y + 10),
            'conf': 90}


def scan_page(name, number, shipment, lines):
    for y, ln in enumerate(lines):
        ln['box'] = (10, 100 * y, 20, 100 * y + 10)
    return {'file': name, 'path': name, 'page': number, 'width': 1700,
            'height': 2200, 'dpi': 200, 'shipment_id': shipment,
            'dealer': None, 'lines': lines, 'checkoff_x': None,
            'printed_total': None}


class TestMisreadRules(unittest.TestCase):
    """GCRS control tickets run in sequence, so real tickets one digit
    apart sit next to each other on a page. A line is only ever treated
    as a misread of a neighbouring ticket when the paperwork does not show
    that neighbour under its own number and the part and amount agree."""

    SHP = '2026-07-01 17:00.01'

    def rec(self, master_rows=(), dc_text=''):
        from recon.dealerconnect import merge
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        rec = engine.Reconciler({'asof': datetime.date(2026, 10, 5),
                                 'threshold': 60})
        if master_rows:
            path = os.path.join(tmp, 'm.xlsx')
            make_master(path, [dict(r) for r in master_rows])
            rec.load_master(path, 'm.xlsx')
        else:
            rec.load_master(None, None)
        rec.load_dc([], dc_text)
        self.assertEqual(rec.dc, merge(rec.dc_rows))
        return rec

    def scan(self, rec, pages):
        rec.pages = pages
        rec.parts = engine.PartCorrector(
            [r.get('part') for r in rec.ledger.rows] +
            [d['part'] for d in rec.dc.values() if d.get('part')])
        rec._resolve_lines()
        rec._apply_scans()
        return [x['ticket'] for x in rec.r['new_shipments']]

    def test_a_neighbour_on_dealerconnect_never_replaces_a_ticket(self):
        # five tickets in a row on one page; Mopar's unpaid list has only
        # the second (the first is already paid)
        rec = self.rec(dc_text='C418800202\t22222222AB\t\t125.00\t\t\t')
        lines = [scan_line('C418800201', '11111111AA', 125.0),
                 scan_line('C418800202', '22222222AB', 125.0),
                 scan_line('C418800203', '33333333AC', 60.0),
                 scan_line('C418800204', '44444444AD', 75.0),
                 scan_line('C418800205', '55555555AE', 90.0)]
        added = self.scan(rec, [scan_page('a.pdf', 1, self.SHP, lines)])
        self.assertEqual(added, ['C418800201', 'C418800202', 'C418800203',
                                 'C418800204', 'C418800205'])
        self.assertEqual(rec.r['ocr_review'], [])
        by = {x['ticket']: x for x in rec.r['new_shipments']}
        self.assertEqual(by['C418800201']['source'], 'scan')
        self.assertEqual(by['C418800202']['source'], 'DealerCONNECT')
        # each ticket points at its own line (where "UnPaid" is stamped)
        for i, t in enumerate(added):
            page, ln = rec.page_refs[t][0]
            self.assertEqual(ln['box'][1], 100 * i, t)
        # ... also when the DealerCONNECT ticket is on the next page
        rec = self.rec(dc_text='C418800202\t22222222AB\t\t125.00\t\t\t')
        added = self.scan(rec, [
            scan_page('a.pdf', 1, self.SHP,
                      [scan_line('C418800150', '99999999AA', 10.0),
                       scan_line('C418800201', '11111111AA', 125.0)]),
            scan_page('a.pdf', 2, self.SHP,
                      [scan_line('C418800202', '22222222AB', 125.0),
                       scan_line('C418800203', '33333333AC', 60.0)])])
        self.assertEqual(added, ['C418800150', 'C418800201', 'C418800202',
                                 'C418800203'])

    def test_a_real_misread_is_still_fixed_from_dealerconnect(self):
        # ...207 scanned as ...287; DealerCONNECT has ...207 with the same
        # part and amount, and it is on no page under its own number
        rec = self.rec(dc_text='C418800207\t22222222AB\t\t125.00\t\t\t')
        added = self.scan(rec, [scan_page('a.pdf', 1, self.SHP, [
            scan_line('C418800201', '11111111AA', 50.0),
            scan_line('C418800287', '22222222AB', 125.0)])])
        self.assertEqual(added, ['C418800201', 'C418800207'])
        # (...201 is one digit from ...207 too, but its part and amount
        # are different, so it is left alone without comment)
        self.assertEqual(len(rec.r['ocr_review']), 1)
        self.assertIn('C418800207', rec.r['ocr_review'][0]['issue'])
        self.assertIn('used that', rec.r['ocr_review'][0]['issue'])

    def test_a_neighbour_with_another_part_or_amount_is_another_ticket(self):
        # DealerCONNECT's ...202 is on no uploaded page, but the line that
        # reads ...201 shows a different part and amount: it is ...201
        rec = self.rec(dc_text='C418800202\t22222222AB\t\t125.00\t\t\t')
        added = self.scan(rec, [scan_page('a.pdf', 1, self.SHP, [
            scan_line('C418800201', '11111111AA', 50.0)])])
        self.assertEqual(added, ['C418800201'])
        self.assertEqual(rec.ledger.find('C418800202'), [])
        self.assertEqual(rec.r['ocr_review'], [])
        # the same part with a different amount is not enough either
        rec = self.rec(dc_text='C418800202\t11111111AA\t\t125.00\t\t\t')
        added = self.scan(rec, [scan_page('a.pdf', 1, self.SHP, [
            scan_line('C418800201', '11111111AA', 50.0)])])
        self.assertEqual(added, ['C418800201'])
        self.assertEqual(rec.r['ocr_review'], [])
        # nothing readable on the line to compare: kept as read, and said
        rec = self.rec(dc_text='C418800202\t22222222AB\t\t125.00\t\t\t')
        added = self.scan(rec, [scan_page('a.pdf', 1, self.SHP, [
            scan_line('C418800201', '', None)])])
        self.assertEqual(added, ['C418800201'])
        note = rec.r['ocr_review'][0]
        self.assertEqual(note['ticket'], 'C418800201')
        self.assertIn('Kept as C418800201', note['issue'])

    def test_master_neighbour_on_another_page_is_not_a_misread(self):
        master = [row(None, self.SHP, 'C418800202', '22222222AB', 125)]
        rec = self.rec(master)
        added = self.scan(rec, [
            scan_page('a.pdf', 1, self.SHP,
                      [scan_line('C418800150', '99999999AA', 10.0),
                       scan_line('C418800201', '22222222AB', 125.0)]),
            scan_page('a.pdf', 2, self.SHP,
                      [scan_line('C418800202', '22222222AB', 125.0),
                       scan_line('C418800203', '33333333AC', 60.0)])])
        # ...201 is its own ticket even with the same part and amount,
        # because ...202 is right there on page 2
        self.assertEqual(added, ['C418800150', 'C418800201', 'C418800203'])
        self.assertEqual(rec.r['ocr_review'], [])
        self.assertEqual(len(rec.page_refs['C418800202']), 1)

    def test_master_neighbour_is_matched_only_when_the_line_agrees(self):
        master = [row(None, self.SHP, 'C418800202', '22222222AB', 125),
                  row(None, self.SHP, 'C418800150', '99999999AA', 10)]
        # agrees (same part and amount), and ...202 is on no page: misread
        rec = self.rec(master)
        added = self.scan(rec, [scan_page('a.pdf', 1, self.SHP, [
            scan_line('C418800150', '99999999AA', 10.0),
            scan_line('C418800282', '22222222AB', 125.0)])])
        self.assertEqual(added, [])
        self.assertIn('matched to C418800202',
                      rec.r['ocr_review'][0]['issue'])
        self.assertEqual(len(rec.page_refs['C418800202']), 1)
        # a different amount: kept as its own ticket, and flagged
        rec = self.rec(master)
        added = self.scan(rec, [scan_page('a.pdf', 1, self.SHP, [
            scan_line('C418800150', '99999999AA', 10.0),
            scan_line('C418800201', '22222222AB', 60.0)])])
        self.assertEqual(added, ['C418800201'])
        self.assertIn('kept as its own ticket',
                      rec.r['ocr_review'][0]['issue'])
        self.assertEqual(rec.page_refs.get('C418800202', []), [])

    def test_second_copy_of_a_page_with_a_misread_adds_nothing(self):
        s1 = '2026-10-01 17:00.01'
        master = [row(None, s1, t, '11111111AA', 50) for t in
                  ('C412200212', 'C412200213', 'C412200300')]
        rec = self.rec(master)
        added = self.scan(rec, [
            scan_page('a.pdf', 1, s1, [scan_line(t) for t in (
                'C412200212', 'C412200213', 'C412200300')]),
            scan_page('b.pdf', 1, s1, [scan_line(t) for t in (
                'C412200212', 'C412200218', 'C412200300')])])
        self.assertEqual(added, [])                 # ...218 is ...213
        self.assertEqual(len(rec.page_refs['C412200213']), 2)
        # the same with new tickets that DealerCONNECT knows, misread
        # copy first
        dc = '\n'.join(f'{t}\t11111111AA\t\t50.00\t\t\t' for t in (
            'C412200212', 'C412200213', 'C412200300'))
        rec = self.rec(dc_text=dc)
        added = self.scan(rec, [
            scan_page('b.pdf', 1, s1, [scan_line(t) for t in (
                'C412200212', 'C412200218', 'C412200300')]),
            scan_page('a.pdf', 1, s1, [scan_line(t) for t in (
                'C412200212', 'C412200213', 'C412200300')])])
        self.assertEqual(added, ['C412200212', 'C412200213', 'C412200300'])

    def test_scanned_memo_credit_goes_to_a_neighbour_only_when_sure(self):
        def credit(ticket, amount, memo='CC00900009'):
            return {'ticket': ticket, 'memo': memo,
                    'memo_date': datetime.date(2026, 10, 3),
                    'part': None, 'claim': None, 'amount': amount}
        master = [row(None, self.SHP, 'C418800202', '22222222AB', 125)]
        # same amount, and ...202 has no credit of its own: applied, said
        rec = self.rec(master)
        rec._apply_credit(credit('C418800282', 125.0), True)
        self.assertEqual(rec.ledger.find('C418800202')[0]['memo'],
                         'CC00900009')
        self.assertIn('applied to C418800202', rec.r['warnings'][0])
        # a different amount: its own (unmatched) credit, ...202 stays open
        rec = self.rec(master)
        rec._apply_credit(credit('C418800201', 60.0), True)
        self.assertIsNone(rec.ledger.find('C418800202')[0]['memo'])
        self.assertEqual(len(rec.r['unmatched_credits']), 1)
        # ...202 is credited in the same batch: ...201 is not a misread
        rec = self.rec(master)
        rec.credit_tickets = {'C418800201', 'C418800202'}
        rec._apply_credit(credit('C418800201', 125.0), True)
        rec._apply_credit(credit('C418800202', 125.0, 'CC00900010'), True)
        self.assertEqual(rec.ledger.find('C418800202')[0]['memo'],
                         'CC00900010')
        self.assertEqual(rec.ledger.find('C418800201')[0]['memo'],
                         'CC00900009')

    def test_a_request_made_in_this_run_shows_on_the_unpaid_list(self):
        rec = self.rec([row(None, self.SHP, 'C418800202', '22222222AB',
                            125)])
        rec.pages = []
        rec.build_request()
        self.assertEqual(rec.r['unpaid'][0]['requested'], '2026-10-05')
        rec = self.rec([row(None, self.SHP, 'C418800202', '22222222AB',
                            125)])
        rec.o['mark_requested'] = False
        rec.pages = []
        rec.build_request()
        self.assertIsNone(rec.r['unpaid'][0]['requested'])


class TestFilesThatGiveNothing(unittest.TestCase):
    """A file in the wrong box, or one that cannot be read, is called out
    instead of quietly producing zeros."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def run_engine(self, **inputs):
        base = {'master': None, 'memos': [], 'shippers': [], 'dc': [],
                'dc_text': ''}
        base.update(inputs)
        out = os.path.join(self.tmp, 'out')
        os.makedirs(out, exist_ok=True)
        return engine.run(base, {'asof': datetime.date(2026, 10, 5),
                                 'threshold': 60}, out)

    def write(self, name, data):
        path = os.path.join(self.tmp, name)
        with open(path, 'wb') as fh:
            fh.write(data)
        return path, name

    def test_shipper_file_with_no_tickets_is_called_out(self):
        for name, data in (('empty.pdf', b''),
                           ('broken.pdf', b'%PDF-1.4 this is not a pdf'),
                           ('letter.pdf', b'PK\x03\x04 a word file')):
            res = self.run_engine(shippers=[self.write(name, data)])
            warn = [w for w in res['warnings'] if w.startswith(name)]
            self.assertEqual(len(warn), 1, res['warnings'])
            self.assertIn('no control tickets could be read', warn[0])
            self.assertNotIn('Traceback', warn[0])

    def test_dealerconnect_file_that_is_not_excel_is_left_out(self):
        res = self.run_engine(
            dc=[self.write('unpaid.xlsx', b'')],
            dc_text='C400000001\t11111111AA\t\t50.00\t\t\t')
        warn = [w for w in res['warnings'] if w.startswith('unpaid.xlsx')]
        self.assertEqual(len(warn), 1, res['warnings'])
        self.assertIn('could not be opened', warn[0])
        # the pasted rows were still used
        self.assertEqual(res['inputs']['dealerconnect'],
                         ['unpaid.xlsx', 'pasted rows'])

    def test_memo_that_cannot_be_read_is_called_out(self):
        res = self.run_engine(memos=[self.write('memo.pdf', b'')])
        self.assertTrue(any(w.startswith('memo.pdf') for w in
                            res['warnings']), res['warnings'])


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


# ── page TOTAL check, re-reads, and the master's look ───────────────────
def ocr_line(text, top, left=300):
    """One OCR line, words 100 px apart, the way recon.ocr returns it."""
    words = [{'text': t, 'left': left + 100 * i, 'top': top, 'width': 90,
              'height': 20, 'conf': 90.0} for i, t in enumerate(text.split())]
    return {'text': text, 'words': words, 'left': left, 'top': top,
            'right': left + 100 * len(words), 'bottom': top + 20}


def page_line(ticket, part='11111111AA', amount=50.0, y=0):
    ln = scan_line(ticket, part, amount)
    ln['box'] = (300, y, 400, y + 20)
    ln['line_box'] = (200, y, 1500, y + 20)
    return ln


def total_page(lines, total_texts=(), shipment='2026-09-01 17:00.01',
               name='w.pdf', number=1):
    for i, ln in enumerate(lines):
        ln['box'] = (300, 500 + 30 * i, 400, 520 + 30 * i)
        ln['line_box'] = (200, 500 + 30 * i, 1500, 520 + 30 * i)
    p = scan_page(name, number, shipment, lines)
    for i, ln in enumerate(lines):          # scan_page moves the boxes
        ln['box'] = (300, 500 + 30 * i, 400, 520 + 30 * i)
    p['total_texts'] = list(total_texts)
    return p


class TestPageTotals(unittest.TestCase):
    """Every shipper page prints "TOTAL <count> <amount>". The rows read
    from a page are checked against it, pages that add tickets are read
    again, and the reads are compared."""

    def rec(self, master_rows=()):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        rec = engine.Reconciler({'asof': datetime.date(2026, 10, 5),
                                 'threshold': 60})
        if master_rows:
            path = os.path.join(tmp, 'm.xlsx')
            make_master(path, [dict(r) for r in master_rows])
            rec.load_master(path, 'm.xlsx')
        else:
            rec.load_master(None, None)
        rec.load_dc([], '')
        rec.parts = engine.PartCorrector(
            [r.get('part') for r in rec.ledger.rows])
        return rec

    def test_total_readings(self):
        from recon.shipper import total_amounts, total_counts
        self.assertEqual(total_amounts('TOTAL 3 3,575.00')[0], [3575.0])
        self.assertEqual(total_counts('TOTAL 3 3,575.00'), [3])
        self.assertEqual(total_counts('TOTAL 14 1,400.00'), [14])
        # the count circled by hand: pen marks are not a count
        self.assertEqual(total_counts('TOTAL C2) 3,035.00'), [])
        self.assertEqual(total_counts('TOTAL (@) 1,815.00'), [])
        # the circled count glued onto the amount: both readings offered
        direct, loose = total_amounts('7965.00')
        self.assertEqual(direct, [7965.0])
        self.assertIn(965.0, loose)
        direct, loose = total_amounts('51, 615.00')
        self.assertEqual(direct, [51615.0])
        self.assertIn(1615.0, loose)
        self.assertEqual(total_amounts('TOTAL 2 650-00')[0], [650.0])
        self.assertEqual(total_amounts('TOTAL Cp sec'), ([], []))

    def test_only_the_table_is_read(self):
        from recon.shipper import parse_page
        lines = [
            ocr_line('DEALER: 455580000 SHIP TO:', 160),   # looks like one
            ocr_line('PART CONTROL CLAIM PART PART CORE AMOUNT', 480),
            ocr_line('CHECKOFF TICKET NUMBER NUMBER DESCRIPTION QUANTITY '
                     'USD', 505),
            ocr_line('C400000001 11111111AA CORE 1 75.00', 535),
            ocr_line('C400000002 22222222AB CORE 1 1,000.00', 565),
            ocr_line('TOTAL 2 1,075.00', 625),
            # a driver's scan receipt stapled under the table
            ocr_line('C400000003', 860), ocr_line('C400000004', 890),
            ocr_line('PICKUP VERIFICATION:-', 1500),
        ]
        page = parse_page({'page': 1, 'width': 1700, 'height': 2200,
                           'dpi': 200, 'lines': lines})
        self.assertEqual([ln['ticket'] for ln in page['lines']],
                         ['C400000001', 'C400000002'])
        self.assertEqual(page['total_texts'], ['TOTAL 2 1,075.00'])
        self.assertEqual(page['printed_total'], 1075.0)
        # no TOTAL read at all: a big gap still ends the table
        page = parse_page({'page': 1, 'width': 1700, 'height': 2200,
                           'dpi': 200,
                           'lines': lines[:5] + lines[6:]})
        self.assertEqual([ln['ticket'] for ln in page['lines']],
                         ['C400000001', 'C400000002'])
        # headings unreadable: the page is taken whole, as before
        page = parse_page({'page': 1, 'width': 1700, 'height': 2200,
                           'dpi': 200, 'lines': lines[3:5]})
        self.assertEqual(len(page['lines']), 2)

    def check(self, rec, page, reads=(), crops=((), ())):
        cand = rec._candidates(page, reads, crops)
        if reads:
            rec._merge_reads(page, list(reads), cand)
        new = any(not rec.ledger.find(ln['ticket']) for ln in page['lines'])
        return rec._page_check(page, cand, new)

    def test_a_page_that_adds_up_matches(self):
        rec = self.rec()
        page = total_page([page_line('C400000001', amount=75.0),
                           page_line('C400000002', amount=1000.0)],
                          ['TOTAL 2 1,075.00'])
        c = self.check(rec, page)
        self.assertEqual((c['status'], c['lines'], c['amount'],
                          c['printed_count'], c['printed_amount']),
                         ('ok', 2, 1075.0, 2, 1075.0))
        # count circled (unreadable) but the amount ties: still a match
        page['total_texts'] = ['TOTAL (@) 1,075.00']
        self.assertEqual(self.check(rec, page)['status'], 'ok')
        # a close-up that glued the count onto the amount, count confirmed
        page['total_texts'] = []
        self.assertEqual(self.check(rec, page, (), (['21,075.00'], ['2']))
                         ['status'], 'ok')
        # the same glued reading with no count to confirm it is not enough
        self.assertEqual(self.check(rec, page, (), (['21,075.00'], []))
                         ['status'], 'unread')

    def test_a_misread_amount_is_put_right_from_the_total(self):
        # first read 4,000.00 where the page says 1,000.00 (page total
        # 3,575.00); a re-read has it right
        rec = self.rec()
        page = total_page([page_line('C400000001', amount=75.0),
                           page_line('C400000002', amount=4000.0),
                           page_line('C400000003', amount=2500.0)],
                          ['TOTAL 3 3,575.00'])
        again = [page_line('C400000001', amount=75.0, y=500),
                 page_line('C400000002', amount=1000.0, y=530),
                 page_line('C400000003', amount=2500.0, y=560)]
        c = self.check(rec, page, [{'rows': again,
                                    'total_texts': ['TOTAL 3 3,575.00']}])
        self.assertEqual(c['status'], 'ok')
        self.assertEqual([ln['amount'] for ln in page['lines']],
                         [75.0, 1000.0, 2500.0])
        self.assertIn('first read as $4,000.00',
                      page['lines'][1]['flags'][0])
        self.assertEqual(c['changes'],
                         ['C400000002: amount $4,000.00 → $1,000.00'])
        # no re-read has the right amount: nothing is forced, page flagged
        rec = self.rec()
        page = total_page([page_line('C400000001', amount=75.0),
                           page_line('C400000002', amount=4000.0)],
                          ['TOTAL 2 1,075.00'])
        again = [page_line('C400000001', amount=75.0, y=500),
                 page_line('C400000002', amount=4000.0, y=530)]
        c = self.check(rec, page, [{'rows': again,
                                    'total_texts': ['TOTAL 2 1,075.00']}])
        self.assertEqual(c['status'], 'mismatch')
        self.assertEqual(page['lines'][1]['amount'], 4000.0)
        self.assertIn('$1,075.00', c['note'])
        self.assertIn('$4,075.00', c['note'])

    def test_an_unreadable_amount_is_what_the_total_leaves(self):
        rec = self.rec()
        page = total_page([page_line('C400000001', amount=500.0),
                           page_line('C400000002', amount=None),
                           page_line('C400000003', amount=120.0)],
                          ['TOTAL 3 745.00'])
        again = [page_line('C400000001', amount=500.0, y=500),
                 page_line('C400000002', amount=None, y=530),
                 page_line('C400000003', amount=120.0, y=560)]
        c = self.check(rec, page, [{'rows': again,
                                    'total_texts': ['TOTAL 3 745.00']}])
        self.assertEqual(page['lines'][1]['amount'], 125.0)
        self.assertIn('TOTAL leaves', page['lines'][1]['flags'][0])
        self.assertEqual(c['status'], 'ok')
        # one read of the TOTAL is not enough to fill an amount from it
        rec = self.rec()
        page = total_page([page_line('C400000001', amount=500.0),
                           page_line('C400000002', amount=None)],
                          ['TOTAL 2 625.00'])
        again = [page_line('C400000001', amount=500.0, y=500),
                 page_line('C400000002', amount=None, y=530)]
        c = self.check(rec, page, [{'rows': again, 'total_texts': []}])
        self.assertIsNone(page['lines'][1]['amount'])
        self.assertNotEqual(c['status'], 'ok')

    def test_a_line_the_first_read_missed(self):
        def reread(extra, amount=50.0):
            return {'rows': [page_line('CB00800001', amount=100.0, y=500),
                             page_line('CW00100530', amount=50.0, y=530),
                             page_line(extra, '33333333AC', amount, y=560)],
                    'total_texts': []}

        def page():
            return total_page([page_line('CB00800001', amount=100.0),
                               page_line('CW00100530', amount=50.0)])
        # only one re-read has it and nothing else knows the ticket: not
        # added, but said
        rec = self.rec()
        p = page()
        self.check(rec, p, [reread('CW00100951'),
                            {'rows': [], 'total_texts': []}])
        self.assertEqual(len(p['lines']), 2)
        self.assertIn('CW00100951', rec.r['ocr_review'][0]['issue'])
        # this run's credit memo pays that ticket: it is a real line
        rec = self.rec()
        rec.credit_tickets = {'CW00100951'}
        p = page()
        self.check(rec, p, [reread('CW00100951'),
                            {'rows': [], 'total_texts': []}])
        self.assertEqual([ln['ticket'] for ln in p['lines']],
                         ['CB00800001', 'CW00100530', 'CW00100951'])
        self.assertIn('missed on the first read', p['lines'][2]['flags'][0])
        # two re-reads have it
        rec = self.rec()
        p = page()
        self.check(rec, p, [reread('CW00100951'), reread('CW00100951')])
        self.assertEqual(len(p['lines']), 3)
        # one re-read has it and the page only adds up with it
        rec = self.rec()
        p = page()
        p['total_texts'] = ['TOTAL 3 200.00']
        c = self.check(rec, p, [reread('CW00100951'),
                                {'rows': [], 'total_texts': []}])
        self.assertEqual(len(p['lines']), 3)
        self.assertEqual(c['status'], 'ok')
        # the added line goes on the master with the page's shipment
        rec.pages = [p]
        rec._resolve_lines()
        rec._apply_scans()
        added = {x['ticket']: x for x in rec.r['new_shipments']}
        self.assertEqual(added['CW00100951']['shipment'],
                         '2026-09-01 17:00.01')
        self.assertIn('missed on the first read',
                      added['CW00100951']['flags'])
        # something with no part and no amount is not a line
        rec = self.rec()
        p = page()
        junk = reread('C400000218', None)
        junk['rows'][2]['part'] = ''
        self.check(rec, p, [junk, dict(junk)])
        self.assertEqual(len(p['lines']), 2)

    def test_a_short_page_is_called_out(self):
        # printed TOTAL 2 / $125.00, one line read (the other is crossed
        # out by hand, or the scan dropped it)
        rec = self.rec()
        page = total_page([page_line('C400000001', amount=50.0)],
                          ['TOTAL 2 125.00'])
        again = {'rows': [page_line('C400000001', amount=50.0, y=500)],
                 'total_texts': ['TOTAL 2 125.00']}
        rec.pages = [page]
        page['check'] = self.check(rec, page, [again, dict(again)])
        self.assertEqual(page['check']['status'], 'mismatch')
        self.assertIn('shows 2 lines, 1 were read', page['check']['note'])
        self.assertIn('crossed out', page['check']['note'])
        rec._report_page_checks()
        self.assertIn('do not match their printed TOTAL',
                      rec.r['warnings'][0])
        self.assertIn('Page does not match', rec.r['ocr_review'][0]['issue'])
        # the same page with its ticket already on the master: listed for
        # review, no warning up top
        rec = self.rec([row(None, '2026-09-01 17:00.01', 'C400000001',
                            '11111111AA', 50)])
        page = total_page([page_line('C400000001', amount=50.0)],
                          ['TOTAL 2 125.00'])
        rec.pages = [page]
        page['check'] = self.check(rec, page, [again, dict(again)])
        rec._report_page_checks()
        self.assertEqual(rec.r['page_checks'][0]['status'], 'mismatch')
        self.assertEqual(len(rec.r['ocr_review']), 1)
        self.assertEqual(rec.r['warnings'], [])

    def test_a_total_hidden_by_the_pen_is_unread_not_wrong(self):
        # two reads agree on 995.00, the rows add to 1,995.00: the circle
        # around the count covers the "1,"
        rec = self.rec()
        page = total_page([page_line('C400000001', amount=1000.0),
                           page_line('C400000002', amount=995.0)],
                          ['TOTAL © 995.00'])
        again = {'rows': [page_line('C400000001', amount=1000.0, y=500),
                          page_line('C400000002', amount=995.0, y=530)],
                 'total_texts': ['TOTAL © 995.00']}
        c = self.check(rec, page, [again])
        self.assertEqual(c['status'], 'unread')
        # rows on the master that add up by the master's amounts match
        rec = self.rec([row(None, '2026-09-01 17:00.01', 'C400000001',
                            '11111111AA', 1000)])
        page = total_page([page_line('C400000001', amount=4000.0)],
                          ['TOTAL 1 1,000.00'])
        self.assertEqual(self.check(rec, page)['status'], 'ok')

    def test_part_numbers_are_settled_over_the_reads(self):
        rec = self.rec([row('CC00800001', '2026-06-01 17:00.01',
                            'C300000001', '5YH25LXHAA', 100),
                        row('CC00800001', '2026-06-01 17:00.01',
                            'C300000002', '53010748AA', 100)])

        def settle(*parts):
            reads = [page_line('C400000001', p) for p in parts]
            rec._vote_part(reads[0], reads)
            return reads[0]['part'], ' '.join(reads[0].get('flags', []))
        # a look-alike of a part on record (S for 5), on every read
        part, flag = settle('SYH25LXHAA', 'SYH25LXHAA', 'SYH25LXHAA')
        self.assertEqual(part, '5YH25LXHAA')
        self.assertIn('matched to 5YH25LXHAA', flag)
        # every read shows 53010749AA; ...48AA is on record. 9 and 8 are
        # not look-alikes, so it is a different part: kept, and said
        part, flag = settle('53010749AA', '53010749AA', '53010749AA')
        self.assertEqual(part, '53010749AA')
        self.assertIn('kept as read', flag)
        # not on record and the reads disagree: most reads, flagged
        part, flag = settle('68261528AR', '68261528AE', '68261528AE')
        self.assertEqual(part, '68261528AE')
        self.assertIn('read differently', flag)
        part, flag = settle('68261528AR', '68261528AE', '68261528AK')
        self.assertEqual(part, '68261528AR')
        self.assertIn('68261528AR / 68261528AE / 68261528AK', flag)
        # not on record, every read the same: nothing to flag
        part, flag = settle('68261528AE', '68261528AE', '68261528AE')
        self.assertEqual((part, flag), ('68261528AE', ''))
        # the flag reaches the New Shipments list
        rec2 = self.rec()
        ln = page_line('C400000009', '68261528AR')
        rec2._vote_part(ln, [ln, page_line('C400000009', '68261528AE'),
                             page_line('C400000009', '68261528AK')])
        rec2.pages = [total_page([ln])]
        rec2._resolve_lines()
        rec2._apply_scans()
        self.assertIn('read differently', rec2.r['new_shipments'][0]['flags'])

    def credit(self, ticket, amount, part='33333333AC'):
        return {'ticket': ticket, 'memo': 'CC00900009',
                'memo_date': datetime.date(2026, 10, 3), 'part': part,
                'claim': None, 'amount': amount, 'description': 'WHEEL'}

    def short_page(self, rec, total_texts):
        page = total_page([page_line('CB00800001', amount=100.0),
                           page_line('CW00100530', amount=50.0)],
                          total_texts)
        rec.pages = [page]
        page['check'] = self.check(rec, page)
        rec._resolve_lines()
        rec._apply_scans()
        return page

    def test_a_credited_ticket_dropped_from_a_scan(self):
        """A credit for a ticket that is not on the master used to be
        added with no Shipment ID and nothing said."""
        # the page is short by exactly one line and this amount
        rec = self.rec()
        self.short_page(rec, ['TOTAL 3 200.00', 'TOTAL 3 200.00'])
        rec._memos = (['m.pdf'], [('m.pdf', {
            'credits': [self.credit('CW00100951', 50.0)], 'memos': {},
            'warnings': [], 'dealer': None})])
        rec.apply_memos()
        got = rec.ledger.find('CW00100951')[0]
        self.assertEqual(got['shipment'], '2026-09-01 17:00.01')
        self.assertEqual(got['memo'], 'CC00900009')
        self.assertIn('given this page', rec.r['ocr_review'][-1]['issue'])
        self.assertEqual(rec.r['data_fixes'][-1]['field'], 'Shipment ID')
        # short, but not by this amount: flagged, Shipment ID left blank
        rec = self.rec()
        self.short_page(rec, ['TOTAL 3 400.00', 'TOTAL 3 400.00'])
        rec._memos = (['m.pdf'], [('m.pdf', {
            'credits': [self.credit('CW00100951', 50.0)], 'memos': {},
            'warnings': [], 'dealer': None})])
        rec.apply_memos()
        self.assertIsNone(rec.ledger.find('CW00100951')[0]['shipment'])
        self.assertIn('may have been dropped from a scan',
                      rec.r['warnings'][-1])
        self.assertIn('CW00100951 (w.pdf p.1)', rec.r['warnings'][-1])
        self.assertIn('put its Shipment ID on the master',
                      rec.r['ocr_review'][-1]['issue'])
        # a page that checks out, or a ticket from another number range,
        # says nothing
        for texts, ticket in ((['TOTAL 2 150.00'], 'CW00100951'),
                              (['TOTAL 3 200.00', 'TOTAL 3 200.00'],
                               'CW00190951'),
                              (['TOTAL 3 200.00', 'TOTAL 3 200.00'],
                               'C456000001')):
            rec = self.rec()
            self.short_page(rec, texts)
            rec._memos = (['m.pdf'], [('m.pdf', {
                'credits': [self.credit(ticket, 50.0)], 'memos': {},
                'warnings': [], 'dealer': None})])
            rec.apply_memos()
            self.assertIsNone(rec.ledger.find(ticket)[0]['shipment'])
            self.assertEqual(rec.r['warnings'], [], (texts, ticket))

    def test_the_dealer_code_printed_on_the_documents_wins(self):
        def run(entered, printed):
            rec = engine.Reconciler({'asof': datetime.date(2026, 10, 5),
                                     'threshold': 60, 'dealer': entered})
            rec.load_master(None, None)
            rec.pages = []
            for i, code in enumerate(printed):
                p = total_page([page_line(f'C40000000{i}')], number=i + 1)
                p['dealer'] = code
                rec.pages.append(p)
            rec.check_dealer()
            return rec
        rec = run('44558', ['45558'] * 5)
        self.assertEqual(rec.dealer, '45558')
        self.assertIn('set up as 44558', rec.r['warnings'][0])
        self.assertIn('print 45558', rec.r['warnings'][0])
        # agrees, or one stray misread among many: nothing said
        for printed in (['45558'] * 5, ['45558'] * 6 + ['45658']):
            rec = run('45558', printed)
            self.assertEqual((rec.dealer, rec.r['warnings']), ('45558', []))
        # one page is not enough to overrule what was entered
        rec = run('44558', ['45558'])
        self.assertEqual((rec.dealer, rec.r['warnings']), ('44558', []))
        # nothing entered: read from the documents, as before
        self.assertEqual(run('', ['45558'] * 3).dealer, '45558')

    def test_credit_requested_is_not_stamped_unless_asked(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        path = os.path.join(tmp, 'm.xlsx')
        make_master(path, [row(None, '2026-07-01 17:00.01', 'C400000001',
                               '11111111AA', 50)])
        for asked, want in ((False, None), (True, '2026-10-05')):
            out = os.path.join(tmp, f'out{asked}')
            os.makedirs(out)
            res = engine.run(
                {'master': (path, 'm.xlsx'), 'memos': [], 'shippers': [],
                 'dc': [], 'dc_text': ''},
                {'asof': datetime.date(2026, 10, 5), 'threshold': 60,
                 'mark_requested': asked}, out)
            self.assertEqual(len(res['credit_request']), 1)
            rows, _ = M.read_master(os.path.join(out, 'm.xlsx'))
            self.assertEqual(rows[0]['requested'], want)


class TestMasterLook(unittest.TestCase):
    """The master comes back looking like the workbook that went in:
    grid, fills and alignment (it used to come back bare)."""

    def test_cells_are_formatted(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        path = os.path.join(tmp, 'm.xlsx')
        M.write_master([
            row('CC00800001', None, 'C300000001', '11111111AA', 75, 254636),
            row('CC00800001', '2026-06-01 17:00.01', 'C300000002',
                '22222222AB', 120),
            row('CC00800002', '2026-06-02 17:00.01', 'C300000003',
                '33333333AC', 500),
            row(None, '2026-08-03 17:00.01', 'C400000001', 'U1111111AB',
                2500, req='2026-10-05'),
            row(None, '2026-08-04 17:00.01', 'C400000002', '44444444AD',
                50)], path)
        wb = load_workbook(path)
        ws = wb['Core Returns']

        def look(c):
            b = c.border
            return (c.font.name, c.font.sz, c.fill.fill_type,
                    c.fill.fgColor.rgb, c.alignment.horizontal,
                    c.alignment.vertical,
                    tuple((s.style, s.color.rgb if s.color else None)
                          for s in (b.left, b.right, b.top, b.bottom)))
        grid = (('thin', 'FFB7B7B7'),) * 4
        align = ['left', 'left', 'center', 'center', 'center', 'right',
                 'center', 'center']
        # credited rows banded white / gray (odd rows gray), unpaid yellow
        fills = {2: 'FFFFFFFF', 3: 'FFF2F2F2', 4: 'FFFFFFFF',
                 5: 'FFFFE699', 6: 'FFFFE699'}
        for r, color in fills.items():
            for col in range(1, 9):
                self.assertEqual(
                    look(ws.cell(r, col)),
                    ('Arial', 10, 'solid', color, align[col - 1], 'center',
                     grid), (r, col))
            self.assertEqual(ws.cell(r, 6).number_format, '"$"#,##0.00')
        for col in range(1, 9):
            h = ws.cell(1, col)
            self.assertTrue(h.font.b)
            self.assertEqual((h.font.color.rgb, h.fill.fgColor.rgb,
                              h.alignment.horizontal),
                             ('FFFFFFFF', 'FF1F4E78', 'center'))
            self.assertEqual(h.border.left.style, 'thin')
        self.assertEqual(ws.column_dimensions['D'].width, 13)
        # what the app already kept is still there
        self.assertEqual(ws.freeze_panes, 'A2')
        self.assertEqual(ws.auto_filter.ref, 'A1:H6')
        self.assertEqual(len(ws.conditional_formatting), 1)
        self.assertTrue(str(ws['G5'].value).startswith('=IF(B5=""'))
        self.assertEqual(ws['H5'].value, '2026-10-05')
        # Unpaid Cores: plain. Header, grid, Arial 10, no fills
        up = wb['Unpaid Cores']
        self.assertEqual([up.cell(r, 3).value for r in (2, 3)],
                         ['C400000001', 'C400000002'])
        for r in (2, 3):
            for col in range(1, 9):
                font, size, fill, _, horiz, vert, border = look(
                    up.cell(r, col))
                self.assertEqual((font, size, fill, horiz, vert, border),
                                 ('Arial', 10, None, align[col - 1],
                                  'center', grid), (r, col))
        self.assertEqual(up['A1'].fill.fgColor.rgb, 'FF1F4E78')
        # and it reads back the same
        rows, _ = M.read_master(path)
        self.assertEqual([r['ticket'] for r in rows],
                         ['C300000001', 'C300000002', 'C300000003',
                          'C400000001', 'C400000002'])
