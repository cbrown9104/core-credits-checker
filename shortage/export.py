"""The Excel file of an invoice's lines: one sheet of the short lines
(the claim worksheet) and one sheet of every line."""
import io

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from .match import HOW_TEXT

RESULT = {'short': 'Short', 'partial': 'Partial', 'received': 'Received',
          'core': 'Core charge', 'ignored': 'Ignored part',
          'none': 'No charge'}
STATUS = {'': '', 'review': 'Review', 'filed': 'Filed', 'paid': 'Paid',
          'denied': 'Denied', 'found': 'Found'}
COLUMNS = (
    ('Invoice', 20), ('Invoice date', 13), ('Deadline', 13), ('Result', 12),
    ('Part', 15), ('Description', 16), ('Order', 10), ('Type', 6),
    ('Order date', 13), ('PDC', 8), ('Shipper', 11), ('Billed qty', 10),
    ('Received qty', 12), ('Short qty', 10), ('Billed each', 12),
    ('Short value', 13), ('Status', 10), ('Claim number', 16), ('Note', 34),
    ('How it matched', 44), ('Printed on the invoice', 50))
MONEY_COLS, DATE_COLS = (15, 16), (2, 3, 9)


def shown_part(part):
    """The part number as the DMS and the counter know it: the invoice
    pads short numbers with zeros in front (04429825)."""
    return part.lstrip('0') or part


def _row(l):
    short = max(l['qty'] - l['received'], 0) if l['state'] in (
        'short', 'partial') else 0
    each = l['net'] / l['qty'] if l['qty'] else l['unit_price']
    how = ' '.join(x for x in (HOW_TEXT.get(l['how'], ''), l['hint']) if x)
    return [l['number'], l['invoice_date'], l['deadline'],
            RESULT.get(l['state'], l['state']), shown_part(l['part']),
            l['description'], l['order_no'], l['order_type'],
            l['order_date'], l['pdc'], l['shipper'], l['qty'],
            l['received'], short, round(float(each), 2),
            round(float(short * each), 2),
            STATUS.get(l['status'], l['status']), l['claim_no'], l['note'],
            how, l['detail']]


def _sheet(ws, lines):
    ws.append([c[0] for c in COLUMNS])
    for cell in ws[1]:
        cell.font = Font(bold=True, color='FFFFFF')
        cell.fill = PatternFill('solid', fgColor='1F3864')
        cell.alignment = Alignment(vertical='center')
    for i, (_, width) in enumerate(COLUMNS, 1):
        ws.column_dimensions[get_column_letter(i)].width = width
    for l in lines:
        ws.append(_row(l))
    for row in ws.iter_rows(min_row=2):
        for i in MONEY_COLS:
            row[i - 1].number_format = '#,##0.00'
        for i in DATE_COLS:
            row[i - 1].number_format = 'mm/dd/yyyy'
    ws.freeze_panes = 'A2'
    if lines:
        ws.auto_filter.ref = ws.dimensions


def workbook(lines):
    """Bytes of the .xlsx for these lines (rows of store.lines_for)."""
    wb = Workbook()
    ws = wb.active
    ws.title = 'Shortages'
    _sheet(ws, [l for l in lines if l['state'] in ('short', 'partial')])
    _sheet(wb.create_sheet('All lines'), lines)
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()
