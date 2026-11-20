"""What Shortage Check keeps for a store: its invoices and their lines,
the receipts from the DMS file, the upload log and the settings. Every
query here is tied to one store id."""
import collections
import datetime

from accounts import core, db

from . import match as matcher
from .receipts import part_key

STATUSES = ('', 'review', 'filed', 'paid', 'denied', 'found')
DEFAULT_DAYS = 14
# The facing PDC starts with fewer days. A store can change both numbers.
FACING_DAYS = 7
MAX_DAYS = 120
MAX_PARTS = 500
MAX_IDS = 5000
NOTE_MAX, CLAIM_MAX = 500, 40
SOON_DAYS = 3


def _lock(conn, store_id):
    """One change to a store's Shortage Check data at a time, held to the
    end of the transaction."""
    conn.execute('SELECT pg_advisory_xact_lock(712006, %s::int)',
                 (int(store_id) % 2147483647,))


def _central_day(dt):
    try:
        from zoneinfo import ZoneInfo
        local = dt.astimezone(ZoneInfo('America/Chicago'))
    except Exception:
        local = dt - datetime.timedelta(hours=5)
    return f'{local.strftime("%b")} {local.day}, {local.year}'


def get_settings(store_id, conn=None):
    sql = ('SELECT default_days, pdc_days, ignored_parts FROM sc_settings '
           'WHERE store_id = %s')
    row = conn.execute(sql, (store_id,)).fetchone() if conn is not None \
        else db.one(sql, (store_id,))
    if not row:
        return {'default_days': DEFAULT_DAYS, 'pdc_days': {},
                'ignored_parts': []}
    return {'default_days': row['default_days'],
            'pdc_days': dict(row['pdc_days'] or {}),
            'ignored_parts': list(row['ignored_parts'] or [])}


def save_settings(store_id, default_days, pdc_days, ignored_parts, actor):
    """Raises ValueError with a message for the person when a value is
    out of range."""
    try:
        default_days = int(default_days)
    except (TypeError, ValueError):
        raise ValueError('Days to file a claim has to be a number.')
    if not 1 <= default_days <= MAX_DAYS:
        raise ValueError(f'Days to file a claim has to be 1 to {MAX_DAYS}.')
    clean_pdc = {}
    for pdc, days in (pdc_days or {}).items():
        pdc = str(pdc).strip()
        try:
            days = int(days)
        except (TypeError, ValueError):
            raise ValueError(f'The days for PDC {pdc} have to be a number.')
        if not (pdc.isdigit() and len(pdc) == 5):
            raise ValueError(f'"{pdc}" is not a PDC code. A PDC code is '
                             f'five digits, like 03181.')
        if not 1 <= days <= MAX_DAYS:
            raise ValueError(f'The days for PDC {pdc} have to be 1 to '
                             f'{MAX_DAYS}.')
        clean_pdc[pdc] = days
    parts = []
    for p in ignored_parts or []:
        p = ''.join(ch for ch in str(p).upper() if ch.isalnum())
        if p and p not in parts:
            parts.append(p)
    if len(parts) > MAX_PARTS:
        raise ValueError(f'The ignored list can hold {MAX_PARTS} parts.')
    with db.connect() as conn:
        _lock(conn, store_id)
        conn.execute(
            'INSERT INTO sc_settings (store_id, default_days, pdc_days, '
            'ignored_parts) VALUES (%s, %s, %s, %s) ON CONFLICT (store_id) '
            'DO UPDATE SET default_days = EXCLUDED.default_days, pdc_days = '
            'EXCLUDED.pdc_days, ignored_parts = EXCLUDED.ignored_parts',
            (store_id, default_days, db.jsonb(clean_pdc), db.jsonb(parts)))
    core.audit(store_id, actor, 'sc_settings_saved',
               {'default_days': default_days, 'pdc_days': clean_pdc,
                'ignored_parts': len(parts)})


def _fuller(inv, old):
    """True when the new copy of an invoice has more than the saved one."""
    if old['complete']:
        return False
    return inv['complete'] or len(inv['lines']) > old['n']


def save_invoice(store_id, inv, filename, actor):
    """Save one parsed invoice. Returns (result, message).

    A copy that adds nothing to the saved one changes nothing. A fuller
    copy takes the place of a cut-off one, and what the store typed on a
    line (status, claim number, note) moves to the same line of the new
    copy."""
    lines = inv['lines']
    with db.connect() as conn:
        _lock(conn, store_id)
        old = conn.execute(
            'SELECT i.id, i.complete, i.created_at, (SELECT count(*) FROM '
            'sc_lines l WHERE l.invoice_id = i.id) AS n FROM sc_invoices i '
            'WHERE i.store_id = %s AND i.number = %s',
            (store_id, inv['number'])).fetchone()
        if old and not _fuller(inv, old):
            msg = f'Already loaded on {_central_day(old["created_at"])}. ' \
                  f'Nothing changed.'
            if not old['complete']:
                msg += (' The saved copy is cut off, and this file has no '
                        'more of it.')
            return 'already', msg
        typed = collections.defaultdict(list)
        charges = {code: [c['name'], float(c['amount'])]
                   for code, c in inv['charges'].items()}
        head = (inv['title'], inv['date'], inv['facing_pdc'],
                inv['net_total'], inv['lines_net'], inv['complete'],
                db.jsonb(charges), inv['pages'], filename)
        if old:
            for r in conn.execute(
                    'SELECT shipper, order_no, part, status, claim_no, '
                    'note, changed_email, changed_at FROM sc_lines WHERE '
                    'invoice_id = %s ORDER BY seq', (old['id'],)):
                typed[(r['shipper'], r['order_no'], r['part'])].append(r)
            conn.execute('DELETE FROM sc_lines WHERE invoice_id = %s',
                         (old['id'],))
            conn.execute(
                'UPDATE sc_invoices SET title = %s, invoice_date = %s, '
                'facing_pdc = %s, net_total = %s, lines_total = %s, '
                'complete = %s, charges = %s, pages = %s, filename = %s '
                'WHERE id = %s',
                head + (old['id'],))
            invoice_id = old['id']
        else:
            invoice_id = conn.execute(
                'INSERT INTO sc_invoices (store_id, number, title, '
                'invoice_date, facing_pdc, net_total, lines_total, '
                'complete, charges, pages, filename, created_by, '
                'created_email) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, '
                '%s, %s, %s, %s, %s) RETURNING id',
                (store_id, inv['number']) + head +
                ((actor or {}).get('id'), (actor or {}).get('email', ''))
            ).fetchone()['id']
            if inv['facing_pdc']:
                conn.execute(
                    'INSERT INTO sc_settings (store_id, pdc_days) VALUES '
                    '(%s, %s) ON CONFLICT (store_id) DO NOTHING',
                    (store_id, db.jsonb({inv['facing_pdc']: FACING_DAYS})))
        rows = []
        for l in lines:
            kept = typed[(l['shipper'], l['order_no'], l['part'])]
            k = kept.pop(0) if kept else {}
            rows.append((
                invoice_id, store_id, l['seq'], l['page'], l['pdc'],
                l['shipper'], l['order_no'], l['order_type'],
                l['order_date'], l['part'], l['alt_part'],
                l['description'][:60], l['qty'],
                l['unit_price'], l['net'], l['kind'], l['detail'],
                k.get('status', ''), k.get('claim_no', ''),
                k.get('note', ''), k.get('changed_email', ''),
                k.get('changed_at')))
        with conn.cursor() as cur:
            cur.executemany(
                'INSERT INTO sc_lines (invoice_id, store_id, seq, page, '
                'pdc, shipper, order_no, order_type, order_date, part, '
                'alt_part, description, qty, unit_price, net, kind, '
                'detail, status, claim_no, note, changed_email, '
                'changed_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, '
                '%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)', rows)
    core.audit(store_id, actor, 'sc_invoice_loaded',
               {'number': inv['number'], 'lines': len(lines),
                'complete': inv['complete'], 'replaced': bool(old)})
    n = f'{len(lines):,} line{"" if len(lines) == 1 else "s"}'
    if old:
        return ('loaded' if inv['complete'] else 'cut_off',
                f'This fuller copy took the place of the cut-off one: {n}. '
                f'Statuses, claim numbers and notes were kept.' +
                ('' if inv['complete'] else ' ' + inv['warnings'][-1]))
    if inv['complete']:
        return 'loaded', (f'Loaded {n}. The total matches the invoice: '
                          f'${inv["lines_net"]:,.2f}.')
    return 'cut_off', f'Loaded {n}. ' + inv['warnings'][-1] + \
        ' Send the complete PDF and it takes the place of this copy.'


def save_receipts(store_id, parsed):
    """Add the receipts of one DMS file. Returns how many were new."""
    rows = [(store_id, r['rec_key'], r['day'], r['part'], r['part_raw'],
             r['order_no'], r['shipper'], r['qty'])
            for r in parsed['receipts']]
    days = [(store_id, d, v[0], v[1]) for d, v in parsed['days'].items()]
    count = 'SELECT count(*) AS n FROM sc_receipts WHERE store_id = %s'
    with db.connect() as conn:
        _lock(conn, store_id)
        before = conn.execute(count, (store_id,)).fetchone()['n']
        with conn.cursor() as cur:
            if rows:
                cur.executemany(
                    'INSERT INTO sc_receipts (store_id, rec_key, day, '
                    'part, part_raw, order_no, shipper, qty) VALUES (%s, '
                    '%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (store_id, '
                    'rec_key) DO NOTHING', rows)
            cur.executemany(
                'INSERT INTO sc_days (store_id, day, row_count, receipts) '
                'VALUES (%s, %s, %s, %s) ON CONFLICT (store_id, day) DO '
                'UPDATE SET row_count = GREATEST(sc_days.row_count, '
                'EXCLUDED.row_count), receipts = GREATEST(sc_days.receipts, '
                'EXCLUDED.receipts)', days)
        return conn.execute(count, (store_id,)).fetchone()['n'] - before


def _state(line, got, ignored):
    if line['kind'] == 'core':
        return 'core'
    if line['kind'] != 'part':
        return 'none'           # billed at nothing: no money to claim
    if part_key(line['part']) in ignored:
        return 'ignored'
    if line['qty'] <= 0:
        return 'none'
    if got >= line['qty']:
        return 'received'
    return 'partial' if got > 0 else 'short'


def rematch(store_id):
    """Work out again, for every line of the store, what was received,
    how it matched and when its deadline is. Returns how many lines
    changed."""
    with db.connect() as conn:
        _lock(conn, store_id)
        s = get_settings(store_id, conn)
        ignored = {part_key(p) for p in s['ignored_parts']}
        lines = conn.execute(
            'SELECT l.id, l.part, l.alt_part, l.shipper, l.order_no, '
            'l.order_date, l.qty, l.kind, l.seq, l.pdc, l.received, l.how, '
            'l.hint, '
            'l.state, l.deadline, i.invoice_date, i.number FROM sc_lines l '
            'JOIN sc_invoices i ON i.id = l.invoice_id WHERE l.store_id = '
            '%s', (store_id,)).fetchall()
        receipts = conn.execute(
            'SELECT part, shipper, order_no, qty, day, part_raw FROM '
            'sc_receipts WHERE store_id = %s', (store_id,)).fetchall()
        for l in lines:
            l['sort'] = (l['invoice_date'] or datetime.date.min,
                         l['number'], l['seq'])
        found = matcher.match(lines, receipts, ignored)
        changes = []
        for l in lines:
            got, how, hint = found.get(l['id'], (0, '', ''))
            state = _state(l, got, ignored)
            days = s['pdc_days'].get(l['pdc'], s['default_days'])
            deadline = l['invoice_date'] + datetime.timedelta(
                days=int(days)) if l['invoice_date'] else None
            if (got, how, hint, state, deadline) != (
                    l['received'], l['how'], l['hint'], l['state'],
                    l['deadline']):
                changes.append((got, how, hint, state, deadline, l['id']))
        if changes:
            with conn.cursor() as cur:
                cur.executemany(
                    'UPDATE sc_lines SET received = %s, how = %s, hint = '
                    '%s, state = %s, deadline = %s WHERE id = %s', changes)
    return len(changes)


_SHORT = "l.state IN ('short', 'partial')"
# A short line is open until someone files a claim for it or closes it.
_OPEN = _SHORT + " AND l.status IN ('', 'review')"
# what the store was billed for the pieces it did not receive (the net
# amount: a promotion discount is already off)
_VALUE = 'round((l.qty - l.received) * l.net / NULLIF(l.qty, 0), 2)'


def facts(store_id, today):
    """The numbers at the top of the page, over invoices not marked done."""
    soon = today + datetime.timedelta(days=SOON_DAYS)
    return db.one(
        f'SELECT count(*) FILTER (WHERE {_OPEN}) AS open_n, '
        f'coalesce(sum({_VALUE}) FILTER (WHERE {_OPEN}), 0) AS open_v, '
        f'count(*) FILTER (WHERE {_OPEN} AND l.deadline BETWEEN %(t)s AND '
        f'%(soon)s) AS soon_n, '
        f'coalesce(sum({_VALUE}) FILTER (WHERE {_OPEN} AND l.deadline '
        f'BETWEEN %(t)s AND %(soon)s), 0) AS soon_v, '
        f'count(*) FILTER (WHERE {_OPEN} AND l.deadline < %(t)s) AS late_n, '
        f'coalesce(sum({_VALUE}) FILTER (WHERE {_OPEN} AND l.deadline < '
        f'%(t)s), 0) AS late_v, '
        f"count(*) FILTER (WHERE {_SHORT} AND l.status = 'filed') AS "
        f'filed_n, '
        f"coalesce(sum({_VALUE}) FILTER (WHERE {_SHORT} AND l.status = "
        f"'filed'), 0) AS filed_v "
        f'FROM sc_lines l JOIN sc_invoices i ON i.id = l.invoice_id WHERE '
        f'l.store_id = %(s)s AND NOT i.done',
        {'s': store_id, 't': today, 'soon': soon})


def invoice_list(store_id):
    return db.rows(
        f'SELECT i.id, i.number, i.title, i.invoice_date, i.complete, '
        f'i.net_total, i.lines_total, i.done, i.filename, i.created_at, '
        f'count(l.id) AS n_lines, '
        f"count(l.id) FILTER (WHERE l.state = 'received') AS n_received, "
        f'count(l.id) FILTER (WHERE {_OPEN}) AS n_open, '
        f'coalesce(sum({_VALUE}) FILTER (WHERE {_OPEN}), 0) AS open_v, '
        f"count(l.id) FILTER (WHERE {_SHORT} AND l.status = 'filed') AS "
        f'n_filed, '
        f"count(l.id) FILTER (WHERE {_SHORT} AND l.status IN ('paid', "
        f"'denied', 'found')) AS n_closed, "
        f'min(l.deadline) FILTER (WHERE {_OPEN}) AS next_deadline '
        f'FROM sc_invoices i LEFT JOIN sc_lines l ON l.invoice_id = i.id '
        f'WHERE i.store_id = %s GROUP BY i.id ORDER BY i.invoice_date DESC '
        f'NULLS LAST, i.number DESC', (store_id,))


def get_invoice(store_id, invoice_id):
    return db.one('SELECT * FROM sc_invoices WHERE store_id = %s AND id = '
                  '%s', (store_id, invoice_id))


def lines_for(store_id, invoice_id=None):
    """The lines of one invoice, or with no invoice every short line that
    is not closed, across the invoices not marked done."""
    where, params = 'l.store_id = %s', [store_id]
    if invoice_id is not None:
        where += ' AND l.invoice_id = %s'
        params.append(invoice_id)
    else:
        where += (f" AND {_SHORT} AND l.status NOT IN ('paid', 'denied', "
                  f"'found') AND NOT i.done")
    return db.rows(
        'SELECT l.id, i.number, i.invoice_date, l.seq, l.pdc, l.shipper, '
        'l.order_no, l.order_type, l.order_date, l.part, l.description, '
        'l.qty, l.unit_price, l.net, l.detail, l.received, l.how, l.hint, '
        'l.state, l.deadline, l.status, l.claim_no, l.note FROM sc_lines l '
        'JOIN sc_invoices i ON i.id = l.invoice_id WHERE ' + where +
        ' ORDER BY i.invoice_date, i.number, l.seq', params)


def update_lines(store_id, ids, changes, actor):
    """Set the status, claim number or note on lines of this store.
    Returns how many lines changed. Raises ValueError for a bad value."""
    if not isinstance(ids, list) or not ids or \
            not all(isinstance(i, int) and not isinstance(i, bool)
                    for i in ids):
        raise ValueError('Pick the lines first.')
    if len(ids) > MAX_IDS:
        raise ValueError(f'Pick {MAX_IDS:,} lines or fewer at a time.')
    sets, params = [], []
    if 'status' in changes:
        if changes['status'] not in STATUSES:
            raise ValueError('That status is not one of the choices.')
        sets.append('status = %s')
        params.append(changes['status'])
    if 'claim_no' in changes:
        claim = ' '.join(changes['claim_no'].split())[:CLAIM_MAX]
        sets.append('claim_no = %s')
        params.append(claim)
        if claim and 'status' not in changes:
            # a claim number on a line nobody has filed yet files it
            sets.append("status = CASE WHEN status IN ('', 'review') THEN "
                        "'filed' ELSE status END")
    if 'note' in changes:
        sets.append('note = %s')
        params.append(' '.join(changes['note'].split())[:NOTE_MAX])
    if not sets:
        raise ValueError('Nothing to change.')
    return db.execute(
        'UPDATE sc_lines SET ' + ', '.join(sets) + ', changed_email = %s, '
        'changed_at = now() WHERE store_id = %s AND id = ANY(%s)',
        params + [(actor or {}).get('email', ''), store_id, ids])


def set_done(store_id, invoice_id, done):
    return db.execute('UPDATE sc_invoices SET done = %s WHERE store_id = %s '
                      'AND id = %s', (bool(done), store_id, invoice_id))


def delete_invoice(store_id, invoice_id, actor):
    """Remove an invoice and its lines. Returns its number, or None."""
    with db.connect() as conn:
        _lock(conn, store_id)
        row = conn.execute(
            'DELETE FROM sc_invoices WHERE store_id = %s AND id = %s '
            'RETURNING number', (store_id, invoice_id)).fetchone()
    if not row:
        return None
    core.audit(store_id, actor, 'sc_invoice_deleted',
               {'number': row['number']})
    return row['number']


def log_upload(store_id, filename, kind, result, message, actor):
    db.execute('INSERT INTO sc_uploads (store_id, filename, kind, result, '
               'message, user_email) VALUES (%s, %s, %s, %s, %s, %s)',
               (store_id, filename[:200], kind, result, message[:600],
                (actor or {}).get('email', '')))


def recent_uploads(store_id, limit=15):
    return db.rows('SELECT filename, kind, result, message, user_email, '
                   'created_at FROM sc_uploads WHERE store_id = %s ORDER BY '
                   'id DESC LIMIT %s', (store_id, limit))


def days(store_id, first, last):
    """{day: (rows, receipts)} of the DMS file between two dates."""
    return {r['day']: (r['row_count'], r['receipts']) for r in db.rows(
        'SELECT day, row_count, receipts FROM sc_days WHERE store_id = %s '
        'AND day BETWEEN %s AND %s', (store_id, first, last))}


def coverage(store_id):
    """First and last day of DMS data on file, or None."""
    row = db.one('SELECT min(day) AS first, max(day) AS last FROM sc_days '
                 'WHERE store_id = %s AND row_count > 0', (store_id,))
    return row if row and row['first'] else None
