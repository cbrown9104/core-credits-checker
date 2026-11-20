"""Read a Mopar parts invoice: the weekly W or WA document a dealer
saves from DealerCONNECT as a PDF with a text layer.

The invoice is a run of shipper blocks. Each block starts with a line
such as

    0999900-5000002 ORD#: EA1030 O/T:E DATE: 2026-10-30

(PDC 09999, shipper 5000002, order EA1030, order type E), lists its part
lines, and ends with SUBTOTAL. The last page has SUMMARY with the
invoice totals. A PDF with no SUMMARY page is a cut-off copy.
"""
import datetime
import re
from decimal import Decimal

from recon import ocr

_MONEY = r'(-?[\d,]*\.\d{2}-?)'
NUMBER_RE = re.compile(r'^\s*INVOICE NUMBER\s*:\s*(\S.*?)\s*$')
DATE_RE = re.compile(r'^\s*INVOICE DATE\s*:\s*(\S.*?)\s*$')
PAGE_RE = re.compile(r'^\s*PAGE\s*:\s*(\d+)\s*$')
TITLE_RE = re.compile(r'^\s{4,}([A-Z][A-Z ]*INVOICE)\s*$')
HEAD_START_RE = re.compile(r'^\s*FCA US LLC\s*$')
HEAD_END_RE = re.compile(r'^\s*NO\s+NBR\b')
BLOCK_RE = re.compile(
    r'^\s*(\d{5})\d{2}-(\d{5,9})\s+ORD#:\s*(\S*)\s+O/T:\s*([A-Z0-9]?)\s*'
    r'DATE:\s*(\d{4}-\d{2}-\d{2})?\s*$')
ITEM_RE = re.compile(
    r'^\s*(\d{1,4})\s+(\S{3,15})\s+(.*?)\s*(-?\d{1,6})\s+' + _MONEY +
    r'\s+' + _MONEY + r'\s+(\d{1,2})\s+(\d{1,3})\s+' + _MONEY + r'\s+' +
    _MONEY + r'\s+([A-Z0-9])\s*$')
DEPOSIT_RE = re.compile(r'^\s*ARC\w*\s+DEPOSIT PART VALUES\s+' + _MONEY +
                        r'\s+' + _MONEY + r'\s*$')
SUBTOTAL_RE = re.compile(r'^\s*SUBTOTAL\s+' + _MONEY + r'\s+' + _MONEY +
                         r'\s*$')
SUMMARY_RE = re.compile(r'^\s*SUMMARY\s*:?\s*$')
TOTAL_RE = re.compile(r'^\s*(TOTAL GROSS AMOUNT|NET INVOICE AMOUNT)\s+' +
                      _MONEY + r'\s*$')
# A core charge is billed as its own line: U + the part's digits.
CORE_RE = re.compile(r'^U\d{6,9}[A-Z]{1,2}$')
# How far a page heading may run before the column titles end it.
HEAD_MAX_LINES = 30
DETAIL_MAX = 400
CENT = Decimal('0.01')


class NotAnInvoice(Exception):
    """The file is not a Mopar parts invoice this reader understands."""


def _money(token):
    s = token.replace(',', '').strip()
    neg = s.startswith('-') or s.endswith('-')
    s = s.strip('-') or '0'
    v = Decimal(s)
    return -v if neg else v


def _date(text):
    try:
        return datetime.datetime.strptime(
            re.sub(r'\s+', ' ', text.strip().title()), '%B %d, %Y').date()
    except ValueError:
        return None


def _iso(text):
    try:
        return datetime.date.fromisoformat(text) if text else None
    except ValueError:
        return None


def clean_number(raw):
    """'03181000W  00509357' -> '03181000W 00509357'."""
    return re.sub(r'\s+', ' ', (raw or '').strip().upper())


def _new_invoice(number):
    m = re.match(r'^(\d{5})', number)
    return {'number': number, 'title': '', 'date': None, 'pages': 0,
            'facing_pdc': m.group(1) if m else '',
            'lines': [], 'deposits': Decimal('0'), 'gross_total': None,
            'net_total': None, 'has_summary': False, 'warnings': []}


def _finish(inv):
    lines = inv['lines']
    inv['lines_net'] = sum((l['net'] for l in lines), Decimal('0')) + \
        inv['deposits']
    inv['lines_gross'] = sum((l['gross'] for l in lines), Decimal('0')) + \
        inv['deposits']
    pairs = [(inv['lines_net'], inv['net_total']),
             (inv['lines_gross'], inv['gross_total'])]
    printed = [(a, b) for a, b in pairs if b is not None]
    inv['complete'] = bool(printed) and any(abs(a - b) <= CENT
                                            for a, b in printed)
    if not inv['has_summary']:
        inv['warnings'].append(
            'This copy has no SUMMARY page. It is cut off: lines after '
            'the last page in the file are missing.')
    elif not inv['complete']:
        inv['warnings'].append(
            f'The lines read add up to ${inv["lines_net"]:,.2f}. The '
            f'invoice says NET INVOICE AMOUNT '
            f'${(inv["net_total"] or 0):,.2f}.')
    return inv


def parse_invoice_text(text):
    """All invoices in the text of one PDF, in the order they appear."""
    invoices, inv, block, last = [], None, None, None
    in_head, head_lines, page, title = False, 0, 0, ''
    in_summary = False

    def use(number):
        nonlocal inv, block, last, in_summary
        if inv is not None and inv['number'] == number:
            return
        inv = next((i for i in invoices if i['number'] == number), None)
        if inv is None:
            inv = _new_invoice(number)
            invoices.append(inv)
        block, last, in_summary = None, None, False

    for raw in text.splitlines():
        line = raw.rstrip()
        if HEAD_START_RE.match(line):
            in_head, head_lines = True, 0
            continue
        if in_head:
            head_lines += 1
            m = NUMBER_RE.match(line)
            if m:
                use(clean_number(m.group(1)))
                if title and not inv['title']:
                    inv['title'] = title
                continue
            m = TITLE_RE.match(line)
            if m:
                title = re.sub(r'\s+', ' ', m.group(1).strip())
                continue
            m = DATE_RE.match(line)
            if m and inv is not None:
                inv['date'] = inv['date'] or _date(m.group(1))
                continue
            m = PAGE_RE.match(line)
            if m and inv is not None:
                page = int(m.group(1))
                inv['pages'] = max(inv['pages'], page)
                continue
            if HEAD_END_RE.match(line) or head_lines > HEAD_MAX_LINES:
                in_head = False
            continue
        if inv is None or not line.strip():
            continue
        if SUMMARY_RE.match(line):
            in_summary, inv['has_summary'] = True, True
            continue
        if in_summary:
            m = TOTAL_RE.match(line)
            if m:
                key = 'net_total' if m.group(1).startswith('NET') else \
                    'gross_total'
                inv[key] = _money(m.group(2))
            continue
        m = BLOCK_RE.match(line)
        if m:
            if block is not None and block['shipper'] == m.group(2):
                # the same shipper again after a page break
                continue
            block = {'pdc': m.group(1), 'shipper': m.group(2),
                     'order_no': m.group(3).upper(),
                     'order_type': m.group(4), 'order_date': _iso(m.group(5))}
            last = None
            continue
        m = ITEM_RE.match(line)
        if m and block is not None:
            part = re.sub(r'[^A-Z0-9]', '', m.group(2).upper())
            last = dict(block, seq=len(inv['lines']) + 1, page=page,
                        part=part, description=m.group(3).strip(),
                        qty=int(m.group(4)), unit_price=_money(m.group(5)),
                        gross=_money(m.group(6)), net=_money(m.group(10)),
                        kind='core' if CORE_RE.match(part) else 'part',
                        detail='')
            inv['lines'].append(last)
            continue
        m = DEPOSIT_RE.match(line)
        if m:
            inv['deposits'] += _money(m.group(2))
            continue
        if SUBTOTAL_RE.match(line):
            continue
        if last is not None and len(last['detail']) < DETAIL_MAX:
            piece = re.sub(r'\s+', ' ', line.strip())
            last['detail'] = (last['detail'] + ' | ' + piece
                              if last['detail'] else piece)[:DETAIL_MAX]
    return [_finish(i) for i in invoices]


def read_invoice_pdf(path):
    """The invoices in one PDF. Raises NotAnInvoice with a reason a parts
    manager can act on."""
    text = ocr.pdf_text(path)
    if not text.strip():
        raise NotAnInvoice(
            'This PDF has no text in it (it looks like a scan). Save the '
            'invoice from DealerCONNECT as a PDF and send that file.')
    if 'INVOICE NUMBER' not in text.upper():
        raise NotAnInvoice(
            'No INVOICE NUMBER was found. This does not look like a Mopar '
            'parts invoice.')
    invoices = [i for i in parse_invoice_text(text) if i['number']]
    if not invoices:
        raise NotAnInvoice('No invoice could be read from this PDF.')
    return invoices
