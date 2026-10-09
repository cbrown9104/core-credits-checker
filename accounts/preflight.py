"""Start-up check. Runs before the web server starts (see the Dockerfile).

With accounts off (no DATABASE_URL) it does nothing and the site starts
open, as it always has.

With accounts on it makes sure the database answers and its tables are in
place, and that sign-in can work at all. If not, it says why in plain
words and exits. The web server then never starts, so the hosting service
marks the deploy as failed and keeps the previous version running,
instead of putting up a site that nobody can sign in to.

    python -m accounts.preflight

The database password is never printed.
"""
import os
import signal
import sys
import time

from . import db

# How long to keep trying a database that does not answer yet (a new one
# takes a minute or two to come up).
try:
    WAIT_SECONDS = max(0, int(os.environ.get('PREFLIGHT_WAIT_SECONDS', '')))
except ValueError:
    WAIT_SECONDS = 90
PAUSE_SECONDS = 3


def say(text):
    print(f'[start] {text}', flush=True)


def stop(reason, fixes=()):
    say('NOT STARTED. ' + reason)
    for line in fixes:
        say('  - ' + line)
    return 1


def explain(e):
    """(plain reason, can_it_fix_itself) for a failed connection."""
    text = ' '.join(str(e).split()).lower()
    state = getattr(e, 'sqlstate', None) or ''
    if state.startswith('28') or 'password authentication failed' in text \
            or 'no pg_hba.conf entry' in text or \
            ('role' in text and 'does not exist' in text):
        return ('The database refused the user name or password in '
                'DATABASE_URL. Copy the address again from the database\'s '
                'page.', False)
    if state == '3D000' or ('database' in text and 'does not exist' in text):
        return ('The database named at the end of DATABASE_URL does not '
                'exist. Copy the address again from the database\'s page.',
                False)
    if 'could not translate host name' in text or \
            'name or service not known' in text or \
            'nodename nor servname' in text or \
            'temporary failure in name resolution' in text:
        return ('The database server named in DATABASE_URL was not found. '
                'Check that the whole address was copied, and that the '
                'database is in the same region as this service (the '
                'internal address only works inside one region).', True)
    if 'starting up' in text or state == '57P03':
        return ('The database is still starting.', True)
    if 'timeout' in text or 'timed out' in text:
        return ('The database did not answer in time.', True)
    if 'connection refused' in text:
        return ('The database server refused the connection (it may still '
                'be starting).', True)
    return ('The database could not be reached (' + db.brief(e) + ').', True)


def _reach(u):
    """Open one connection, trying for up to WAIT_SECONDS. Returns None
    when it worked, else the plain reason it did not."""
    import psycopg
    deadline = time.monotonic() + WAIT_SECONDS
    told = None
    while True:
        try:
            with psycopg.connect(
                    u, connect_timeout=10,
                    application_name='core-credits-checker-start') as conn:
                conn.execute('SELECT 1')
            return None
        except (psycopg.OperationalError, OSError) as e:
            reason, may_pass = explain(e)
        except Exception as e:      # an address the driver cannot use
            reason, may_pass = ('The database could not be reached ('
                                f'{e.__class__.__name__}).'), False
        if not may_pass or time.monotonic() + PAUSE_SECONDS >= deadline:
            return reason
        if reason != told:
            say(reason + ' Trying again for up to '
                f'{max(0, int(deadline - time.monotonic()))} seconds.')
            told = reason
        time.sleep(PAUSE_SECONDS)


def _give_up(signum, frame):
    say('NOT STARTED. The start-up check did not finish in time: the '
        'database stopped answering part way through.')
    os._exit(1)


def _make_ready():
    """The steps the web server takes on its first request, done here so
    a problem stops the deploy instead of the live site. A table lock held
    by something else, or a blip, is waited out for a while."""
    from . import core
    deadline = time.monotonic() + min(WAIT_SECONDS, 60)
    while True:
        try:
            db.pool()                   # puts the tables in place
            core.secret('session_key')
            owners = core.ensure_owners()
            people = db.one(
                'SELECT count(*) AS n FROM users u WHERE u.is_owner OR '
                'EXISTS (SELECT 1 FROM memberships m WHERE m.user_id = '
                'u.id)')['n']
            ever = db.one('SELECT 1 AS x FROM users WHERE last_login_at '
                          'IS NOT NULL LIMIT 1')
            stores = db.one('SELECT count(*) AS n FROM stores')['n']
            return owners, people, ever, stores
        except db.Unavailable:
            if time.monotonic() + PAUSE_SECONDS >= deadline:
                raise
            db.close()
            time.sleep(PAUSE_SECONDS)


def main():
    u = db.url()
    if not u:
        say('Accounts are off (DATABASE_URL is not set). The site runs '
            'open: no sign-in, nothing saved.')
        return 0
    timed = hasattr(signal, 'SIGALRM')
    if timed:
        # nothing here may wait forever, whatever the database does
        was = signal.signal(signal.SIGALRM, _give_up)
        signal.alarm(WAIT_SECONDS + 90)
    try:
        return _check(u)
    finally:
        if timed:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, was)


def _check(u):
    try:
        db.check_url(u)
    except db.Unavailable as e:
        return stop(str(e))
    from psycopg.conninfo import conninfo_to_dict
    info = conninfo_to_dict(u)
    host = str(info.get('host') or 'this machine')
    say(f'Accounts are on. Database server: {host}; database: '
        f'{info.get("dbname") or "(default)"}.')
    if host.endswith('.render.com'):
        say('Note: that is the database\'s external address. It works, '
            'but the "Internal Database URL" is faster.')

    reason = _reach(u)
    if reason:
        return stop(reason, ['Fix DATABASE_URL, then deploy again.',
                             'Or remove DATABASE_URL to keep the site '
                             'running open.'])

    from . import mailer
    try:
        owners, people, ever, stores = _make_ready()
    except Exception as e:      # reached, but could not be made ready
        cause = e.__cause__ if isinstance(e, db.Unavailable) and \
            e.__cause__ is not None else e
        return stop('The database answered, but it could not be made '
                    f'ready ({db.brief(cause)}).')
    finally:
        db.close()
    say(f'Database reached. Tables are in place. Stores: {stores}. People '
        f'who can sign in: {people}.')

    # Can anyone actually sign in with these settings?
    missing = []
    if not people:
        missing.append('OWNER_EMAIL is not set to a valid email address, '
                       'so there is nobody who could sign in.')
    elif not owners:
        say('Note: OWNER_EMAIL is not set, so there is no owner account '
            '(nobody can add stores).')
    how = mailer.backend()
    if how is None:
        missing.append('RESEND_API_KEY is not set, so sign-in links cannot '
                       'be emailed.')
    elif how != 'resend' and os.environ.get('RENDER'):
        # the development settings, on the live host
        missing.append(f'MAIL_BACKEND is set to {how}, so sign-in links '
                       f'would not be emailed. Remove MAIL_BACKEND.')
    base = mailer.site_url()
    wanted = os.environ.get('APP_BASE_URL', '').strip()
    if wanted and wanted.rstrip('/') != base:
        missing.append('APP_BASE_URL has to be the site\'s address and '
                       'nothing else, such as '
                       'https://partsmanagersolutions.com.')
    elif not base and how not in ('log', 'memory'):
        missing.append('APP_BASE_URL is not set, so sign-in links cannot '
                       'be built (set it to the site\'s address, such as '
                       'https://partsmanagersolutions.com).')
    if missing and not ever:
        # First-time set-up is not finished. Starting now would swap a
        # working open site for a sign-in page nobody can get past.
        return stop('Sign-in cannot work yet, and nobody has signed in on '
                    'this database before, so accounts were not turned on:',
                    missing + ['Fix the settings above, then deploy again.'])
    for line in missing:
        say('WARNING: ' + line)
    if how is not None:
        say('Email: ' + ('sent through Resend' if how == 'resend' else
                         f'NOT sent (MAIL_BACKEND={how}: for development '
                         f'only)') + f', from {mailer.sender()}.')
    if base:
        say(f'Sign-in links point to {base}.')
    say('Ready.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
