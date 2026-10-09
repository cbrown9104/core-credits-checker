"""Postgres access for store accounts.

Accounts are ON when the DATABASE_URL environment variable is set and OFF
otherwise. With it off the app behaves exactly as it did before accounts
existed: no sign-in, nothing saved.

One small connection pool is shared by the web threads and the run worker.
The tables (accounts/schema.sql) are put in place on first use, and again
whenever that file changes.

When the database cannot be reached, every helper here raises Unavailable.
The web layer turns that into a plain "try again in a minute" answer.
"""
import atexit
import hashlib
import os
import re
import threading
import time
from contextlib import contextmanager

_lock = threading.Lock()
_pool = None
_pool_url = None
_ready = set()
# Goes up every time the pool is closed, so callers that cache something
# per database (the web layer's start-up step) know to do it again.
generation = 0
_exit_hook = False

# How long one request waits for a connection before giving up.
WAIT_SECONDS = 10
# After a failed attempt, nobody tries again for RETRY_AFTER seconds, and
# then only one request at a time does (waiting PROBE_SECONDS at most).
# Everyone else is answered at once. There are only a few web threads, and
# without this an outage would park all of them in a queue of time-outs.
RETRY_AFTER = 5
PROBE_SECONDS = 4
_down = False
_down_until = 0.0
_probe = threading.Lock()

SCHEMA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           'schema.sql')
URL_RE = re.compile(r'^postgres(ql)?://\S+$')

NOT_A_URL = ('DATABASE_URL is not a database address. It has to start with '
             'postgresql:// (on Render: open the database, copy "Internal '
             'Database URL", and paste that as the value).')


class Unavailable(Exception):
    """The database cannot be reached right now: it is restarting, the
    network dropped, or DATABASE_URL is wrong."""


def url():
    return (os.environ.get('DATABASE_URL') or '').strip()


def enabled():
    return bool(url())


_checked_url = None


def check_url(u):
    """Raise Unavailable unless u is shaped like a database address.

    Done before the address is handed to the driver: the driver's own
    complaint about a malformed address can quote all of it, password
    included, and that would end up in the server log."""
    global _checked_url
    if u and u == _checked_url:
        return
    if not URL_RE.match(u or ''):
        raise Unavailable(NOT_A_URL)
    try:
        from psycopg.conninfo import conninfo_to_dict
        conninfo_to_dict(u)
    except Exception:
        raise Unavailable(NOT_A_URL) from None
    _checked_url = u


def brief(e, u=None):
    """One line about a connection problem, for the server log. The
    address and its password are taken out: the driver's text can quote
    them."""
    text = ' '.join(str(e).split())
    u = u if u is not None else url()
    if u:
        text = text.replace(u, '(address)')
        try:
            from psycopg.conninfo import conninfo_to_dict
            secret = conninfo_to_dict(u).get('password')
        except Exception:
            return e.__class__.__name__
        if secret:
            text = text.replace(str(secret), '***')
    return f'{e.__class__.__name__}: {text[:300]}'


def _open_pool(u):
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool
    return ConnectionPool(
        u, min_size=1, max_size=6, timeout=WAIT_SECONDS,
        kwargs={
            'row_factory': dict_row,
            'application_name': 'core-credits-checker',
            # Give up on a server that does not answer, and notice a
            # connection whose other end has gone away, within seconds
            # instead of the operating system's many minutes.
            'connect_timeout': 10,
            'keepalives': 1, 'keepalives_idle': 30,
            'keepalives_interval': 10, 'keepalives_count': 3,
            'tcp_user_timeout': 10000,
        },
        # After an outage the pool retries on a slowing schedule. Start
        # that schedule over every 30 seconds, so the site is back within
        # seconds of the database, not minutes.
        reconnect_timeout=30,
        check=ConnectionPool.check_connection, open=True)


def schema_version():
    """A fingerprint of schema.sql. The tables are (re)applied whenever
    it differs from the one recorded in the database."""
    with open(SCHEMA_PATH, 'rb') as fh:
        return hashlib.sha256(fh.read()).hexdigest()[:20]


def _apply_schema(p, wait):
    """Put the tables in place, once per version of schema.sql.

    An ordinary start finds the recorded fingerprint and changes nothing,
    so it takes no locks on tables that a running copy of the app is
    using (a new version starts while the old one is still serving).
    Returns True when it had to apply the file."""
    want = schema_version()
    with p.connection(timeout=wait) as conn:
        # one start-up at a time may touch the schema
        conn.execute('SELECT pg_advisory_xact_lock(712004)')
        have = None
        if conn.execute("SELECT to_regclass('settings') AS t"
                        ).fetchone()['t']:
            row = conn.execute("SELECT value FROM settings WHERE key = "
                               "'schema_version'").fetchone()
            have = row['value'] if row else None
        if have == want:
            return False
        with open(SCHEMA_PATH, encoding='utf-8') as fh:
            conn.execute(fh.read())
        conn.execute(
            "INSERT INTO settings (key, value) VALUES ('schema_version', "
            "%s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            (want,))
    return True


def _connect_errors():
    """What "could not get a connection" looks like (the pool's time-out
    is an OperationalError too)."""
    import psycopg
    return (psycopg.OperationalError, OSError)


def _mark_down(e):
    global _down, _down_until
    _down = True
    _down_until = time.monotonic() + RETRY_AFTER
    print(f'[db] cannot reach the database: {brief(e)}', flush=True)


def _mark_up():
    global _down
    if _down:
        _down = False
        print('[db] the database is reachable again', flush=True)


def _pool_for(u, wait):
    """The pool for this address, with the tables in place."""
    global _pool, _pool_url, _exit_hook
    with _lock:
        if _down and time.monotonic() < _down_until:
            # someone ahead of us in this queue just found it down
            raise Unavailable('The database could not be reached.')
        if _pool is not None and _pool_url != u:
            _pool.close()
            _pool = None
        if _pool is None:
            _pool = _open_pool(u)
            _pool_url = u
            if not _exit_hook:      # close cleanly when the server stops
                atexit.register(close)
                _exit_hook = True
        if u not in _ready:
            _apply_schema(_pool, wait)
            _ready.add(u)
        return _pool


def _checkout():
    """(pool, connection), or Unavailable. Whoever gets a connection gives
    it back with pool.putconn()."""
    u = url()
    if not u:
        raise RuntimeError('Accounts are off: DATABASE_URL is not set.')
    check_url(u)
    probing = False
    if _down:
        if time.monotonic() < _down_until or \
                not _probe.acquire(blocking=False):
            raise Unavailable('The database could not be reached a moment '
                              'ago.')
        probing = True
    try:
        wait = PROBE_SECONDS if probing else WAIT_SECONDS
        try:
            p = _pool_for(u, wait)
            conn = p.getconn(timeout=wait)
        except _connect_errors() as e:
            _mark_down(e)
            raise Unavailable('The database could not be reached.') from e
        _mark_up()
        return p, conn
    finally:
        if probing:
            _probe.release()


def pool():
    """The shared pool, opened on first use (and re-opened if the URL
    changes, which only happens in tests)."""
    p, conn = _checkout()
    p.putconn(conn)
    return p


def close():
    global _pool, _pool_url, generation, _down, _down_until
    with _lock:
        if _pool is not None:
            _pool.close()
        _pool = None
        _pool_url = None
        _ready.clear()
        _down = False
        _down_until = 0.0
        generation += 1


@contextmanager
def connect():
    """A connection in a transaction: committed when the block ends,
    rolled back if it raises. Raises Unavailable when no connection can
    be had."""
    p, conn = _checkout()
    try:
        with conn:
            yield conn
    finally:
        p.putconn(conn)


def rows(sql, params=None):
    with connect() as conn:
        return conn.execute(sql, params).fetchall()


def one(sql, params=None):
    with connect() as conn:
        return conn.execute(sql, params).fetchone()


def execute(sql, params=None):
    with connect() as conn:
        return conn.execute(sql, params).rowcount


def jsonb(value):
    from psycopg.types.json import Jsonb
    return Jsonb(value)
