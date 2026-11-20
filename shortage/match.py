"""Match invoice lines to receipts and say how each one matched.

A receipt counts for a line when the part number is the same and either
the shipper number or the order number is the same. After the exact
matches come two near ones, both on the same shipper or order: the part
number the invoice says was superseded, and a part number that differs
only in its last letter. A receipt is used once: its quantity is shared
out, never counted for two lines.

An order number that is only close (EA1113 against EA1116) is never
counted. Daily order numbers end in the date, so a close number is a
different day's order, and counting it would hide a real shortage. The
line shows such a receipt as a hint instead.
"""
import collections

from .receipts import part_key

# How a line found its receipts, best first.
BY_SHIPPER = 'shipper'
BY_ORDER = 'order'
SUPERSEDED = 'superseded'
NEAR_PART = 'near_part'
HOW_TEXT = {
    BY_SHIPPER: 'Matched by shipper number',
    BY_ORDER: 'Matched by order number',
    SUPERSEDED: 'Received under the part number that was ordered',
    NEAR_PART: 'Received under a different suffix',
}
HINTS = 2


def _base(part):
    """The part number without its last letter (68381242AD -> 68381242A)."""
    return part[:-1] if len(part) > 5 and part[-1].isalpha() else ''


class _Pool:
    """Receipts of one part for one shipper and order, with the quantity
    still free. Plus and minus rows of the same receipt cancel here."""
    __slots__ = ('part', 'shipper', 'order_no', 'free', 'day', 'raw')

    def __init__(self, part, shipper, order_no):
        self.part, self.shipper, self.order_no = part, shipper, order_no
        self.free, self.day, self.raw = 0, None, ''


def match(lines, receipts, ignored=()):
    """lines: dicts with id, part, alt_part, shipper, order_no,
    order_date, qty, kind, sort.
    receipts: dicts with part (already a part_key), shipper, order_no,
    qty, day, part_raw. Returns {line id: (received, how, hint)}."""
    ignored = {part_key(p) for p in ignored}
    pools = {}
    for r in receipts:
        key = (r['part'], r['shipper'], r['order_no'])
        pool = pools.get(key)
        if pool is None:
            pool = pools[key] = _Pool(*key)
        pool.free += r['qty']
        if r['qty'] > 0 and (pool.day is None or r['day'] < pool.day):
            pool.day, pool.raw = r['day'], r.get('part_raw', '')
    by = {name: collections.defaultdict(list) for name in (
        'shipper', 'order', 'base_shipper', 'base_order', 'part')}
    for pool in pools.values():
        if pool.free <= 0:
            continue
        base = _base(pool.part)
        by['part'][pool.part].append(pool)
        if pool.shipper:
            by['shipper'][(pool.part, pool.shipper)].append(pool)
            if base:
                by['base_shipper'][(base, pool.shipper)].append(pool)
        if pool.order_no:
            by['order'][(pool.part, pool.order_no)].append(pool)
            if base:
                by['base_order'][(base, pool.order_no)].append(pool)

    work = []
    for l in sorted(lines, key=lambda l: l['sort']):
        part = part_key(l['part'])
        if l.get('kind', 'part') != 'part' or l['qty'] <= 0 or \
                part in ignored:
            continue
        work.append({'id': l['id'], 'part': part,
                     'alt': part_key(l.get('alt_part') or ''),
                     'shipper': l['shipper'],
                     'order_no': l['order_no'], 'need': l['qty'],
                     'since': l.get('order_date'),
                     'got': 0, 'how': '', 'via': None})

    def take(w, found, how):
        for pool in found:
            if w['need'] <= 0:
                return
            if pool.free <= 0:
                continue
            n = min(pool.free, w['need'])
            pool.free -= n
            w['need'] -= n
            w['got'] += n
            if not w['how']:
                w['how'], w['via'] = how, pool

    def superseded(w):
        if not w['alt']:
            return []
        found = by['shipper'].get((w['alt'], w['shipper']), []) if \
            w['shipper'] else []
        if w['order_no']:
            found = found + by['order'].get((w['alt'], w['order_no']), [])
        return found

    def near_part(w):
        base = _base(w['part'])
        if not base:
            return []
        found = by['base_shipper'].get((base, w['shipper']), []) if \
            w['shipper'] else []
        if w['order_no']:
            found = found + by['base_order'].get((base, w['order_no']), [])
        return [p for p in found if p.part != w['part']]

    passes = (
        (BY_SHIPPER, lambda w: by['shipper'].get(
            (w['part'], w['shipper']), []) if w['shipper'] else []),
        (BY_ORDER, lambda w: by['order'].get(
            (w['part'], w['order_no']), []) if w['order_no'] else []),
        (SUPERSEDED, superseded),
        (NEAR_PART, near_part),
    )
    # Every exact match is settled before any near match is tried, so a
    # near match can never take a receipt an exact match needs.
    for how, find in passes:
        for w in work:
            if w['need'] > 0:
                take(w, find(w), how)

    out = {}
    for w in work:
        hint = ''
        if w['how'] in (SUPERSEDED, NEAR_PART) and w['via'] is not None:
            hint = f'Received as {w["via"].raw or w["via"].part}.'
        if w['need'] > 0:
            # A receipt from before the order was placed cannot be it. A
            # receipt with no shipper number is a repair-order purchase,
            # not a shipment.
            spare = sorted((p for p in by['part'].get(w['part'], [])
                            if p.free > 0 and p.shipper and not (
                                w['since'] and p.day and p.day < w['since'])),
                           key=lambda p: (p.day is None, p.day))[:HINTS]
            if spare:
                bits = []
                for p in spare:
                    where = ' / '.join(x for x in (
                        f'order {p.order_no}' if p.order_no else '',
                        f'shipper {p.shipper}' if p.shipper else '') if x)
                    when = f' on {p.day.month}/{p.day.day}' if p.day else ''
                    bits.append(f'{p.free} received{when}'
                                f'{" (" + where + ")" if where else ""}')
                hint = (hint + ' ' if hint else '') + \
                    'Same part, different order: ' + '; '.join(bits) + '.'
        out[w['id']] = (w['got'], w['how'], hint)
    return out
