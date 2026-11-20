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


def _is_total_line(up):
    return up.startswith('TOTAL') and 'PARTS' not in up


def _rows(lines):
    """Ticket rows and the text of the printed TOTAL line(s) from OCR
    lines (top to bottom).

    Only the table counts: rows start under the column headings
    (CHECKOFF TICKET NUMBER ...) and stop at the printed TOTAL line or
    the PICKUP VERIFICATION block, or at a gap of several lines.
    Anything else on the page that looks like a ticket (the DEALER line
    read as one, a driver's scan receipt or hand-written ticket numbers
    below the table) is not a shipper line. A page whose
    headings cannot be read is taken whole, as before."""
    rows, totals = [], []
    head = None
    for ln in lines:
        up = ln['text'].upper()
        if 'TICKET' in up and ('CHECKOFF' in up or
                               up.count('NUMBER') >= 2):
            head = ln.get('top')
            break
    ended = False
    for idx, ln in enumerate(lines):
        up = ln['text'].upper()
        if _is_total_line(up):
            totals.append(ln['text'])
            ended = ended or bool(rows)
            continue
        if ended:
            continue
        if rows and 'PICKUP' in up and 'VERIF' in up:
            ended = True
            continue
        if head is not None and ln.get('top') is not None and \
                ln['top'] <= head:
            continue
        row = parse_line(ln['words'])
        if not row:
            continue
        if rows:
            # Shipper lines sit one under the other. Something that looks
            # like a ticket well below the last line (a driver's scan
            # receipt attached to the page, a hand-written list) is not
            # part of the table, and nothing after it is either.
            last = rows[-1]['line_box']
            if row['line_box'][1] - last[3] > 4 * max(12, last[3] - last[1]):
                ended = True
                continue
        if row['amount'] is None and idx + 1 < len(lines):
            # amount/qty wrapped onto the next line
            nxt = [w['text'] for w in lines[idx + 1]['words']]
            if 1 <= len(nxt) <= 3 and _money(nxt[-1]) is not None:
                row['amount'] = _money(nxt[-1])
                if len(nxt) >= 2 and QTY_RE.match(nxt[-2]):
                    row['qty'] = int(nxt[-2].replace('l', '1')
                                     .replace('I', '1').replace('|', '1'))
        rows.append(row)
    return rows, totals


def parse_page(page, file_label='', path=''):
    out = {'file': file_label, 'path': path, 'page': page['page'],
           'width': page['width'], 'height': page['height'],
           'dpi': page['dpi'], 'shipment_id': None, 'dealer': None,
           'lines': [], 'checkoff_x': None, 'printed_total': None,
           'total_texts': []}
    lines = page['lines']
    for ln in lines:
        up = ln['text'].upper()
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
        if _is_total_line(up):
            vals = [_money(w['text']) for w in ln['words']]
            vals = [v for v in vals if v is not None]
            if vals:
                out['printed_total'] = vals[-1]
    out['lines'], out['total_texts'] = _rows(lines)
    return out


# ── the printed TOTAL on each page, and second reads ────────────────────
# Every shipper page ends with "TOTAL <count> <amount>". The count is
# usually circled by hand, which OCR reads as stray characters around (or
# glued onto) the numbers, so a TOTAL line is read several ways and each
# reading only offers candidates.
_TOTAL_AMT_RE = re.compile(r'(?<![\d.,])(\d[\d,]*)[.\-](\d{2})(?!\d)')
# A re-read of the ticket rows: (scale, blur radius). Reading the same
# page again a little differently gives different mistakes, which is
# what makes comparing the reads useful.
REREADS = ((2, 1.0), (2, 1.5))


def total_amounts(text):
    """(as read, with stray leading digits dropped) amounts in a TOTAL
    reading. '7965.00' is 7,965.00 as read; the circled count glued on
    the front makes 965.00 just as likely, so it is offered as well."""
    direct, loose = [], []
    t = re.sub(r'(?<=\d),\s+(?=\d)', ',', text or '')
    t = t.replace('O', '0').replace('o', '0')
    for m in _TOTAL_AMT_RE.finditer(t):
        digits = m.group(1).replace(',', '')
        if not digits:
            continue
        cents = int(m.group(2)) / 100.0
        direct.append(int(digits) + cents)
        # one stray digit in front, or two when the number is long
        # enough that a two-digit count could be glued onto it
        for cut in (1, 2) if len(digits) >= 6 else (1,):
            if len(digits) - cut >= 1:
                loose.append(int(digits[cut:]) + cents)
    return direct, loose


def total_counts(text):
    """Line counts a TOTAL reading shows: a 1-2 digit number standing on
    its own. A count with pen marks read into it ("C2)", "(@)") is not
    taken as a number."""
    return [int(tok) for tok in (text or '').split()
            if tok.isdigit() and len(tok) <= 2 and int(tok) > 0]


def _row_metrics(page):
    """(top of first row, bottom of last row, row pitch, right edge of
    the amount column) from the rows read on a page, or None."""
    boxes = [ln['line_box'] for ln in page['lines'] if ln.get('line_box')]
    if not boxes:
        return None
    tops = sorted(b[1] for b in boxes)
    heights = sorted(b[3] - b[1] for b in boxes)
    h = heights[len(heights) // 2]
    gaps = sorted(b - a for a, b in zip(tops, tops[1:]) if b - a > h * 0.5)
    pitch = gaps[len(gaps) // 2] if gaps else h * 1.45
    pitch = max(h * 1.1, min(pitch, h * 2.2))
    return (tops[0], max(b[3] for b in boxes), pitch,
            max(b[2] for b in boxes))


def _prep(im, scale, blur):
    from PIL import Image, ImageFilter
    out = im.resize((int(im.width * scale), int(im.height * scale)),
                    Image.LANCZOS)
    if blur:
        out = out.filter(ImageFilter.GaussianBlur(blur))
    pad = 40
    bg = Image.new('L', (out.width + 2 * pad, out.height + 2 * pad), 255)
    bg.paste(out, (pad, pad))
    return bg, pad


def reread_rows(im, page, variants=REREADS):
    """Read the ticket rows of a page again from its image, once per
    variant. Returns [{'rows': [...], 'total_texts': [...]}] with every
    box in the first read's page coordinates."""
    met = _row_metrics(page)
    if not met or im is None:
        return []
    top, bottom, pitch, _ = met
    y0 = max(0, int(top - pitch * 1.5))
    y1 = min(im.height, int(bottom + pitch * 5))
    band = im.crop((0, y0, im.width, y1))
    out = []
    for scale, blur in variants:
        img, pad = _prep(band, scale, blur)
        lines = ocr.ocr_image(img)
        rows, totals = _rows(lines)

        def back(box):
            l, t, r, b = box
            return ((l - pad) / scale, (t - pad) / scale + y0,
                    (r - pad) / scale, (b - pad) / scale + y0)
        for row in rows:
            row['box'] = back(row['box'])
            row['line_box'] = back(row['line_box'])
        out.append({'rows': rows, 'total_texts': totals})
    return out


def total_crops(im, page):
    """Read just the TOTAL amount and the TOTAL count under the last row,
    each a few ways. Returns (amount readings, count readings)."""
    met = _row_metrics(page)
    if not met or im is None:
        return [], []
    _, bottom, pitch, right = met
    y0 = int(bottom + pitch * 0.55)
    y1 = min(im.height, int(bottom + pitch * 2.3))
    if y1 - y0 < 8:
        return [], []
    amounts, counts = [], []
    spans = (('amt', right - 175, right + 25, '0123456789.,'),
             ('cnt', right - 330, right - 150, '0123456789'))
    for kind, x0, x1, allowed in spans:
        x0, x1 = max(0, int(x0)), min(im.width, int(x1))
        if x1 - x0 < 20:
            continue
        crop = im.crop((x0, y0, x1, y1))
        for scale, blur, psm in ((3, 1.5, 7), (2, 1.0, 7), (3, 2.0, 6)):
            img, _ = _prep(crop, scale, blur)
            text = ' '.join(ln['text'] for ln in
                            ocr.ocr_image(img, psm=psm, whitelist=allowed))
            if text.strip():
                (amounts if kind == 'amt' else counts).append(text)
    return amounts, counts


def read_shipper_pdf(path, file_label='', progress=None):
    """OCR a shipper PDF; returns a list of parsed pages."""
    pages = ocr.ocr_pdf(path, progress=progress, label=file_label)
    return [parse_page(p, file_label=file_label, path=path) for p in pages]
