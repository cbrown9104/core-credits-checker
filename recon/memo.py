"""Parser for Mopar Weekly Global Core Return Credit Memo PDFs.

Each credit is a block that starts with an order line
(0318100-3272051 ORD#: GCRS O/T:M) and ends with SUB-TOTAL. Inside it:
the item line (part line or ARC deposit line), REFERENCE/CONTROL NUMBER,
RM:<ticket> CORE:<part>, and sometimes CLAIM:/VIN:/REPAIR:. A block can
run across a page break, so page headers never close a block.
"""
import datetime
import re

from .common import (clean_key, claim_out, norm_part_text, ocr_ticket,
                     parse_money)
from . import ocr

MEMO_NO_RE = re.compile(r'CREDIT\s+MEMO\s+NUMBER\s*:?\s*\d*?(C[A-Z]\d{8})\b')
MEMO_DATE_RE = re.compile(
    r'CREDIT\s+MEMO\s+DATE\s*:?\s*([A-Z]{3,9})\s+(\d{1,2})\s*,?\s*(\d{4})')
BILL_TO_RE = re.compile(r'BILL\s+TO\s*:?\s*(\d{5})\d{0,3}')
ORDER_RE = re.compile(r'^\s*\d{7}-\d{6,8}\s+ORD#')
REF_RE = re.compile(r'REFERENCE/CONTROL\s+NUMBER\s+(\S+)')
RM_RE = re.compile(r'RM:\s*(\S+)')
CORE_RE = re.compile(r'CORE:\s*(\S+)')
CLAIM_RE = re.compile(r'CLAIM:\s*(\S+)')
VIN_RE = re.compile(r'VIN:\s*(\S+)')
REPAIR_RE = re.compile(r'REPAIR:\s*(\S+)')
SUBTOTAL_RE = re.compile(r'SUB-TOTAL')
NET_TOTAL_RE = re.compile(r'NET\s+CREDIT\s+AMOUNT\s+([\d,]+\.\d{2})-?')
CREDIT_AMT_RE = re.compile(r'(?<![\d,.])((?:\d{1,3}(?:,\d{3})+|\d+)\.\d{2})-')
ITEM_RE = re.compile(r'^\s*(\d{1,3})\s+([A-Z0-9]{5,14})\s+(.*)$')

_MONTHS = {m: i for i, m in enumerate(
    ['JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN', 'JUL', 'AUG', 'SEP', 'OCT',
     'NOV', 'DEC'], start=1)}


def _memo_date(m):
    mon = _MONTHS.get(m.group(1)[:3].upper())
    if not mon:
        return None
    try:
        return datetime.date(int(m.group(3)), mon, int(m.group(2)))
    except ValueError:
        return None


def _part(raw, from_ocr):
    if from_ocr:
        from .shipper import ocr_part
        return ocr_part(raw)
    return norm_part_text(raw)


def _ticket(raw, from_ocr):
    t = clean_key(raw).rstrip('.,;:')
    if from_ocr:
        fixed = ocr_ticket(t)
        if fixed:
            return fixed[0]
    return t


def parse_memo_text(text, from_ocr=False, source=''):
    """Parse credit memo text into credit lines.

    Returns dict(credits=[...], memos={memo_no: {...}}, warnings=[...],
    dealer=str|None).
    """
    credits = []
    memos = {}
    warnings = []
    dealer = None
    memo_no = None
    memo_date = None
    block = None

    def close_block(subtotal=None):
        nonlocal block
        if block is None:
            return
        amount = subtotal
        if amount is None:
            amount = round(sum(it['net'] for it in block['items']), 2) or None
        tickets = block['tickets']
        rec = {
            'memo': block['memo'], 'memo_date': block['memo_date'],
            'part': block['core'] or next(
                (it['part'] for it in block['items']
                 if not it['part'].startswith('ARC')), ''),
            'claim': block['claim'], 'vin': block['vin'],
            'repair': block['repair'],
            'description': (block['items'][0]['desc']
                            if block['items'] else ''),
            'arc': (block['items'][0]['part']
                    if block['items'] and
                    block['items'][0]['part'].startswith('ARC') else ''),
            'amount': amount, 'source': source,
        }
        if not tickets:
            rec['ticket'] = None
            credits.append(rec)
        elif len(tickets) == 1:
            rec['ticket'] = tickets[0]
            credits.append(rec)
        else:
            # Several tickets in one block: pair with item lines if counts
            # agree, otherwise keep the block total on the first ticket.
            items = block['items']
            for i, t in enumerate(tickets):
                r = dict(rec, ticket=t)
                if len(items) == len(tickets):
                    r['amount'] = items[i]['net']
                elif i:
                    r['amount'] = 0.0
                    warnings.append(
                        f'{block["memo"]}: block with {len(tickets)} tickets '
                        f'but {len(items)} item lines; amount kept on '
                        f'{tickets[0]}')
                credits.append(r)
        if block['memo']:
            memos[block['memo']]['lines_total'] = round(
                memos[block['memo']]['lines_total'] + (amount or 0), 2)
        block = None

    for raw in text.splitlines():
        line = raw.rstrip()
        up = line.upper()
        m = MEMO_NO_RE.search(up)
        if m:
            if m.group(1) != memo_no:
                memo_no = m.group(1)
                if memo_no not in memos:
                    memo_date = None
                    memos[memo_no] = {'memo': memo_no, 'date': None,
                                      'net_total': None, 'lines_total': 0.0,
                                      'source': source}
                else:
                    memo_date = memos[memo_no]['date']
            continue
        m = MEMO_DATE_RE.search(up)
        if m:
            memo_date = _memo_date(m) or memo_date
            if memo_no:
                memos[memo_no]['date'] = memo_date
            continue
        m = BILL_TO_RE.search(up)
        if m and not dealer:
            dealer = m.group(1)
        m = NET_TOTAL_RE.search(up)
        if m:
            close_block()
            if memo_no:
                memos[memo_no]['net_total'] = parse_money(m.group(1))
            continue
        if ORDER_RE.match(up):
            close_block()
            block = {'memo': memo_no, 'memo_date': memo_date, 'items': [],
                     'tickets': [], 'core': '', 'claim': None, 'vin': '',
                     'repair': ''}
            continue
        if block is None:
            continue
        if SUBTOTAL_RE.search(up):
            amts = CREDIT_AMT_RE.findall(up)
            close_block(parse_money(amts[-1]) if amts else None)
            continue
        m = REF_RE.search(up)
        if m:
            t = _ticket(m.group(1), from_ocr)
            if t and t not in block['tickets']:
                block['tickets'].append(t)
            continue
        m = RM_RE.search(up)
        if m:
            t = _ticket(m.group(1), from_ocr)
            if t and t not in block['tickets']:
                block['tickets'].append(t)
            c = CORE_RE.search(up)
            if c:
                block['core'] = _part(c.group(1), from_ocr)
            continue
        if 'CLAIM:' in up or 'VIN:' in up:
            c = CLAIM_RE.search(up)
            if c:
                block['claim'] = claim_out(c.group(1))
            v = VIN_RE.search(up)
            if v:
                block['vin'] = v.group(1)
            rp = REPAIR_RE.search(up)
            if rp:
                block['repair'] = rp.group(1)
            continue
        amts = CREDIT_AMT_RE.findall(up)
        im = ITEM_RE.match(up)
        if im and amts:
            part = im.group(2)
            desc = re.split(r'\s{2,}|\s\d+\s+[\d,]+\.\d{2}-', im.group(3))[0]
            block['items'].append({
                'part': _part(part, from_ocr) if not part.startswith('ARC')
                else part,
                'desc': desc.strip(),
                'net': parse_money(amts[-1]) or 0.0})
    close_block()

    for memo in memos.values():
        if memo['net_total'] is not None and \
                abs(memo['net_total'] - memo['lines_total']) > 0.005:
            warnings.append(
                f'{memo["memo"]}: credit lines add up to '
                f'${memo["lines_total"]:,.2f} but the memo says NET CREDIT '
                f'AMOUNT ${memo["net_total"]:,.2f}')
    return {'credits': credits, 'memos': memos, 'warnings': warnings,
            'dealer': dealer}


def parse_memo_pdf(path, source='', progress=None):
    """Read a credit memo PDF (text layer, or OCR when it's a scan)."""
    text = ocr.pdf_text(path)
    from_ocr = False
    if 'CREDIT MEMO NUMBER' not in text.upper():
        text = ocr.ocr_text(path, progress=progress, label=source)
        from_ocr = True
    result = parse_memo_text(text, from_ocr=from_ocr, source=source)
    result['ocr'] = from_ocr
    if not result['memos']:
        result['warnings'].append(
            f'{source}: no "CREDIT MEMO NUMBER" found — is this a Mopar '
            f'Weekly Global Core Return Credit Memo?')
    return result
