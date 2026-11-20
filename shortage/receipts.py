"""Read the DMS parts transaction file (Reynolds Dynamic Reporting export,
CSV or Excel). The receipts are its purchase rows (Trx_Type PUR): on
those rows "Invoice" holds the order number and "Shipping_Number" holds
the Mopar shipper number."""
import collections
import csv
import datetime
import hashlib
import io
import re

# Column names as Reynolds exports them, compared with case, spaces and
# punctuation removed.
COLUMNS = {'order': 'invoice', 'date': 'trxdate', 'part': 'partnumber',
           'qty': 'qty', 'type': 'trxtype', 'shipper': 'shippingnumber',
           'recid': 'recid'}
NEEDED = ('order', 'date', 'part', 'qty', 'type', 'shipper')
RECEIPT_TYPE = 'PUR'
# The DMS puts a make code in front of every part number (CH68524020-AB).
# It is taken off when nearly every row starts with the same two letters.
PREFIX_SHARE = 0.8
MAX_ROWS = 400000


class NotReceipts(Exception):
    """The file is not a parts transaction file this reader understands."""


def _key(name):
    return re.sub(r'[^a-z0-9]', '', str(name or '').lower())


def part_key(raw, prefix=''):
    """A part number in the one form both files are compared in: letters
    and digits only, no make code, no leading zeros."""
    s = re.sub(r'[^A-Z0-9]', '', str(raw or '').upper())
    if prefix and s.startswith(prefix) and len(s) > len(prefix):
        s = s[len(prefix):]
    return s.lstrip('0') or s


def _date(raw):
    s = str(raw or '').strip()
    if isinstance(raw, datetime.datetime):
        return raw.date()
    if isinstance(raw, datetime.date):
        return raw
    for fmt in ('%m/%d/%y', '%m/%d/%Y', '%Y-%m-%d'):
        try:
            return datetime.datetime.strptime(s[:10], fmt).date()
        except ValueError:
            continue
    return None


def _int(raw):
    try:
        return int(round(float(str(raw).replace(',', '').strip() or 0)))
    except ValueError:
        return 0


def _table(content, filename):
    """Rows of the file as lists of cell values."""
    name = (filename or '').lower()
    if name.endswith(('.xlsx', '.xlsm')) or content[:2] == b'PK':
        from openpyxl import load_workbook
        try:
            wb = load_workbook(io.BytesIO(content), read_only=True,
                               data_only=True)
        except Exception:
            raise NotReceipts('This Excel file could not be opened.')
        try:
            return [list(r) for r in wb.worksheets[0].iter_rows(
                values_only=True, max_row=MAX_ROWS)]
        finally:
            wb.close()
    text = content.decode('utf-8-sig', errors='replace')
    return list(csv.reader(io.StringIO(text, newline='')))


def read_receipts(content, filename=''):
    """Parse the file's bytes. Returns the receipts, the make prefix that
    was taken off, and how many rows the file has for each day."""
    table = [r for r in _table(content, filename)
             if any(str(c or '').strip() for c in r)]
    if not table:
        raise NotReceipts('This file is empty.')
    head = [_key(c) for c in table[0]]
    col = {field: head.index(name) for field, name in COLUMNS.items()
           if name in head}
    missing = [COLUMNS[f] for f in NEEDED if f not in col]
    if missing:
        raise NotReceipts(
            'This is not the parts transaction file: it has no column '
            'for ' + ', '.join(missing) + '. The file needs the columns '
            'Invoice, Trx_Date, PartNumber, Qty, Trx_Type and '
            'Shipping_Number.')

    def cell(row, field):
        i = col.get(field)
        return '' if i is None or i >= len(row) or row[i] is None \
            else row[i]

    body = table[1:MAX_ROWS + 1]
    starts = collections.Counter(
        p[:2] for p in (re.sub(r'[^A-Z0-9]', '',
                               str(cell(r, 'part')).upper()) for r in body)
        if len(p) > 4 and p[:2].isalpha())
    prefix = ''
    if starts and body:
        top, n = starts.most_common(1)[0]
        if n >= PREFIX_SHARE * len(body):
            prefix = top

    days = collections.defaultdict(lambda: [0, 0])
    receipts = []
    for row in body:
        day = _date(cell(row, 'date'))
        if day is None:
            continue
        days[day][0] += 1
        if str(cell(row, 'type')).strip().upper() != RECEIPT_TYPE:
            continue
        raw_part = str(cell(row, 'part')).strip()
        part = part_key(raw_part, prefix)
        qty = _int(cell(row, 'qty'))
        if not part or qty == 0:
            continue
        days[day][1] += 1
        recid = str(cell(row, 'recid')).strip()
        if not recid:
            recid = 'h' + hashlib.sha1('|'.join(
                str(c) for c in row).encode('utf-8', 'replace')
            ).hexdigest()[:24]
        receipts.append({
            'rec_key': recid, 'day': day, 'part': part,
            'part_raw': raw_part[:40],
            'order_no': str(cell(row, 'order')).strip().upper()[:20],
            'shipper': re.sub(r'\s+', '', str(cell(row, 'shipper')))[:20],
            'qty': qty})
    if not days:
        raise NotReceipts('No row in this file has a date in Trx_Date.')
    return {'receipts': receipts, 'prefix': prefix,
            'rows': sum(d[0] for d in days.values()),
            'days': {d: tuple(v) for d, v in days.items()},
            'first': min(days), 'last': max(days)}
