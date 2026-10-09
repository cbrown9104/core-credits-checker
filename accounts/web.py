"""Sign-in, sessions and the account pages (History, Users, Stores).

How sign-in works: a person types their work email, gets a link that
works once and expires in 20 minutes, and lands signed in for 30 days on
that browser. There are no passwords. Only invited emails get a link.

Everything here is inert until DATABASE_URL is set (see accounts/db.py).
"""
import datetime
import io
import os
import secrets
import threading
import traceback
from urllib.parse import urlsplit

from flask import (Blueprint, abort, flash, g, jsonify, make_response,
                   redirect, render_template, request, send_file, session,
                   url_for)
from flask.sessions import SecureCookieSessionInterface
from markupsafe import Markup

from . import core, db, mailer, storage

bp = Blueprint('accounts', __name__)

INVITE_MINUTES = 72 * 60
# Largest request body accepted from a signed-out visitor (a sign-in form
# is a few hundred bytes).
SIGNED_OUT_MAX_BODY = 64 * 1024

# Pages a signed-out visitor may open.
PUBLIC = {'index', 'static', 'favicon', 'accounts.login',
          'accounts.login_link', 'accounts.logout', 'accounts.healthz'}
# Pages that are about the person, not about one store. They work with no
# store picked yet, and posts to them are not tied to a store.
STORE_FREE = PUBLIC | {'accounts.owner', 'accounts.owner_create_store',
                       'accounts.owner_open', 'accounts.switch_store',
                       'accounts.choose_store'}

_booted = None
_boot_lock = threading.Lock()

UNAVAILABLE = ('The site cannot reach its saved records right now. Nothing '
               'was lost. Wait a minute, then try again.')


def enabled():
    return db.enabled()


def base_url():
    """Where emailed links point. Taken from configuration, never from the
    request's Host header, so a forged header cannot redirect a link.
    Only local development (links printed to the console, or tests) may
    fall back to the address in the request."""
    for key in ('APP_BASE_URL', 'RENDER_EXTERNAL_URL'):
        v = os.environ.get(key, '').strip().rstrip('/')
        if v:
            return v
    if mailer.backend() in ('log', 'memory'):
        return request.host_url.rstrip('/')
    raise mailer.MailError('APP_BASE_URL is not set, so sign-in links '
                           'cannot be sent.')


def client_ip():
    """Best guess at the visitor's address, for rate limiting only."""
    ip = request.headers.get('CF-Connecting-IP', '').strip()
    if not ip:
        fwd = request.headers.get('X-Forwarded-For', '')
        ip = fwd.split(',')[0].strip() if fwd else (request.remote_addr or '')
    return ip[:64]


def csrf_token():
    tok = session.get('csrf')
    if not tok:
        tok = secrets.token_urlsafe(24)
        session['csrf'] = tok
    return tok


def form_guard():
    """Hidden fields every form carries: the page token, and the store the
    page was showing (so a click on a stale tab cannot act on whichever
    store was opened since in another tab)."""
    out = Markup('<input type="hidden" name="csrf_token" value="%s">') % \
        csrf_token()
    store = getattr(g, 'store', None)
    if store:
        out += Markup('<input type="hidden" name="store_check" '
                      'value="%s">') % store['id']
    return out


def _csrf_ok():
    sent = request.headers.get('X-CSRF-Token') or \
        request.form.get('csrf_token') or ''
    have = session.get('csrf') or ''
    if not sent or not have:
        return False
    return secrets.compare_digest(sent.encode('utf-8', 'replace'),
                                  have.encode('utf-8'))


def _wants_json():
    return request.path.startswith('/api/')


def _refuse(code, title, message, link=None, link_label=None):
    if _wants_json():
        return jsonify({'error': message}), code
    return render_template('account/message.html', title=title,
                           lines=[message], link=link,
                           link_label=link_label), code


# Where a sign-in may send someone afterwards: the app's own pages only.
# Never a sign-in link, sign-out or an action (a crafted ?next= must not be
# able to walk a person into someone else's link).
NEXT_PAGES = ('/reconcile', '/history', '/users', '/owner', '/stores')


def _safe_next(target):
    """Only ever redirect to one of this site's own pages."""
    if not target or len(target) > 300:
        return ''
    parts = urlsplit(target)
    if parts.scheme or parts.netloc or not target.startswith('/') or \
            target.startswith('//') or '\\' in target or \
            any(ord(ch) < 32 for ch in target):
        return ''
    if target == '/' or any(parts.path == p for p in NEXT_PAGES):
        return target
    return ''


def actor():
    """The signed-in person as a small dict for audit columns."""
    if not getattr(g, 'user', None):
        return None
    return {'id': g.user['id'], 'email': g.user['email']}


def is_admin():
    user = getattr(g, 'user', None)
    return bool(user) and (getattr(g, 'role', None) == 'admin' or
                           user['is_owner'])


def _boot(app):
    """Once per database: load the cookie-signing key and the owner list."""
    global _booted
    key = (db.url(), db.generation, os.environ.get('OWNER_EMAIL', ''))
    if _booted == key and app.secret_key:
        return
    with _boot_lock:        # the first requests arrive together
        if _booted == key and app.secret_key:
            return
        app.secret_key = core.secret('session_key')
        core.ensure_owners()
        # Runs live in this server's memory while they work. Anything
        # still marked "running" at start-up was cut off by a restart.
        storage.close_interrupted_runs()
        _booted = key


def _db_errors():
    """Errors that mean "the database is in trouble", not "the request
    was wrong": it cannot be reached, or it dropped what it was doing."""
    try:
        import psycopg
    except ImportError:         # accounts cannot be on without the driver
        return (db.Unavailable,)
    return (db.Unavailable, psycopg.OperationalError)


def _unavailable(e=None):
    """The answer while the database cannot be reached: a plain page (or
    JSON for the app's own calls), never a stack trace. It touches neither
    the session nor the database."""
    if e is not None and not isinstance(e, db.Unavailable):
        # reached, but it failed mid-request: worth the full detail
        traceback.print_exception(type(e), e, e.__traceback__)
    if _wants_json():
        resp = jsonify({'error': UNAVAILABLE})
    else:
        resp = make_response(render_template(
            'account/unavailable.html', message=UNAVAILABLE,
            again=request.path if request.method in ('GET', 'HEAD')
            else '/'))
    resp.status_code = 503
    resp.headers['Retry-After'] = '30'
    resp.headers['Cache-Control'] = 'no-store'
    return resp


class _Sessions(SecureCookieSessionInterface):
    """Flask opens the session before any request hook runs, so the
    cookie-signing key has to be in place by then: load it here."""

    def open_session(self, app, request):
        if enabled():
            try:
                _boot(app)
            except _db_errors() as e:
                # No database, so no way to tell who this is. _before
                # answers with the "back in a minute" page.
                request.environ['pms.db_down'] = e
                if not app.secret_key:
                    return None         # Flask supplies an empty session
        return super().open_session(app, request)


def _pick_store(user, stores):
    """(store, role) for this request, or (None, None) if the person has
    to choose. Never guesses between several stores: a store someone else
    just added them to must not become where their work lands."""
    def mine(store_id):
        return next((s for s in stores if s['id'] == store_id), None)

    for candidate in (session.get('sid'), user.get('last_store_id')):
        if not candidate:
            continue
        m = mine(candidate)
        if m:
            return m, m['role']
        if user['is_owner']:
            o = core.store_by_id(candidate)
            if o:
                return o, 'admin'
    if len(stores) == 1:
        return stores[0], stores[0]['role']
    return None, None


def _before():
    g.user = None
    g.store = None
    g.role = None
    g.stores = []
    if not enabled():
        return None
    ep = request.endpoint
    # Neither needs to know who is asking. The health check in particular
    # must answer while the database is away: the server itself is fine,
    # and restarting it would not bring the database back.
    if ep in ('static', 'accounts.healthz'):
        return None
    if 'pms.db_down' in request.environ:
        return _unavailable(request.environ['pms.db_down'])

    sx = session.get('sx')
    if sx:
        # Looked up on every request: signing out, or being removed from
        # the last store, ends the session on the server at once.
        user, refreshed = core.session_user(sx)
        if user and core.can_sign_in(user):
            g.user = user
            if refreshed:
                session.modified = True     # re-issue the 30-day cookie
        else:
            session.clear()

    public = ep is None or ep in PUBLIC
    changing = request.method not in ('GET', 'HEAD', 'OPTIONS')

    # Signed out: turned away before the request body is even read.
    if not g.user and not public:
        if _wants_json():
            return jsonify({'error': 'You are signed out. Sign in again, '
                                     'then retry.'}), 401
        nxt = request.full_path.rstrip('?') if request.method == 'GET' \
            else ''
        return redirect(url_for('accounts.login', next=_safe_next(nxt)
                                or None))
    if not g.user and changing:
        # A sign-in form is tiny. A streamed upload (no stated size) or
        # anything over the limit is refused unread.
        size = request.content_length
        # (gunicorn sets wsgi.input_terminated on every request, so the
        # sign of a streamed body is that flag WITHOUT a Content-Length)
        streamed = 'chunked' in request.headers.get(
            'Transfer-Encoding', '').lower() or (
            request.environ.get('wsgi.input_terminated') and
            not request.environ.get('CONTENT_LENGTH'))
        if streamed:
            return _refuse(411, 'Not accepted', 'That request cannot be '
                           'accepted.')
        if (size or 0) > SIGNED_OUT_MAX_BODY:
            return _refuse(413, 'Too large', 'That request is too large.')

    if changing and not _csrf_ok():
        return _refuse(400, 'Reload the page',
                       'This page was open too long. Reload it and try '
                       'again.', request.path, 'Reload')

    if g.user:
        g.stores = core.stores_for(g.user['id'])
        store, role = _pick_store(g.user, g.stores)
        if store is not None:
            if session.get('sid') != store['id']:
                session['sid'] = store['id']
            if g.user.get('last_store_id') != store['id']:
                # where this person works: the next sign-in lands here
                core.remember_store(g.user['id'], store['id'])
        elif 'sid' in session:
            session.pop('sid')
        g.store, g.role = store, role

    if public or ep in STORE_FREE:
        return None
    if not g.store:
        if g.stores:
            return redirect(url_for('accounts.choose_store'))
        if g.user['is_owner']:
            return redirect(url_for('accounts.owner'))
        return _refuse(403, 'No store yet', 'Your email is not on a store '
                       'right now. Ask your store admin to add you again.')
    if changing:
        # The page says which store it was showing. If another tab has
        # switched stores since, stop instead of acting on the wrong one.
        claimed = request.headers.get('X-Store-Id') or \
            request.form.get('store_check') or ''
        if claimed != str(g.store['id']):
            return _refuse(409, 'Reload the page',
                           'This page was showing a different store than '
                           'the one open now. Reload it, check the store '
                           'name at the top, and try again.',
                           request.path, 'Reload')
    return None


def _after(resp):
    if enabled():
        resp.headers.setdefault('Referrer-Policy', 'no-referrer')
        resp.headers.setdefault('X-Content-Type-Options', 'nosniff')
        resp.headers.setdefault('X-Frame-Options', 'DENY')
        if getattr(g, 'user', None) and \
                resp.mimetype in ('text/html', 'application/json'):
            resp.headers['Cache-Control'] = 'no-store'
    return resp


def _context():
    on = enabled()
    return {
        'accounts_on': on,
        'app_name': mailer.app_name(),
        'support_email': mailer.support_email() if on else '',
        'me': getattr(g, 'user', None) if on else None,
        'store': getattr(g, 'store', None) if on else None,
        'my_stores': getattr(g, 'stores', []) if on else [],
        'is_admin': is_admin() if on else False,
        'csrf_token': csrf_token if on else (lambda: ''),
        'form_guard': form_guard if on else (lambda: ''),
    }


def _central(dt):
    if not dt:
        return ''
    try:
        from zoneinfo import ZoneInfo
        local = dt.astimezone(ZoneInfo('America/Chicago'))
    except Exception:
        local = dt - datetime.timedelta(hours=5)
    hour = local.strftime('%I').lstrip('0') or '12'
    return (f'{local.strftime("%b")} {local.day}, {local.year} '
            f'{hour}:{local.strftime("%M %p")} CT')


def _money(v):
    try:
        return f'${float(v):,.2f}'
    except (TypeError, ValueError):
        return ''


def _size(v):
    try:
        n = float(v)
    except (TypeError, ValueError):
        return ''
    if n >= 1024 ** 3:
        return f'{n / 1024 ** 3:,.2f} GB'
    if n >= 1024 ** 2:
        return f'{n / 1024 ** 2:,.1f} MB'
    return f'{max(0, round(n / 1024)):,} KB'


def init_app(app):
    app.register_blueprint(bp)
    app.session_interface = _Sessions()
    secure = any(os.environ.get(k, '').strip().startswith('https://')
                 for k in ('APP_BASE_URL', 'RENDER_EXTERNAL_URL'))
    app.config.update(
        SESSION_COOKIE_NAME='pms_session',
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE='Lax',
        SESSION_COOKIE_SECURE=secure,
        PERMANENT_SESSION_LIFETIME=datetime.timedelta(
            days=core.SESSION_DAYS),
        # the cookie is only re-sent when something in it changed
        SESSION_REFRESH_EACH_REQUEST=False,
    )
    app.before_request(_before)
    app.after_request(_after)
    app.context_processor(_context)
    for kind in _db_errors():
        app.register_error_handler(kind, _unavailable)
    app.jinja_env.filters['central'] = _central
    app.jinja_env.filters['money'] = _money
    app.jinja_env.filters['size'] = _size


def _need_accounts():
    if not enabled():
        abort(404)


def _need_admin():
    if not is_admin():
        abort(403)


def _need_owner():
    if not (g.user and g.user['is_owner']):
        abort(404)


def _open_store(store_id):
    """Make a store the one this browser is working in."""
    session['sid'] = store_id
    core.remember_store(g.user['id'], store_id)


# ── sign in / out ───────────────────────────────────────────────────────
@bp.route('/healthz')
def healthz():
    return 'ok'


@bp.route('/login', methods=['GET', 'POST'])
def login():
    _need_accounts()
    if g.user:
        return redirect('/reconcile')
    if request.method != 'POST':
        return render_template('account/login.html', email='', error='',
                               next=_safe_next(request.args.get('next')))
    email = core.norm_email(request.form.get('email'))
    nxt = _safe_next(request.form.get('next'))

    def again(error, code):
        return render_template('account/login.html', email=email,
                               error=error, next=nxt), code

    if not core.valid_email(email):
        return again('Enter your work email address.', 400)
    try:
        if not mailer.configured():
            raise mailer.MailNotConfigured()
        base = base_url()
    except mailer.MailError:
        return again('Email is not set up yet, so sign-in links cannot be '
                     'sent. Contact support.', 503)
    if core.too_many_requests(email, client_ip()):
        return again('Too many sign-in requests. Wait 15 minutes, then '
                     'try again.', 429)
    if nxt:
        session['next'] = nxt
    else:
        session.pop('next', None)
    user = core.user_by_email(email)
    if user and core.can_sign_in(user):
        token = core.new_login_token(user['id'])
        # Sent off this request, so the answer takes the same time and
        # reads the same whether or not the email is registered.
        mailer.in_background(mailer.send_sign_in, user['email'],
                             f'{base}/login/link/{token}',
                             core.TOKEN_MINUTES)
    return render_template('account/login_sent.html', email=email,
                           minutes=core.TOKEN_MINUTES)


def _link_dead():
    return render_template(
        'account/message.html', title='This sign-in link no longer works',
        lines=['A link works once and expires after a short time.',
               'Ask for a new one with your email address.'],
        link=url_for('accounts.login'), link_label='Get a new link'), 410


@bp.route('/login/link/<token>', methods=['GET', 'POST'])
def login_link(token):
    _need_accounts()
    if request.method != 'POST':
        # Looking at the link (GET or HEAD) does NOT use it up: email
        # security scanners open every link in a message, and that must
        # not burn it or sign anyone in. Only the Continue button does.
        user = core.peek_login_token(token)
        if not user:
            return _link_dead()
        other = g.user['email'] if g.user and \
            g.user['id'] != user['id'] else ''
        return render_template('account/login_confirm.html',
                               email=user['email'], token=token,
                               signed_in_as=other)
    user, store_id = core.use_login_token(token)
    if not user or not core.can_sign_in(user):
        return _link_dead()
    nxt = _safe_next(session.get('next'))
    core.end_session(session.get('sx'))
    session.clear()
    session.permanent = True
    session['sx'] = core.new_session(user['id'])
    # A plain sign-in goes back to the store the person worked in last
    # (see _pick_store). An invitation opens its store only when that is
    # the person's one store; someone already on another store is shown
    # the list (with who added them) and picks for themselves.
    mine = core.stores_for(user['id'])
    if store_id and [s['id'] for s in mine] == [store_id]:
        session['sid'] = store_id
        core.remember_store(user['id'], store_id)
    elif store_id and len(mine) > 1:
        return redirect(url_for('accounts.choose_store'))
    return redirect(nxt or '/reconcile')


@bp.route('/logout', methods=['POST'])
def logout():
    _need_accounts()
    core.end_session(session.get('sx'))
    session.clear()
    return redirect('/')


@bp.route('/stores')
def choose_store():
    """Shown when a person is on several stores and has not picked one."""
    _need_accounts()
    return render_template('account/choose_store.html',
                           next=_safe_next(request.args.get('next')))


@bp.route('/switch-store', methods=['POST'])
def switch_store():
    _need_accounts()
    try:
        sid = int(request.form.get('store_id', ''))
    except ValueError:
        abort(400)
    if core.role_in(g.user['id'], sid) or \
            (g.user['is_owner'] and core.store_by_id(sid)):
        _open_store(sid)
    return redirect(_safe_next(request.form.get('next')) or '/reconcile')


# ── history and the saved master ────────────────────────────────────────
@bp.route('/history')
def history():
    _need_accounts()
    sid = g.store['id']
    return render_template(
        'account/history.html', active='history',
        runs=storage.list_runs(sid, 100),
        master=storage.current_master(sid),
        versions=storage.master_versions(sid, 15))


def _send_master(row, content):
    return send_file(
        io.BytesIO(content), as_attachment=True,
        download_name=row['filename'],
        mimetype='application/vnd.openxmlformats-officedocument.'
                 'spreadsheetml.sheet')


@bp.route('/master/download')
def master_download():
    _need_accounts()
    row, content = storage.master_file(g.store['id'])
    if not row:
        abort(404)
    return _send_master(row, content)


@bp.route('/master/<int:master_id>/download')
def master_version_download(master_id):
    _need_accounts()
    row, content = storage.master_file(g.store['id'], master_id)
    if not row:
        abort(404)
    return _send_master(row, content)


@bp.route('/master/<int:master_id>/restore', methods=['POST'])
def master_restore(master_id):
    _need_accounts()
    _need_admin()
    new_id = storage.restore_master(g.store['id'], master_id, actor())
    if not new_id:
        abort(404)
    flash('That version is the current master now. The one it replaced is '
          'still in the list below.')
    return redirect(url_for('accounts.history'))


# ── users (store admin) ─────────────────────────────────────────────────
def _invite(user, store):
    token = core.new_login_token(user['id'], minutes=INVITE_MINUTES,
                                 store_id=store['id'])
    link = f'{base_url()}/login/link/{token}'
    mailer.send_invite(user['email'], link, store['name'],
                       g.user['email'], INVITE_MINUTES)


@bp.route('/users')
def users():
    _need_accounts()
    _need_admin()
    return render_template('account/users.html', active='users',
                           members=core.members(g.store['id']),
                           mail_ready=mailer.configured())


@bp.route('/users/add', methods=['POST'])
def users_add():
    _need_accounts()
    _need_admin()
    role = request.form.get('role', 'user')
    try:
        user, created = core.add_member(
            g.store['id'], request.form.get('email'), role, actor())
    except core.AccountError as e:
        flash(str(e), 'error')
        return redirect(url_for('accounts.users'))
    if created:
        try:
            _invite(user, g.store)
            flash(f'{user["email"]} was added and sent a sign-in link.')
        except mailer.MailError as e:
            flash(f'{user["email"]} was added, but the invitation email '
                  f'did not go out. {e} They can still ask for a link on '
                  f'the sign-in page.', 'error')
    else:
        flash(f'{user["email"]} is now '
              f'{"an admin" if role == "admin" else "a user"}.')
    return redirect(url_for('accounts.users'))


@bp.route('/users/<int:user_id>/invite', methods=['POST'])
def users_invite(user_id):
    _need_accounts()
    _need_admin()
    if not core.role_in(user_id, g.store['id']):
        abort(404)
    user = core.user_by_id(user_id)
    try:
        _invite(user, g.store)
        flash(f'A new sign-in link was sent to {user["email"]}.')
    except mailer.MailError as e:
        flash(str(e), 'error')
    return redirect(url_for('accounts.users'))


@bp.route('/users/<int:user_id>/remove', methods=['POST'])
def users_remove(user_id):
    _need_accounts()
    _need_admin()
    try:
        email = core.remove_member(g.store['id'], user_id, actor())
        flash(f'{email} was removed from this store, effective now.')
    except core.AccountError as e:
        flash(str(e), 'error')
    return redirect(url_for('accounts.users'))


@bp.route('/store/settings', methods=['POST'])
def store_settings():
    _need_accounts()
    _need_admin()
    # The store's name and dealer code are how people tell stores apart
    # when they pick one, so only the owner of the service changes them.
    name, dealer = g.store['name'], g.store['dealer_code']
    if g.user['is_owner']:
        name = request.form.get('name')
        dealer = request.form.get('dealer_code')
    try:
        core.update_store(g.store['id'], name, dealer,
                          request.form.get('threshold_days'), actor())
        flash('Store details saved.')
    except core.AccountError as e:
        flash(str(e), 'error')
    return redirect(url_for('accounts.users'))


# ── owner (runs the service) ────────────────────────────────────────────
@bp.route('/owner')
def owner():
    _need_accounts()
    _need_owner()
    return render_template('account/owner.html', active='owner',
                           stores=core.all_stores(),
                           database_bytes=core.database_bytes(),
                           mail_ready=mailer.configured(),
                           mail_problem=mailer.last_problem)


@bp.route('/owner/stores', methods=['POST'])
def owner_create_store():
    _need_accounts()
    _need_owner()
    try:
        store, user = core.create_store(
            request.form.get('name'), request.form.get('dealer_code'),
            request.form.get('admin_email'), actor())
    except core.AccountError as e:
        flash(str(e), 'error')
        return redirect(url_for('accounts.owner'))
    if request.form.get('send_invite') and user['id'] != g.user['id']:
        try:
            _invite(user, store)
            flash(f'{store["name"]} was created and {user["email"]} was '
                  f'sent a sign-in link.')
        except mailer.MailError as e:
            flash(f'{store["name"]} was created, but the invitation email '
                  f'did not go out. {e}', 'error')
    else:
        flash(f'{store["name"]} was created with {user["email"]} as its '
              f'admin. No email was sent.')
    return redirect(url_for('accounts.owner'))


@bp.route('/owner/open/<int:store_id>', methods=['POST'])
def owner_open(store_id):
    _need_accounts()
    _need_owner()
    if not core.store_by_id(store_id):
        abort(404)
    _open_store(store_id)
    return redirect(url_for('accounts.history'))
