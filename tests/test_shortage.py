"""Tests for Shortage Check: reading a Mopar parts invoice and the DMS
parts transaction file, matching billed to received, and the store pages.

The first three classes need nothing but Python. The last one needs the
scratch database of test_accounts (TEST_DATABASE_URL) and poppler's
pdftotext. All data is made up.
"""
import datetime
import io
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(
    __file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from shortage import invoice, match, receipts  # noqa: E402
from test_accounts import AccountsCase  # noqa: E402

D = datetime.date
NUMBER = '09999000W 00000042'

HEAD = '''FCA US LLC
1000 CHRYSLER DRIVE
AUBURN HILLS, MI 48326

                            {title}

         BILL TO: 12345000                    SHIP TO: 12345000
         EXAMPLE MOTORS                       EXAMPLE MOTORS
         1 MAIN ST                            1 MAIN ST
         ANYTOWN                              ANYTOWN
         TX USA 70000-0000                    TX USA 70000-0000
         SD NO: 01
INVOICE NUMBER: {number}
INVOICE DATE : {date}
PAGE          :    {page}

LINE PART       DESC      QTY UNIT PRC GROSS        EXTD DISCOUNT   NET       P
NO   NBR                                 AMOUNT     DC % AMOUNT     AMOUNT    S
'''


def head(page, number=NUMBER, date='NOVEMBER 07, 2026',
         title='MOPAR PARTS INVOICE'):
    return HEAD.format(page=page, number=number, date=date, title=title)


def block(pdc, shipper, order, otype, date):
    return f'{pdc}00-{shipper} ORD#: {order} O/T:{otype} DATE: {date}\n'


def item(part, desc, qty, unit, dc=12, ps='B'):
    gross = qty * unit
    return (f'   1 {part:<10} {desc:<8} {qty:>5} {unit:>9.2f} {gross:>10.2f} '
            f'{dc} 0        .00 {gross:>9.2f} {ps}\n')


def subtotal(v):
    return f'                        SUBTOTAL  {v:>12.2f}          {v:>12.2f}\n\n'


def summary(v):
    return ('\nSUMMARY:\n-------\n'
            f'    TOTAL GROSS AMOUNT                      {v:>12.2f}\n'
            '    DISCOUNTS EARNED                                 .00\n\n'
            f'    NET INVOICE AMOUNT                      {v:>12.2f}\n')


PAGE_1 = (
    head(1) +
    block('09999', '5000001', 'AR1001', 'A', '2026-10-26') +
    item('11111111AA', 'BATTERY', 18, 10.00, ps='H') +
    '     **SUPPLIER PACKING SLIP # : 730-1\n' +
    subtotal(180.00) +
    block('09999', '5000002', 'EA1030', 'E', '2026-10-30') +
    item('22222222AB', 'OIL', 12, 10.00, ps='D') +
    '     EXAMPLE PARTS DEPT\n' +
    item('22222222AB', 'OIL', 12, 10.00, ps='D') +
    '     EXAMPLE PARTS DEPT\n')
PAGE_2 = (
    head(2) +
    '     **SUPPLIER PACKING SLIP # : X3\n\n' +
    block('09999', '5000002', 'EA1030', 'E', '2026-10-30') +
    item('22222222AB', 'OIL', 12, 10.00, ps='D') +
    subtotal(360.00) +
    block('09998', '5000003', '551030', 'S', '2026-10-30') +
    item('03333333AC', 'WHEEL', 1, 692.00, dc=13) +
    '     EXAMPLE COLLISION\n'
    '     THIS PART HAS AN ASSOCIATED DEPOSIT CHARGE OF $          50.00\n'
    '     ARC31101   DEPOSIT PART VALUES           50.00                     50.00\n' +
    subtotal(742.00) +
    block('09999', '5000004', 'AR1002', 'A', '2026-11-03') +
    item('R4444444AC', 'VALVE', 2, 300.00, ps='A') +
    item('U4444444AC', 'VALVE', 2, 100.00, dc=99, ps='N') +
    subtotal(800.00))
PAGE_3 = head(3) + summary(2082.00)
FULL = PAGE_1 + PAGE_2 + PAGE_3
CUT_OFF = PAGE_1

CSV_HEAD = ('Invoice,Trx_Date,TRANS-TIME,PartNumber,Description,Qty,'
            'Total_Cost,Total_Sale,Trx_Type,Trx_Code,Shipping_Number,'
            'Vendor_Number,Customer_Number,DEPT,RECID,Core,BIN\n')


def csv_row(order, date, part, qty, kind='PUR', shipper='', recid=None,
            code='NO'):
    csv_row.n += 1
    recid = recid or f'100-{csv_row.n}'
    return (f'{order},{date},10:00,{part},THING,{qty},1.00,.00,{kind},'
            f'{code},{shipper},5000,,,{recid},,A1\n')


csv_row.n = 0

SCANS = (
    CSV_HEAD +
    csv_row('AR1001', '11/02/26', 'CH11111111-AA', 18, shipper='5000001') +
    csv_row('EA1030', '11/02/26', 'CH22222222-AB', 12, shipper='5000002') +
    csv_row('EA1030', '11/03/26', 'CH22222222-AB', 12, shipper='5000002') +
    csv_row('551030', '11/03/26', 'CH3333333-AC', 1, shipper='5000003') +
    csv_row('AR1002', '11/04/26', 'CHR4444444-AC', 2, shipper='5000004') +
    csv_row('700001', '11/02/26', 'CH55555555-AA', 1, kind='SLS') +
    csv_row('', '11/02/26', 'CH66666666-AA', 0, kind='ADD') +
    csv_row('RO1', '11/02/26', 'CH77777777-AA', -1, code='IO') +
    csv_row('RO1', '11/02/26', 'CH77777777-AA', 1, code='IO')).encode()


def line(i, part, qty, shipper='', order='', kind='part', order_date=None,
         inv=D(2026, 11, 7)):
    return {'id': i, 'part': part, 'qty': qty, 'shipper': shipper,
            'order_no': order, 'kind': kind, 'order_date': order_date,
            'sort': (inv, 'W1', i)}


def receipt(part, qty, shipper='', order='', day=D(2026, 11, 3)):
    return {'part': receipts.part_key(part), 'qty': qty, 'shipper': shipper,
            'order_no': order, 'day': day, 'part_raw': part}


class TestInvoice(unittest.TestCase):
    def test_a_complete_invoice_is_read_and_ties_to_its_total(self):
        found = invoice.parse_invoice_text(FULL)
        self.assertEqual(len(found), 1)
        inv = found[0]
        self.assertEqual(inv['number'], NUMBER)
        self.assertEqual(inv['title'], 'MOPAR PARTS INVOICE')
        self.assertEqual(inv['date'], D(2026, 11, 7))
        self.assertEqual(inv['facing_pdc'], '09999')
        self.assertEqual(inv['pages'], 3)
        self.assertEqual([(l['shipper'], l['order_no'], l['part'], l['qty'])
                          for l in inv['lines']], [
            ('5000001', 'AR1001', '11111111AA', 18),
            ('5000002', 'EA1030', '22222222AB', 12),
            ('5000002', 'EA1030', '22222222AB', 12),
            ('5000002', 'EA1030', '22222222AB', 12),
            ('5000003', '551030', '03333333AC', 1),
            ('5000004', 'AR1002', 'R4444444AC', 2),
            ('5000004', 'AR1002', 'U4444444AC', 2)])
        first = inv['lines'][0]
        self.assertEqual((first['pdc'], first['order_type'],
                          first['order_date'], float(first['unit_price']),
                          float(first['net'])),
                         ('09999', 'A', D(2026, 10, 26), 10.0, 180.0))
        self.assertIn('SUPPLIER PACKING SLIP # : 730-1', first['detail'])
        # the line before the page break keeps the note printed after it
        self.assertIn('X3', inv['lines'][2]['detail'])
        self.assertEqual(inv['lines'][4]['pdc'], '09998')
        self.assertEqual(float(inv['deposits']), 50.0)
        self.assertEqual([l['kind'] for l in inv['lines']][-2:],
                         ['part', 'core'])
        self.assertEqual(float(inv['net_total']), 2082.0)
        self.assertEqual(float(inv['lines_net']), 2082.0)
        self.assertTrue(inv['complete'])
        self.assertEqual(inv['warnings'], [])

    def test_a_copy_with_no_summary_page_is_cut_off(self):
        inv = invoice.parse_invoice_text(CUT_OFF)[0]
        self.assertEqual(len(inv['lines']), 3)
        self.assertFalse(inv['complete'])
        self.assertIsNone(inv['net_total'])
        self.assertIn('no SUMMARY page', inv['warnings'][0])

    def test_lines_that_do_not_add_up_are_not_complete(self):
        short = PAGE_1 + head(2) + summary(999.00)
        inv = invoice.parse_invoice_text(short)[0]
        self.assertFalse(inv['complete'])
        self.assertIn('$420.00', inv['warnings'][0])
        self.assertIn('$999.00', inv['warnings'][0])

    def test_two_invoices_in_one_file_are_kept_apart(self):
        other = '09999000WA00000007'
        second = (head(1, other, title='AER INVOICE') +
                  block('09999', '6000001', 'AR2001', 'A', '2026-11-04') +
                  item('88888888AA', 'PUMP', 1, 25.00) + subtotal(25.00) +
                  head(2, other, title='AER INVOICE') + summary(25.00))
        found = invoice.parse_invoice_text(FULL + second)
        self.assertEqual([(i['number'], i['title'], len(i['lines']),
                           i['complete']) for i in found],
                         [(NUMBER, 'MOPAR PARTS INVOICE', 7, True),
                          (other, 'AER INVOICE', 1, True)])

    def test_text_that_is_not_an_invoice_gives_nothing(self):
        self.assertEqual(invoice.parse_invoice_text('CREDIT MEMO\n1 2 3'), [])


class TestReceipts(unittest.TestCase):
    def test_purchase_rows_become_receipts(self):
        got = receipts.read_receipts(SCANS, 'CURRENT SCANS.csv')
        self.assertEqual(got['prefix'], 'CH')
        self.assertEqual(got['rows'], 9)
        self.assertEqual((got['first'], got['last']),
                         (D(2026, 11, 2), D(2026, 11, 4)))
        self.assertEqual(got['days'][D(2026, 11, 2)], (6, 4))
        rows = got['receipts']
        self.assertEqual(len(rows), 7)      # the SLS and ADD rows are not
        self.assertEqual(
            (rows[0]['part'], rows[0]['order_no'], rows[0]['shipper'],
             rows[0]['qty'], rows[0]['day']),
            ('11111111AA', 'AR1001', '5000001', 18, D(2026, 11, 2)))
        self.assertEqual(len({r['rec_key'] for r in rows}), 7)

    def test_part_numbers_compare_equal_across_the_two_files(self):
        for dms, billed in (('CH68524020-AB', '68524020AB'),
                            ('CH4877184-AA', '04877184AA'),
                            ('CHR8448028-AC', 'R8448028AC'),
                            ('CHBBH7A001-AA', 'BBH7A001AA')):
            self.assertEqual(receipts.part_key(dms, 'CH'),
                             receipts.part_key(billed), dms)

    def test_a_file_without_the_columns_is_refused_with_the_reason(self):
        with self.assertRaises(receipts.NotReceipts) as e:
            receipts.read_receipts(b'Part,Qty\nA,1\n', 'x.csv')
        self.assertIn('shippingnumber', str(e.exception))
        with self.assertRaises(receipts.NotReceipts):
            receipts.read_receipts(b'', 'x.csv')

    def test_an_excel_file_reads_the_same(self):
        from openpyxl import Workbook
        wb = Workbook()
        for row in SCANS.decode().splitlines():
            wb.active.append(row.split(','))
        out = io.BytesIO()
        wb.save(out)
        got = receipts.read_receipts(out.getvalue(), 'scans.xlsx')
        self.assertEqual(len(got['receipts']), 7)
        self.assertEqual(got['receipts'][3]['part'], '3333333AC')

    def test_a_file_with_no_record_ids_still_gets_stable_keys(self):
        body = ('Invoice,Trx_Date,PartNumber,Qty,Trx_Type,Shipping_Number\n'
                'AR1,11/02/26,68000001-AA,1,PUR,5000001\n'
                'AR1,11/02/26,68000002-AA,1,PUR,5000001\n').encode()
        a = receipts.read_receipts(body)['receipts']
        b = receipts.read_receipts(body)['receipts']
        self.assertEqual([r['rec_key'] for r in a], [r['rec_key'] for r in b])
        self.assertEqual(len({r['rec_key'] for r in a}), 2)
        self.assertEqual(a[0]['part'], '68000001AA')   # no make code here


class TestMatch(unittest.TestCase):
    def test_shipper_then_order_number(self):
        got = match.match(
            [line(1, '11111111AA', 2, '5000001', 'AR1'),
             line(2, '22222222AA', 1, '5000002', 'AR2'),
             line(3, '33333333AA', 1, '5000003', 'AR3')],
            [receipt('11111111-AA', 2, '5000001', 'OTHER'),
             receipt('22222222-AA', 1, '', 'AR2')])
        self.assertEqual(got[1][:2], (2, match.BY_SHIPPER))
        self.assertEqual(got[2][:2], (1, match.BY_ORDER))
        self.assertEqual(got[3], (0, '', ''))

    def test_a_receipt_is_never_counted_twice(self):
        lines = [line(i, '22222222AB', 12, '5000002', 'EA1030')
                 for i in (1, 2, 3)]
        got = match.match(lines, [receipt('22222222AB', 12, '5000002',
                                          'EA1030')] * 2)
        self.assertEqual([got[i][0] for i in (1, 2, 3)], [12, 12, 0])
        # a second invoice for the same order cannot use them again
        later = line(9, '22222222AB', 12, '5000002', 'EA1030',
                     inv=D(2026, 11, 14))
        got = match.match(lines + [later],
                          [receipt('22222222AB', 12, '5000002',
                                   'EA1030')] * 3)
        self.assertEqual(got[9][0], 0)
        self.assertEqual(got[3][0], 12)

    def test_a_partial_receipt(self):
        got = match.match([line(1, '11111111AA', 68, '5000001', 'AR1')],
                          [receipt('11111111AA', 60, '5000001', 'AR1')])
        self.assertEqual(got[1][0], 60)

    def test_a_receipt_and_its_reversal_cancel(self):
        got = match.match(
            [line(1, '11111111AA', 1, '5000001', 'AR1')],
            [receipt('11111111AA', 1, '5000001', 'AR1'),
             receipt('11111111AA', -1, '5000001', 'AR1')])
        self.assertEqual(got[1][0], 0)

    def test_leading_zeros_do_not_make_a_shortage(self):
        got = match.match([line(1, '04877184AA', 3, '5000005', 'AR1003')],
                          [receipt('4877184-AA', 3, '5000005', 'AR1003')])
        self.assertEqual(got[1][0], 3)

    def test_a_superseded_suffix_counts_and_says_so(self):
        got = match.match(
            [line(1, '68381242AD', 1, '5000001', 'AR1'),
             line(2, '68381242AC', 1, '5000001', 'AR1')],
            [receipt('68381242-AC', 1, '5000001', 'AR1'),
             receipt('68381242-AE', 1, '5000001', 'AR1')])
        # the exact match is settled first; the near one takes what is left
        self.assertEqual(got[2][:2], (1, match.BY_SHIPPER))
        self.assertEqual(got[1][:2], (1, match.NEAR_PART))
        self.assertIn('68381242-AE', got[1][2])

    def test_a_close_order_number_is_a_hint_not_a_match(self):
        got = match.match(
            [line(1, '11111111AA', 1, '5000001', 'EA1113',
                  order_date=D(2026, 11, 13))],
            [receipt('11111111AA', 1, '5000009', 'EA1116',
                     day=D(2026, 11, 16)),
             receipt('11111111AA', 1, '5000008', 'EA1105',
                     day=D(2026, 11, 5))])
        received, how, hint = got[1]
        self.assertEqual((received, how), (0, ''))
        self.assertIn('order EA1116', hint)
        self.assertIn('on 11/16', hint)
        # received before the order was placed: it cannot be this one
        self.assertNotIn('EA1105', hint)

    def test_core_charges_and_ignored_parts_are_left_out(self):
        got = match.match(
            [line(1, 'U4444444AC', 2, '5000004', 'AR1', kind='core'),
             line(2, '68-523007AA', 5, '5000004', 'AR1'),
             line(3, '11111111AA', 0, '5000004', 'AR1')],
            [], ignored=['68523007aa'])
        self.assertEqual(got, {})


def pdf(text):
    """A PDF with a text layer that reads back as the given text."""
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas
    out = io.BytesIO()
    c = canvas.Canvas(out, pagesize=letter)
    pages = ('\n' + text).split('\nFCA US LLC\n')[1:]
    for page in pages:
        y = 770
        c.setFont('Courier', 7)
        for row in ('FCA US LLC\n' + page).splitlines():
            c.drawString(20, y, row)
            y -= 9
        c.showPage()
    c.save()
    return out.getvalue()


class TestPages(AccountsCase):
    def send(self, c, *files):
        token, store = self.guard(c)
        return c.post(
            '/api/shortages/upload', content_type='multipart/form-data',
            data={'files': [(io.BytesIO(body), name)
                            for name, body in files]},
            headers={'X-CSRF-Token': token, 'X-Store-Id': store})

    def change(self, c, **body):
        token, store = self.guard(c)
        return c.post('/api/shortages/lines', json=body, headers={
            'X-CSRF-Token': token, 'X-Store-Id': store})

    def lines(self, store):
        from accounts import db
        return db.rows('SELECT * FROM sc_lines WHERE store_id = %s ORDER '
                       'BY seq', (store['id'],))

    def test_a_week_of_files_from_upload_to_claim(self):
        from accounts import db
        store = self.make_store('Shortage Motors', 'amy@short.test')
        amy = self.sign_in('amy@short.test')
        page = amy.get('/shortages')
        self.assertEqual(page.status_code, 200)
        self.assertIn(b'No invoice is loaded yet', page.data)
        self.assertIn(b'href="/shortages">Shortage Check', page.data)

        r = self.send(amy, ('W42.pdf', pdf(FULL)), ('scans.csv', SCANS),
                      ('notes.txt', b'hello'))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        answers = {a['name']: a for a in r.get_json()['files']}
        self.assertEqual(answers['W42.pdf']['result'], 'loaded')
        self.assertIn('7 lines', answers['W42.pdf']['message'])
        self.assertIn('$2,082.00', answers['W42.pdf']['message'])
        self.assertEqual(answers['scans.csv']['result'], 'loaded')
        self.assertIn('7 new receipts', answers['scans.csv']['message'])
        self.assertEqual(answers['notes.txt']['result'], 'refused')
        self.assertIn('no column', answers['notes.txt']['message'])

        got = self.lines(store)
        self.assertEqual([l['state'] for l in got], [
            'received', 'received', 'received', 'short', 'received',
            'received', 'core'])
        self.assertEqual([l['received'] for l in got],
                         [18, 12, 12, 0, 1, 2, 0])
        # the facing PDC gets 7 days from the invoice date, the rest 14
        self.assertEqual(got[0]['deadline'], D(2026, 11, 14))
        self.assertEqual(got[4]['deadline'], D(2026, 11, 21))

        page = amy.get('/shortages').get_data(as_text=True)
        self.assertIn(NUMBER, page)
        self.assertIn('$120.00', page)          # the one short line
        self.assertIn('Not loaded', page)       # notes.txt, with its reason
        inv = db.one('SELECT * FROM sc_invoices WHERE store_id = %s',
                     (store['id'],))
        self.assertTrue(inv['complete'])
        view = amy.get(f'/shortages/invoice/{inv["id"]}')
        self.assertEqual(view.status_code, 200)
        self.assertIn(b'22222222AB', view.data)
        self.assertIn(b'The lines add up to the invoice total', view.data)

        # the same files again change nothing
        r = self.send(amy, ('W42.pdf', pdf(FULL)), ('scans.csv', SCANS))
        self.assertEqual([a['result'] for a in r.get_json()['files']],
                         ['already', 'already'])
        self.assertEqual(len(self.lines(store)), 7)

        # a claim number files the line; the open list keeps it, the
        # front page counts it as filed
        short = got[3]['id']
        r = self.change(amy, ids=[short], claim_no=' 046770  3B ')
        self.assertEqual(r.get_json(), {'updated': 1})
        now = self.lines(store)[3]
        self.assertEqual((now['status'], now['claim_no'],
                          now['changed_email']),
                         ('filed', '046770 3B', 'amy@short.test'))
        self.assertEqual(self.change(amy, ids=[short], status='nope')
                         .status_code, 400)
        self.assertEqual(self.change(amy, ids=[], status='paid')
                         .status_code, 400)
        self.assertEqual(self.change(amy, ids=[short], note='x' * 600)
                         .get_json(), {'updated': 1})
        self.assertEqual(len(self.lines(store)[3]['note']), 500)

        # the receipt arrives: the line is received, the claim stays on it
        more = (CSV_HEAD + csv_row('EA1030', '11/09/26', 'CH22222222-AB',
                                   12, shipper='5000002')).encode()
        r = self.send(amy, ('scans2.csv', more))
        self.assertIn('1 new receipts', r.get_json()['files'][0]['message'])
        now = self.lines(store)[3]
        self.assertEqual((now['state'], now['received'], now['status']),
                         ('received', 12, 'filed'))

        book = amy.get(f'/shortages/invoice/{inv["id"]}/export')
        self.assertEqual(book.status_code, 200)
        from openpyxl import load_workbook
        wb = load_workbook(io.BytesIO(book.data))
        self.assertEqual(wb.sheetnames, ['Shortages', 'All lines'])
        self.assertEqual(wb['All lines'].max_row, 8)
        self.assertEqual(wb['Shortages'].max_row, 1)
        self.assertEqual(amy.get('/shortages/open').status_code, 200)
        self.assertEqual(amy.get('/shortages/open/export').status_code, 200)

    def test_a_fuller_copy_replaces_a_cut_off_one_and_keeps_the_notes(self):
        from accounts import db
        store = self.make_store('Cut Off Motors', 'cal@cut.test')
        cal = self.sign_in('cal@cut.test')
        r = self.send(cal, ('part.pdf', pdf(CUT_OFF)))
        first = r.get_json()['files'][0]
        self.assertEqual(first['result'], 'cut_off')
        self.assertIn('no SUMMARY page', first['message'])
        got = self.lines(store)
        self.assertEqual(len(got), 3)
        self.assertIn(b'Not complete', cal.get('/shortages').data)
        self.change(cal, ids=[got[2]['id']], status='review',
                    note='counted the pallet twice')

        again = self.send(cal, ('part.pdf', pdf(CUT_OFF))).get_json()
        self.assertEqual(again['files'][0]['result'], 'already')
        self.assertIn('cut off', again['files'][0]['message'])

        full = self.send(cal, ('full.pdf', pdf(FULL))).get_json()
        self.assertEqual(full['files'][0]['result'], 'loaded')
        self.assertIn('took the place', full['files'][0]['message'])
        got = self.lines(store)
        self.assertEqual(len(got), 7)
        self.assertEqual((got[2]['status'], got[2]['note']),
                         ('review', 'counted the pallet twice'))
        self.assertEqual(db.one('SELECT count(*) AS n FROM sc_invoices '
                                'WHERE store_id = %s',
                                (store['id'],))['n'], 1)
        # a complete invoice is never replaced
        back = self.send(cal, ('part.pdf', pdf(CUT_OFF))).get_json()
        self.assertEqual(back['files'][0]['result'], 'already')
        self.assertEqual(len(self.lines(store)), 7)

    def test_one_store_never_sees_or_changes_another_stores_lines(self):
        from accounts import db
        mine = self.make_store('Mine Motors', 'me@mine.test')
        other = self.make_store('Other Motors', 'you@other.test')
        me, you = self.sign_in('me@mine.test'), self.sign_in('you@other.test')
        self.send(me, ('W42.pdf', pdf(FULL)), ('scans.csv', SCANS))
        inv = db.one('SELECT id FROM sc_invoices WHERE store_id = %s',
                     (mine['id'],))['id']
        ids = [l['id'] for l in self.lines(mine)]

        page = you.get('/shortages')
        self.assertNotIn(NUMBER.encode(), page.data)
        self.assertEqual(you.get(f'/shortages/invoice/{inv}').status_code,
                         404)
        self.assertEqual(you.get(f'/shortages/invoice/{inv}/export')
                         .status_code, 404)
        self.assertEqual(self.change(you, ids=ids, status='paid')
                         .get_json(), {'updated': 0})
        self.assertEqual(self.post(you, f'/shortages/invoice/{inv}/delete')
                         .status_code, 404)
        self.assertEqual(self.post(you, f'/shortages/invoice/{inv}/done',
                                   {'done': '1'}).status_code, 404)
        self.assertEqual({l['status'] for l in self.lines(mine)}, {''})
        # the other store's own copy of the same invoice is its own
        self.send(you, ('W42.pdf', pdf(FULL)))
        self.assertEqual(db.one(
            'SELECT count(*) AS n FROM sc_invoices WHERE store_id IN (%s, '
            '%s)', (mine['id'], other['id']))['n'], 2)
        # it loaded no receipts, and used none of mine
        self.assertEqual({l['state'] for l in self.lines(other)
                          if l['kind'] == 'part'}, {'short'})

    def test_settings_are_for_admins_and_move_the_deadlines(self):
        store = self.make_store('Settings Motors', 'ann@set.test')
        ann = self.sign_in('ann@set.test')
        self.post(ann, '/users/add', {'email': 'bob@set.test',
                                      'role': 'user'})
        bob = self.sign_in('bob@set.test')
        self.send(bob, ('W42.pdf', pdf(FULL)), ('scans.csv', SCANS))
        self.assertNotIn(b'Save settings', bob.get('/shortages').data)
        self.assertIn(b'Save settings', ann.get('/shortages').data)
        form = {'default_days': '10', 'pdc_days': '09999 = 5\n09998=21',
                'ignored_parts': '22222222ab, PM1262'}
        self.assertEqual(self.post(bob, '/shortages/settings', form)
                         .status_code, 403)
        self.assertEqual(self.post(ann, '/shortages/settings', form)
                         .status_code, 302)
        got = self.lines(store)
        self.assertEqual(got[0]['deadline'], D(2026, 11, 12))
        self.assertEqual(got[4]['deadline'], D(2026, 11, 28))
        self.assertEqual([l['state'] for l in got][1:4], ['ignored'] * 3)
        page = ann.get('/shortages').get_data(as_text=True)
        self.assertIn('09998 = 21', page)
        self.assertIn('22222222AB, PM1262', page)

        bad = self.post(ann, '/shortages/settings', dict(
            form, pdc_days='3181 = 7'), follow_redirects=True)
        self.assertIn(b'is not a PDC code', bad.data)
        self.assertEqual(self.lines(store)[0]['deadline'], D(2026, 11, 12))

        # only an admin removes an invoice; anyone marks it done
        from accounts import db
        inv = db.one('SELECT id FROM sc_invoices WHERE store_id = %s',
                     (store['id'],))['id']
        self.assertEqual(self.post(bob, f'/shortages/invoice/{inv}/delete')
                         .status_code, 403)
        self.assertEqual(self.post(bob, f'/shortages/invoice/{inv}/done',
                                   {'done': '1'}).status_code, 302)
        self.assertIn(b'Reopen', bob.get(f'/shortages/invoice/{inv}').data)
        self.assertEqual(self.post(ann, f'/shortages/invoice/{inv}/delete')
                         .status_code, 302)
        self.assertEqual(self.lines(store), [])

    def test_signed_out_and_wrong_files(self):
        self.make_store('Door Motors', 'dee@door.test')
        out = self.client()
        r = out.get('/shortages')
        self.assertEqual(r.status_code, 302)
        self.assertIn('/login', r.headers['Location'])
        self.assertEqual(out.post('/api/shortages/upload').status_code, 401)
        dee = self.sign_in('dee@door.test')
        # no page token: refused before anything is read
        self.assertEqual(dee.post('/api/shortages/lines',
                                  json={'ids': [1], 'status': 'paid'})
                         .status_code, 400)
        self.assertEqual(self.send(dee).status_code, 400)
        r = self.send(dee, ('memo.pdf', pdf(
            'FCA US LLC\n\n      CREDIT MEMO\n\nnothing here\n')))
        answer = r.get_json()['files'][0]
        self.assertEqual(answer['result'], 'refused')
        self.assertIn('No INVOICE NUMBER', answer['message'])


class TestAccountsOff(unittest.TestCase):
    def test_the_tool_is_not_there_without_accounts(self):
        saved = os.environ.pop('DATABASE_URL', None)
        try:
            import app as webapp
            c = webapp.app.test_client()
            self.assertEqual(c.get('/shortages').status_code, 404)
            self.assertEqual(c.get('/shortages/open').status_code, 404)
            self.assertEqual(c.post('/api/shortages/upload').status_code,
                             404)
            self.assertNotIn(b'Shortage Check', c.get('/').data)
        finally:
            if saved is not None:
                os.environ['DATABASE_URL'] = saved


if __name__ == '__main__':
    unittest.main()
