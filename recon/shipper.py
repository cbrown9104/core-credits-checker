"""Read signed GCRS Dealer Shipper Document scans.

Every page is OCR'd with word boxes so we know, for each control ticket,
which page it is on and where (used to stamp "UnPaid" on the credit
request copy). Each page carries its GCRS SHP# (shipment id), e.g.
"2026-08-03 17:00.01", which is the master's Shipment ID.
"""
import re

from . import ocr
from .common import (MONEY_RE, claim_out, ocr_ticket, part_looks_valid)

SHP_RE = re.compile(
    r'SHP\s*#?\s*:?\s*(\d{4})\s*[-–—~]\s*(\d{2})\s*[-–—~]\s*(\d{2})'
    r'(?:\s+(\d{1,2})\s*[:;.]\s*(\d{2})\s*[.:,;]\s*(\d{2}))?')
DEALER_RE = re.compile(r'DEALER\s*[:;]?\s*(\d{5})\d{0,3}')
QTY_RE = re.compile(r'^[0-9lI|]{1,2}$')
CLAIM_TOKEN_RE = re.compile(r'^\d{5,6}[A-Z]?$')
NOISE = re.compile(r'^[\W_]+$')
SKIP_WORDS = {'UNPAID', 'PAID', 'V', 'X', '/', '\\', 'J', 'VV', '.', ','}

_HDR_DIGITS = str.maketrans({'O': '0', 'o': '0', 'I': '1', 'l': '1',
                             'S': '5', 'B': '8', 'Z': '2'})


_LOOSE_DIGITS = str.maketrans({'O': '0', 'Q': '0', 'D': '0', 'I': '1',
                               'L': '1', '|': '1', '!': '1', 'S': '5',
                               'B': '8', 'Z': '2'})
TIME_RE = re.compile(r'(\d{1,2})\s*[:;.]\s*(\d{2})\s*[.:,;]\s*(\d{2})')


def _shipment_from_header(up):
    """GCRS SHP# from the page header line -> '2026-08-03 17:00.01'.

    Tolerates OCR noise such as 'SHP#42026-08-2i1 18:00.01'.
    """
    m = SHP_RE.search(up)
    if m:
        y, mo, d = m.group(1), m.group(2), m.group(3)
        hh, mm, ss = m.group(4), m.group(5), m.group(6)
    else:
        tail = up[up.index('SHP') + 3:].translate(_LOOSE_DIGITS)
        tail = re.sub(r'[^0-9:;.,\-\s]', '', tail)
        dm = re.search(r'(20\d{2})[\s\-]*(\d{2})[\s\-]*(\d{2})', tail)
        if not dm:
            return None
        y, mo, d = dm.group(1), dm.group(2), dm.group(3)
        tm = TIME_RE.search(tail[dm.end():])
        hh, mm, ss = (tm.group(1), tm.group(2), tm.group(3)) if tm else \
            (None, None, None)
    if not (1 <= int(mo) <= 12 and 1 <= int(d) <= 31):
        return None
    if hh is not None and int(hh) < 24 and int(mm) < 60:
        if int(ss) >= 60:
            ss = '01'    # GCRS ids end in .01; "61" is a misread "01"
        return f'{y}-{mo}-{d} {int(hh):02d}:{mm}.{ss}'
    return f'{y}-{mo}-{d}'


def ocr_part(raw):
    """Part number from OCR: fix look-alikes in the digit body only.

    The last two letters are the suffix and stay as read (Mopar does use
    suffixes such as AI). In a numeric body, I -> 1 and O -> 0; a leading
    O in front of digits is a zero (O5151705AF -> 05151705AF); a leading
    $ is a 5; a check mark read as v/V in front of a U part is dropped.
    """
    s = (raw or '').strip().replace('$', '5').replace('§', '5')
    s = re.sub(r'[^A-Za-z0-9]', '', s).upper()
    if len(s) >= 9 and s[0] == 'V' and (s[1] == 'U' or s[1:8].isdigit()):
        s = s[1:] if s[1] == 'U' else 'U' + s[1:]
    if len(s) < 4:
        return s
    body, suffix = s[:-2], s[-2:]
    # a letter O touching a digit (or another such O) inside the body is 0
    body = re.sub(r'O(?=O*\d)|(?<=\d)O', '0', body)
    if body[:1] in ('U', 'O') or body[:1].isdigit():
        head = body[0] if body[0] == 'U' else ''
        rest = body[len(head):]
        # Only treat as a numeric body when it is mostly digits already
        if sum(ch.isdigit() for ch in rest) >= max(1, len(rest) - 2) and \
                not rest[:1].isalpha() or rest[:1] == 'O':
            rest = rest[:1].replace('O', '0') + \
                rest[1:].replace('I', '1').replace('O', '0')
            body = head + rest
    return body + suffix


def _money(tok):
    t = tok.strip().rstrip('|')
    t = t.replace('O', '0').replace('o', '0')
    if re.fullmatch(r'\d{1,3}(,\d{3})*-\d{2}|\d+-\d{2}', t):   # 1828-00
        t = t[:-3] + '.' + t[-2:]
    m = MONEY_RE.match(t)
    if not m:
        return None
    return float(m.group(1).replace(',', '') + '.' + m.group(2))


def _find_ticket(words):
    """(index, tokens_used, ticket, fixed) for the ticket near line start."""
    tried = 0
    for i, w in enumerate(words[:5]):
        tok = w['text']
        up = tok.upper().strip('.,:;')
        if not up or NOISE.match(up) or up in SKIP_WORDS or \
                set(up) <= set('_-—=~.'):
            continue
        tried += 1
        hit = ocr_ticket(tok)
        if hit:
            return i, 1, hit[0], hit[1]
        digits = re.sub(r'\D', '', tok)
        # Leading C dropped by OCR: bare 9-digit run in the ticket column
        if len(digits) == 9 and len(tok.strip('.,:;')) == 9:
            return i, 1, 'C' + digits, True
        # Ticket split across two tokens (C45383 4085)
        if i + 1 < len(words):
            joined = ocr_ticket(tok + words[i + 1]['text'])
            if joined:
                return i, 2, joined[0], True
        if tried >= 2:
            break
    return None


def parse_line(words):
    """Parse one OCR line into a shipper row, or None."""
    found = _find_ticket(words)
    if not found:
        return None
    i, used, ticket, fixed = found
    rest = words[i + used:]
    toks = [w['text'] for w in rest
            if not NOISE.match(w['text']) and w['text'] not in ("'", '‘')]
    amount = None
    qty = None
    if toks:
        last = toks[-1]
        m = re.fullmatch(r'([0-9lI|])[-—~]([\d,]+[.,]\d{2})', last)
        if m:                                   # 1-75.00  (qty glued on)
            qty = 1 if m.group(1) in 'lI|' else int(m.group(1))
            amount = _money(m.group(2))
        else:
            amount = _money(last)
            if amount is not None and amount >= 1000 and ',' not in last \
                    and last[:1] in '123456789' and \
                    not (len(toks) > 1 and QTY_RE.match(toks[-2].strip('.-'))):
                # 1125.00 = qty 1 + 125.00 (amounts over 999 print a comma)
                qty = int(last[0])
                amount = _money(last[1:])
        if amount is not None:
            toks = toks[:-1]
    if qty is None and toks and QTY_RE.match(toks[-1].strip('.-')):
        q = toks[-1].strip('.-')
        qty = int(q.replace('l', '1').replace('I', '1').replace('|', '1'))
        toks = toks[:-1]
    # Drop stray marks between the ticket and the claim/part (check marks,
    # "som", "7", quotes) — a Mopar part number is at least 7 characters.
    while len(toks) > 1 and len(re.sub(r'[^A-Za-z0-9]', '', toks[0])) <= 3:
        toks = toks[1:]
    claim = None
    first = re.sub(r'[^A-Za-z0-9]', '', toks[0]).upper() if toks else ''
    if len(toks) >= 2 and CLAIM_TOKEN_RE.match(first):
        claim = claim_out(first)
        toks = toks[1:]
        while len(toks) > 1 and \
                len(re.sub(r'[^A-Za-z0-9]', '', toks[0])) <= 3:
            toks = toks[1:]
    part = ocr_part(toks[0]) if toks else ''
    desc = ' '.join(toks[1:]) if len(toks) > 1 else ''
    tw = words[i]
    confs = [w['conf'] for w in words if w.get('conf', -1) >= 0]
    return {
        'ticket': ticket, 'ticket_raw': tw['text'], 'ticket_fixed': fixed,
        'claim': claim, 'part': part,
        'part_ok': part_looks_valid(part), 'description': desc,
        'qty': qty, 'amount': amount,
        'box': (tw['left'], tw['top'], tw['left'] + tw['width'],
                tw['top'] + tw['height']),
        'line_box': (min(w['left'] for w in words),
                     min(w['top'] for w in words),
                     max(w['left'] + w['width'] for w in words),
                     max(w['top'] + w['height'] for w in words)),
        'conf': round(sum(confs) / len(confs), 1) if confs else None,
    }


def parse_page(page, file_label='', path=''):
    out = {'file': file_label, 'path': path, 'page': page['page'],
           'width': page['width'], 'height': page['height'],
           'dpi': page['dpi'], 'shipment_id': None, 'dealer': None,
           'lines': [], 'checkoff_x': None, 'printed_total': None}
    lines = page['lines']
    for idx, ln in enumerate(lines):
        text = ln['text']
        up = text.upper()
        if out['shipment_id'] is None and 'SHP' in up:
            out['shipment_id'] = _shipment_from_header(up)
        if out['dealer'] is None:
            m = DEALER_RE.search(up)
            if m:
                out['dealer'] = m.group(1)
        if out['checkoff_x'] is None and ln['words'] and \
                ln['words'][0]['text'].upper().startswith('CHECKOFF') and \
                'TICKET' in up:
            # column header row: "CHECKOFF TICKET NUMBER NUMBER ..."
            out['checkoff_x'] = ln['words'][0]['left']
        if up.startswith('TOTAL') and 'PARTS' not in up:
            vals = [_money(w['text']) for w in ln['words']]
            vals = [v for v in vals if v is not None]
            if vals:
                out['printed_total'] = vals[-1]
            continue
        row = parse_line(ln['words'])
        if not row:
            continue
        if row['amount'] is None and idx + 1 < len(lines):
            # amount/qty wrapped onto the next line
            nxt = [w['text'] for w in lines[idx + 1]['words']]
            if 1 <= len(nxt) <= 3 and _money(nxt[-1]) is not None:
                row['amount'] = _money(nxt[-1])
                if len(nxt) >= 2 and QTY_RE.match(nxt[-2]):
                    row['qty'] = int(nxt[-2].replace('l', '1')
                                     .replace('I', '1').replace('|', '1'))
        out['lines'].append(row)
    return out


def read_shipper_pdf(path, file_label='', progress=None):
    """OCR a shipper PDF; returns a list of parsed pages."""
    pages = ocr.ocr_pdf(path, progress=progress, label=file_label)
    return [parse_page(p, file_label=file_label, path=path) for p in pages]
