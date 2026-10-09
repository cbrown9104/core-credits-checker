"""Stores, users, memberships, one-time sign-in links and sessions.

Rules (from the pilot plan):
- The account belongs to the store, not a person. Every user in a store
  sees the same master, history and results.
- Roles: admin (adds/removes users) and user (runs reconciliations).
- A person can belong to more than one store.
- Nobody has a password. Sign-in is a one-time link sent to the email.
- An owner (OWNER_EMAIL) runs the service and can open every store.
"""
import datetime
import hashlib
import os
import re
import secrets

from . import db

ROLES = ('admin', 'user')
TOKEN_MINUTES = 20
SESSION_DAYS = 30
# Sign-in requests allowed in WINDOW_MINUTES: for one email from one
# address, for one email from anywhere, and from one address for any email.
MAX_PER_EMAIL_AND_IP = 5
MAX_PER_EMAIL = 15
MAX_PER_IP = 30
WINDOW_MINUTES = 15

EMAIL_RE = re.compile(r'^[^@\s<>"\',;]+@[^@\s<>"\',;]+\.[A-Za-z]{2,}$')


class AccountError(Exception):
    """A problem to show the person as-is."""


def norm_email(value):
    return (value or '').strip().lower()


def valid_email(value):
    v = norm_email(value)
    if len(v) > 254 or any(ord(ch) < 33 or ord(ch) == 127 for ch in v):
        return False
    return bool(EMAIL_RE.match(v))


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _hash(token):
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


# ── settings / secrets ──────────────────────────────────────────────────
def secret(key):
    """A random secret kept in the database (created on first use), so the
    cookie-signing key survives restarts without anyone handling it."""
    row = db.one('SELECT value FROM settings WHERE key = %s', (key,))
    if row:
        return row['value']
    value = secrets.token_hex(32)
    db.execute('INSERT INTO settings (key, value) VALUES (%s, %s) '
               'ON CONFLICT (key) DO NOTHING', (key, value))
    return db.one('SELECT value FROM settings WHERE key = %s',
                  (key,))['value']


# ── users ───────────────────────────────────────────────────────────────
def user_by_id(user_id):
    return db.one('SELECT * FROM users WHERE id = %s', (user_id,))


def user_by_email(email):
    return db.one('SELECT * FROM users WHERE email = %s',
                  (norm_email(email),))


def ensure_user(email, conn=None):
    email = norm_email(email)
    if not valid_email(email):
        raise AccountError(f'"{email}" is not a valid email address.')
    sql = ('INSERT INTO users (email) VALUES (%s) '
           'ON CONFLICT (email) DO UPDATE SET email = EXCLUDED.email '
           'RETURNING *')
    if conn is not None:
        return conn.execute(sql, (email,)).fetchone()
    return db.one(sql, (email,))


def owner_emails():
    raw = os.environ.get('OWNER_EMAIL', '')
    return [e for e in (norm_email(x) for x in re.split(r'[,;\s]+', raw))
            if valid_email(e)]


def ensure_owners():
    """Make sure every OWNER_EMAIL address has an owner account. Addresses
    taken off the list stop being owners."""
    emails = owner_emails()
    with db.connect() as conn:
        for e in emails:
            conn.execute(
                'INSERT INTO users (email, is_owner) VALUES (%s, TRUE) '
                'ON CONFLICT (email) DO UPDATE SET is_owner = TRUE', (e,))
        conn.execute('UPDATE users SET is_owner = FALSE '
                     'WHERE is_owner AND NOT (email = ANY(%s))', (emails,))
    return emails


def can_sign_in(user, conn=None):
    """A person can sign in when they are an owner or on at least one
    store."""
    if not user:
        return False
    if user['is_owner']:
        return True
    sql = 'SELECT 1 AS x FROM memberships WHERE user_id = %s LIMIT 1'
    if conn is not None:
        return bool(conn.execute(sql, (user['id'],)).fetchone())
    return bool(db.one(sql, (user['id'],)))


# ── stores and memberships ──────────────────────────────────────────────
def store_by_id(store_id):
    return db.one('SELECT * FROM stores WHERE id = %s', (store_id,))


def stores_for(user_id):
    """The stores a person is on, with who added them and when."""
    return db.rows(
        'SELECT s.*, m.role, m.added_email, m.created_at AS added_at '
        'FROM memberships m JOIN stores s ON s.id = m.store_id '
        'WHERE m.user_id = %s ORDER BY s.name, s.id', (user_id,))


def role_in(user_id, store_id):
    row = db.one('SELECT role FROM memberships WHERE user_id = %s AND '
                 'store_id = %s', (user_id, store_id))
    return row['role'] if row else None


def remember_store(user_id, store_id):
    """Note the store a person just opened, so the next sign-in lands
    there (and never in a store someone else added them to since)."""
    with db.connect() as conn:
        conn.execute('UPDATE users SET last_store_id = %s WHERE id = %s',
                     (store_id, user_id))
        conn.execute('UPDATE memberships SET last_used_at = now() WHERE '
                     'user_id = %s AND store_id = %s', (user_id, store_id))


def all_stores():
    return db.rows('''
        SELECT s.*,
               (SELECT count(*) FROM memberships m WHERE m.store_id = s.id)
                   AS user_count,
               (SELECT max(r.created_at) FROM runs r WHERE r.store_id = s.id)
                   AS last_run_at,
               (SELECT count(*) FROM runs r WHERE r.store_id = s.id)
                   AS run_count,
               mm.row_count AS master_rows, mm.unpaid_count,
               mm.unpaid_amount, mm.created_at AS master_at
        FROM stores s
        LEFT JOIN masters mm ON mm.store_id = s.id AND mm.is_current
        ORDER BY s.name, s.id''')


def clean_dealer(value):
    return re.sub(r'\D', '', value or '')[:8]


def _clean_name(name):
    name = re.sub(r'[\x00-\x1f\x7f]', ' ', name or '')
    name = re.sub(r'\s+', ' ', name).strip()
    if not name:
        raise AccountError('Enter the store name.')
    if len(name) > 120:
        raise AccountError('The store name is too long (120 characters '
                           'at most).')
    return name


def create_store(name, dealer_code, admin_email, actor):
    """New store with its first admin. Returns (store, admin_user)."""
    name = _clean_name(name)
    if not valid_email(admin_email):
        raise AccountError('Enter a valid email for the store\'s first '
                           'admin.')
    with db.connect() as conn:
        store = conn.execute(
            'INSERT INTO stores (name, dealer_code, created_by) '
            'VALUES (%s, %s, %s) RETURNING *',
            (name, clean_dealer(dealer_code), actor['id'])).fetchone()
        user = ensure_user(admin_email, conn)
        conn.execute(
            'INSERT INTO memberships (user_id, store_id, role, added_by, '
            'added_email) VALUES (%s, %s, %s, %s, %s)',
            (user['id'], store['id'], 'admin', actor['id'], actor['email']))
        _audit(conn, store['id'], actor, 'store_created',
               {'name': name, 'admin': user['email']})
    return store, user


def update_store(store_id, name, dealer_code, threshold_days, actor):
    name = _clean_name(name)
    try:
        th = max(1, min(365, int(threshold_days)))
    except (TypeError, ValueError):
        th = 60
    with db.connect() as conn:
        conn.execute('UPDATE stores SET name = %s, dealer_code = %s, '
                     'threshold_days = %s WHERE id = %s',
                     (name, clean_dealer(dealer_code), th, store_id))
        _audit(conn, store_id, actor, 'store_updated',
               {'name': name, 'dealer_code': clean_dealer(dealer_code),
                'threshold_days': th})


def members(store_id):
    """People on a store. "Last opened" is their activity in THIS store
    only; what they do in other stores is none of this store's business."""
    return db.rows(
        'SELECT u.id, u.email, u.is_owner, m.role, m.created_at, '
        'm.added_email, m.last_used_at FROM memberships m JOIN users u '
        'ON u.id = m.user_id WHERE m.store_id = %s ORDER BY m.role, '
        'u.email', (store_id,))


def _lock_store(conn, store_id):
    """One membership change per store at a time (keeps the "at least one
    admin" rule true even when two admins click at the same moment)."""
    if not conn.execute('SELECT id FROM stores WHERE id = %s FOR UPDATE',
                        (store_id,)).fetchone():
        raise AccountError('That store no longer exists.')


def _keep_one_admin(conn, store_id, leaving_user_id):
    others = conn.execute(
        "SELECT count(*) AS n FROM memberships WHERE store_id = %s AND "
        "role = 'admin' AND user_id <> %s",
        (store_id, leaving_user_id)).fetchone()['n']
    if not others:
        raise AccountError('A store needs at least one admin. Make someone '
                           'else an admin first.')


def add_member(store_id, email, role, actor):
    """Add (or re-role) a person on a store. Returns (user, created)."""
    if role not in ROLES:
        raise AccountError('Pick a role: admin or user.')
    if not valid_email(email):
        raise AccountError('Enter a valid work email address.')
    with db.connect() as conn:
        _lock_store(conn, store_id)
        user = ensure_user(email, conn)
        had = conn.execute(
            'SELECT role FROM memberships WHERE user_id = %s AND '
            'store_id = %s', (user['id'], store_id)).fetchone()
        if had and had['role'] == 'admin' and role != 'admin':
            _keep_one_admin(conn, store_id, user['id'])
        conn.execute(
            'INSERT INTO memberships (user_id, store_id, role, added_by, '
            'added_email) VALUES (%s, %s, %s, %s, %s) '
            'ON CONFLICT (user_id, store_id) DO UPDATE SET '
            'role = EXCLUDED.role',
            (user['id'], store_id, role, actor['id'], actor['email']))
        _audit(conn, store_id, actor,
               'user_role_changed' if had else 'user_added',
               {'email': user['email'], 'role': role})
    return user, not had


def remove_member(store_id, user_id, actor):
    with db.connect() as conn:
        _lock_store(conn, store_id)
        row = conn.execute(
            'SELECT u.*, m.role FROM memberships m JOIN users u '
            'ON u.id = m.user_id WHERE m.store_id = %s AND m.user_id = %s',
            (store_id, user_id)).fetchone()
        if not row:
            raise AccountError('That person is not on this store.')
        if row['role'] == 'admin':
            _keep_one_admin(conn, store_id, user_id)
        conn.execute('DELETE FROM memberships WHERE store_id = %s AND '
                     'user_id = %s', (store_id, user_id))
        # Invitations to THIS store stop working. If the person has no
        # store left at all, so does everything else: every link sent to
        # them and every browser they are signed in on.
        conn.execute('UPDATE login_tokens SET used_at = now() WHERE '
                     'user_id = %s AND store_id = %s AND used_at IS NULL',
                     (user_id, store_id))
        conn.execute('UPDATE users SET last_store_id = NULL WHERE id = %s '
                     'AND last_store_id = %s', (user_id, store_id))
        if not can_sign_in(row, conn):
            conn.execute('UPDATE login_tokens SET used_at = now() WHERE '
                         'user_id = %s AND used_at IS NULL', (user_id,))
            conn.execute('DELETE FROM sessions WHERE user_id = %s',
                         (user_id,))
        _audit(conn, store_id, actor, 'user_removed',
               {'email': row['email']})
    return row['email']


# ── audit ───────────────────────────────────────────────────────────────
def _audit(conn, store_id, actor, action, detail=None):
    conn.execute(
        'INSERT INTO audit_log (store_id, user_id, user_email, action, '
        'detail) VALUES (%s, %s, %s, %s, %s)',
        (store_id, (actor or {}).get('id'), (actor or {}).get('email', ''),
         action, db.jsonb(detail or {})))


def audit(store_id, actor, action, detail=None):
    with db.connect() as conn:
        _audit(conn, store_id, actor, action, detail)


# ── one-time sign-in links ──────────────────────────────────────────────
def too_many_requests(email, ip):
    """Record this sign-in request and say whether it is over a limit.

    The tight limit is per email AND address, so someone hammering another
    person's email from elsewhere cannot lock that person out; the looser
    per-email and per-address ceilings stop a flood."""
    email = norm_email(email)[:254]
    ip = (ip or '')[:64]
    since = _now() - datetime.timedelta(minutes=WINDOW_MINUTES)
    with db.connect() as conn:
        def count(where, params):
            return conn.execute(
                f'SELECT count(*) AS n FROM login_attempts WHERE {where} '
                f'AND created_at > %s', params + (since,)).fetchone()['n']

        over = (count('email = %s AND ip = %s', (email, ip)) >=
                MAX_PER_EMAIL_AND_IP or
                count('email = %s', (email,)) >= MAX_PER_EMAIL or
                (bool(ip) and count('ip = %s', (ip,)) >= MAX_PER_IP))
        if not over:
            # Only requests that go through are counted, so a refused
            # flood cannot run up someone else's allowance.
            conn.execute('INSERT INTO login_attempts (email, ip) VALUES '
                         '(%s, %s)', (email, ip))
        conn.execute("DELETE FROM login_attempts WHERE created_at < "
                     "now() - interval '2 days'")
    return over


def new_login_token(user_id, minutes=TOKEN_MINUTES, store_id=None):
    """A fresh link token. store_id (invitations) is the store the link
    opens after sign-in."""
    token = secrets.token_urlsafe(32)
    expires = _now() + datetime.timedelta(minutes=minutes)
    with db.connect() as conn:
        conn.execute(
            'INSERT INTO login_tokens (user_id, token_hash, store_id, '
            'expires_at) VALUES (%s, %s, %s, %s)',
            (user_id, _hash(token), store_id, expires))
        conn.execute("DELETE FROM login_tokens WHERE expires_at < "
                     "now() - interval '7 days'")
    return token


def peek_login_token(token):
    """The user a still-valid link belongs to, without using it up."""
    if not token or len(token) > 200:
        return None
    return db.one(
        'SELECT u.* FROM login_tokens t JOIN users u ON u.id = t.user_id '
        'WHERE t.token_hash = %s AND t.used_at IS NULL AND '
        't.expires_at > now()', (_hash(token),))


def use_login_token(token):
    """Use a link up. Returns (user, store_id) or (None, None) if it was
    already used, expired or never existed. A link works exactly once, and
    using one retires every other link that person still had."""
    if not token or len(token) > 200:
        return None, None
    with db.connect() as conn:
        owner = conn.execute('SELECT user_id FROM login_tokens WHERE '
                             'token_hash = %s', (_hash(token),)).fetchone()
        if not owner:
            return None, None
        # One sign-in per person at a time: two of their links pressed at
        # the same moment take turns instead of blocking each other.
        conn.execute('SELECT id FROM users WHERE id = %s FOR UPDATE',
                     (owner['user_id'],))
        row = conn.execute(
            'UPDATE login_tokens SET used_at = now() WHERE token_hash = %s '
            'AND used_at IS NULL AND expires_at > now() '
            'RETURNING user_id, store_id', (_hash(token),)).fetchone()
        if not row:
            return None, None
        conn.execute('UPDATE login_tokens SET used_at = now() WHERE '
                     'user_id = %s AND used_at IS NULL', (row['user_id'],))
        user = conn.execute(
            'UPDATE users SET last_login_at = now() WHERE id = %s '
            'RETURNING *', (row['user_id'],)).fetchone()
    return user, row['store_id']


# ── sessions (one row per signed-in browser) ────────────────────────────
def new_session(user_id):
    """Start a session; returns the random id that goes in the cookie."""
    sid = secrets.token_urlsafe(32)
    expires = _now() + datetime.timedelta(days=SESSION_DAYS)
    with db.connect() as conn:
        conn.execute('INSERT INTO sessions (id, user_id, expires_at) '
                     'VALUES (%s, %s, %s)', (_hash(sid), user_id, expires))
        conn.execute('DELETE FROM sessions WHERE expires_at < now()')
    return sid


def session_user(sid):
    """(user, refreshed) for a live session id, else (None, False).
    refreshed is True about once an hour, when the 30 days were renewed."""
    if not sid or not isinstance(sid, str) or len(sid) > 200:
        return None, False
    key = _hash(sid)
    row = db.one(
        'SELECT u.*, s.last_seen_at AS _seen FROM sessions s JOIN users u '
        'ON u.id = s.user_id WHERE s.id = %s AND s.expires_at > now()',
        (key,))
    if not row:
        return None, False
    seen = row.pop('_seen')
    refreshed = False
    if _now() - seen > datetime.timedelta(hours=1):
        db.execute('UPDATE sessions SET last_seen_at = now(), '
                   'expires_at = %s WHERE id = %s',
                   (_now() + datetime.timedelta(days=SESSION_DAYS), key))
        refreshed = True
    return row, refreshed


def end_session(sid):
    if sid and isinstance(sid, str) and len(sid) <= 200:
        db.execute('DELETE FROM sessions WHERE id = %s', (_hash(sid),))


def end_all_sessions(user_id):
    db.execute('DELETE FROM sessions WHERE user_id = %s', (user_id,))
