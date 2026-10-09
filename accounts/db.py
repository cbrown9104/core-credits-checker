"""Postgres access for store accounts.

Accounts are ON when the DATABASE_URL environment variable is set and OFF
otherwise. With it off the app behaves exactly as it did before accounts
existed: no sign-in, nothing saved.

One small connection pool is shared by the web threads and the run worker.
The schema (accounts/schema.sql) is applied on first use and is safe to
re-apply on every start.
"""
import atexit
import os
import threading
from contextlib import contextmanager

_lock = threading.Lock()
_pool = None
_pool_url = None
_ready = set()
# Goes up every time the pool is closed, so callers that cache something
# per database (the web layer's start-up step) know to do it again.
generation = 0
_exit_hook = False

SCHEMA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           'schema.sql')


def url():
    return (os.environ.get('DATABASE_URL') or '').strip()


def enabled():
    return bool(url())


def _open_pool(u):
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool
    return ConnectionPool(
        u, min_size=1, max_size=6, timeout=15,
        kwargs={'row_factory': dict_row},
        check=ConnectionPool.check_connection, open=True)


def pool():
    """The shared pool, opened on first use (and re-opened if the URL
    changes, which only happens in tests)."""
    global _pool, _pool_url
    u = url()
    if not u:
        raise RuntimeError('Accounts are off: DATABASE_URL is not set.')
    with _lock:
        if _pool is not None and _pool_url != u:
            _pool.close()
            _pool = None
        if _pool is None:
            _pool = _open_pool(u)
            _pool_url = u
            global _exit_hook
            if not _exit_hook:      # close cleanly when the server stops
                atexit.register(close)
                _exit_hook = True
        p = _pool
        if u not in _ready:
            with p.connection() as conn:
                # one start-up at a time may touch the schema
                conn.execute('SELECT pg_advisory_xact_lock(712004)')
                with open(SCHEMA_PATH, encoding='utf-8') as fh:
                    conn.execute(fh.read())
            _ready.add(u)
    return p


def close():
    global _pool, _pool_url, generation
    with _lock:
        if _pool is not None:
            _pool.close()
        _pool = None
        _pool_url = None
        _ready.clear()
        generation += 1


@contextmanager
def connect():
    """A connection in a transaction: committed when the block ends,
    rolled back if it raises."""
    with pool().connection() as conn:
        yield conn


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
