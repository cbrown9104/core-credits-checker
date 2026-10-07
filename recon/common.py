"""Shared normalization helpers for the reconciliation engine.

Everything that compares a control ticket, part number, amount or date
goes through these functions so the shipper, credit memo, DealerCONNECT
and master-workbook readers all agree on one canonical form.
"""
import datetime
import re

# Current GCRS control tickets are C + 9 digits (C412345678). Older series
# in a master can be CA/CB/CW + 8 digits, or RA-######.
GCRS_TICKET_RE = re.compile(r'^C\d{9}$')
SERIES_TICKET_RE = re.compile(r'^C[ABW]\d{8}$')
RA_TICKET_RE = re.compile(r'^RA-\d{4,8}$')

MONEY_RE = re.compile(r'^\$?(\d{1,3}(?:,\d{3})+|\d+)[.,](\d{2})-?$')

# OCR look-alikes inside the digit run of a ticket.
_DIGIT_FIX = str.maketrans({
    'O': '0', 'o': '0', 'Q': '0', 'D': '0', 'U': '0',
    'I': '1', 'l': '1', 'i': '1', '|': '1', '!': '1', 'L': '1', 'J': '1',
    'Z': '2', 'z': '2',
    'S': '5', 's': '5', '$': '5',
    'G': '6', 'b': '6',
    'T': '7',
    'B': '8',
    'g': '9', 'q': '9',
})
# Things OCR returns for the leading "C".
_C_LOOKALIKES = set('C©€(6cG[{0OQ')


def clean_key(value):
    """Uppercase, trimmed string key, or '' for blanks."""
    if value is None:
        return ''
    return str(value).strip().upper()


def is_valid_ticket(ticket):
    t = clean_key(ticket)
    return bool(GCRS_TICKET_RE.match(t) or SERIES_TICKET_RE.match(t)
                or RA_TICKET_RE.match(t))


def ocr_ticket(token):
    """Turn one OCR token into a control ticket, or None.

    Handles the misreads seen on scanned GCRS shippers: a leading C read as
    6, (, © or €, and letters inside the digit run (O→0, S→5, B→8, I→1).
    Returns (ticket, fixed) where fixed is True when characters were changed.
    """
    if not token:
        return None
    raw = re.sub(r'[^\w©€(\[{|!$]', '', token)
    if len(raw) < 9 or len(raw) > 11:
        return None
    first, rest = raw[0], raw[1:]
    if first not in _C_LOOKALIKES:
        return None
    # CA/CB/CW series: C + letter + 8 digits (CBO0123456 -> CB00123456)
    if len(raw) == 10 and rest[0] in 'ABW':
        tail = rest[1:].translate(_DIGIT_FIX)
        if len(tail) == 8 and tail.isdigit() and rest[1:2] in '0OoQD':
            ticket = 'C' + rest[0] + tail
            return (ticket, ticket != raw)
    digits = rest.translate(_DIGIT_FIX)
    if len(digits) == 9 and digits.isdigit():
        ticket = 'C' + digits
        return (ticket, ticket != raw)
    return None


def norm_part_text(raw):
    """Part number from a text source (memo text, DealerCONNECT, master):
    uppercase with spaces/dashes removed, no OCR substitutions — Mopar
    suffixes such as AI are real."""
    s = clean_key(raw)
    return re.sub(r'[^A-Z0-9]', '', s)


def part_looks_valid(part):
    """Loose Mopar part shape: 7-12 alphanumerics ending in two letters."""
    return bool(re.fullmatch(r'[A-Z0-9]{5,10}[A-Z]{2}', part or ''))


def parse_money(token):
    """'1,050.00' / '2500.00-' / '$75.00' -> float, else None."""
    if token is None:
        return None
    if isinstance(token, (int, float)):
        return float(token)
    t = str(token).strip()
    m = MONEY_RE.match(t)
    if not m:
        try:
            return float(t.replace(',', '').replace('$', ''))
        except ValueError:
            return None
    return float(m.group(1).replace(',', '') + '.' + m.group(2))


def money_out(value):
    """Store whole-dollar amounts as int (how the master keeps them)."""
    if value is None:
        return None
    v = round(float(value), 2)
    return int(v) if v == int(v) else v


def claim_out(value):
    """Claim numbers: numeric ones as int (like the master), others as text."""
    if value is None:
        return None
    s = str(value).strip()
    if not s or s.upper() in ('NONE', 'NAN'):
        return None
    if re.fullmatch(r'\d+', s):
        return int(s)
    if re.fullmatch(r'\d+\.0', s):
        return int(float(s))
    return s


def shipment_date(shipment):
    """'2026-08-03 17:00.01' or a date -> datetime.date, else None."""
    if shipment is None:
        return None
    if isinstance(shipment, datetime.datetime):
        return shipment.date()
    if isinstance(shipment, datetime.date):
        return shipment
    m = re.match(r'\s*(\d{4})-(\d{2})-(\d{2})', str(shipment))
    if not m:
        return None
    try:
        return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def iso(d):
    return d.isoformat() if d else ''


def hamming1(a, b):
    """True when a and b are the same length and differ in one position."""
    if len(a) != len(b) or a == b:
        return False
    return sum(1 for x, y in zip(a, b) if x != y) == 1
