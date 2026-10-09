"""Send the sign-in and invitation emails.

Backends, picked by environment:
- RESEND_API_KEY set  -> sent through Resend's HTTPS API.
- MAIL_BACKEND=memory -> kept in `outbox` (tests).
- MAIL_BACKEND=log    -> printed to the server log (local development
                         only: a sign-in link in a log is a key to an
                         account).
- none of those       -> MailNotConfigured, and the page says so.

MAIL_FROM is the sender, e.g. "Parts Manager Solutions
<signin@partsmanagersolutions.com>". Its domain has to be verified with the
email provider.
"""
import datetime
import json
import os
import re
import threading
import traceback
import urllib.error
import urllib.request

RESEND_URL = 'https://api.resend.com/emails'
EMAIL_SHAPE = re.compile(r'^[^@\s<>"\',;]+@[^@\s<>"\',;]+\.[A-Za-z]{2,}$')
outbox = []
# The most recent failure to send, for the owner's Stores page:
# {'at': datetime, 'what': str}. Sign-in emails are sent in the background,
# so this is where a failure becomes visible.
last_problem = {}


class MailError(Exception):
    pass


class MailNotConfigured(MailError):
    pass


def app_name():
    return os.environ.get('APP_NAME', '').strip() or 'Parts Manager Solutions'


def support_email():
    """Where people are told to write for help (SUPPORT_EMAIL), or ''."""
    v = os.environ.get('SUPPORT_EMAIL', '').strip()
    return v if EMAIL_SHAPE.match(v) else ''


SITE_RE = re.compile(r'^https?://[A-Za-z0-9.\-]+(:\d{1,5})?$')


def site_url():
    """The site's public address, where emailed links point: APP_BASE_URL,
    else the address Render gives the service. '' when neither is a plain
    address (https://name, nothing after it)."""
    for key in ('APP_BASE_URL', 'RENDER_EXTERNAL_URL'):
        v = os.environ.get(key, '').strip().rstrip('/')
        if v and SITE_RE.match(v):
            return v
    return ''


def sender():
    return (os.environ.get('MAIL_FROM', '').strip() or
            f'{app_name()} <signin@partsmanagersolutions.com>')


def backend():
    b = os.environ.get('MAIL_BACKEND', '').strip().lower()
    if b in ('memory', 'log'):
        return b
    if os.environ.get('RESEND_API_KEY', '').strip():
        return 'resend'
    return None


def configured():
    return backend() is not None


def _note_problem(what):
    last_problem.clear()
    last_problem.update(at=datetime.datetime.now(datetime.timezone.utc),
                        what=str(what)[:300])


def in_background(func, *args):
    """Run a send off the request thread; a failure is logged and kept in
    last_problem. (Tests collect mail in memory, so they run it inline.)"""
    def run():
        try:
            func(*args)
        except MailError as e:
            _note_problem(e)
        except Exception as e:       # never let a mail problem kill a thread
            traceback.print_exc()
            _note_problem(e.__class__.__name__)

    if backend() == 'memory':
        run()
    else:
        threading.Thread(target=run, daemon=True).start()


def send(to, subject, text, html=None):
    b = backend()
    if b is None:
        raise MailNotConfigured(
            'Email is not set up yet, so sign-in links cannot be sent.')
    if b == 'memory':
        outbox.append({'to': to, 'subject': subject, 'text': text,
                       'html': html})
        return
    if b == 'log':
        print(f'[mail to {to}] {subject}\n{text}', flush=True)
        return
    body = {'from': sender(), 'to': [to], 'subject': subject, 'text': text}
    if html:
        body['html'] = html
    reply_to = os.environ.get('MAIL_REPLY_TO', '').strip()
    if reply_to:
        body['reply_to'] = reply_to
    req = urllib.request.Request(
        RESEND_URL, data=json.dumps(body).encode('utf-8'), method='POST',
        headers={
            'Authorization': 'Bearer ' +
                             os.environ['RESEND_API_KEY'].strip(),
            'Content-Type': 'application/json',
            'User-Agent': 'core-credits-checker/1.0',
        })
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            resp.read()
    except urllib.error.HTTPError as e:
        detail = ''
        try:
            detail = json.loads(e.read().decode('utf-8', 'replace')).get(
                'message', '')
        except Exception:
            pass
        # never include the API key or the email body in the error
        print(f'[mail] provider answered HTTP {e.code}: {detail}',
              flush=True)
        _note_problem(f'The email provider refused a message (HTTP '
                      f'{e.code}). {detail}')
        raise MailError(f'The email provider refused the message '
                        f'(HTTP {e.code}).')
    except (urllib.error.URLError, OSError) as e:
        print(f'[mail] could not reach the provider: {e}', flush=True)
        _note_problem('Could not reach the email provider.')
        raise MailError('Could not reach the email provider. Try again in '
                        'a minute.')


def _wrap(title, lines, button_url, button_label, foot):
    """A plain, client-safe HTML email (tables-free, inline styles)."""
    from html import escape
    paras = ''.join(f'<p style="margin:0 0 14px;font-size:15px;'
                    f'line-height:1.5;color:#222;">{escape(x)}</p>'
                    for x in lines)
    return (
        '<div style="font-family:Segoe UI,Arial,sans-serif;max-width:520px;'
        'margin:0 auto;padding:24px;">'
        f'<h2 style="margin:0 0 16px;font-size:20px;color:#0f3460;">'
        f'{escape(title)}</h2>{paras}'
        f'<p style="margin:22px 0;"><a href="{escape(button_url)}" '
        'style="background:#5a8dee;color:#fff;text-decoration:none;'
        'padding:12px 22px;border-radius:6px;font-weight:600;'
        f'font-size:15px;display:inline-block;">{escape(button_label)}'
        '</a></p>'
        '<p style="margin:0 0 6px;font-size:12px;color:#666;">If the button '
        'does not work, copy this address into your browser:</p>'
        f'<p style="margin:0 0 18px;font-size:12px;color:#666;'
        f'word-break:break-all;">{escape(button_url)}</p>'
        f'<p style="margin:0;font-size:12px;color:#888;">{escape(foot)}'
        '</p></div>')


def send_sign_in(to, link, minutes):
    name = app_name()
    lines = [f'Use this link to sign in to {name}.',
             f'It works once and expires in {minutes} minutes.']
    foot = ('If you did not ask to sign in, ignore this email. Nobody can '
            'sign in without this link.')
    text = '\n\n'.join(lines + [link, foot])
    send(to, f'Your sign-in link for {name}', text,
         _wrap(f'Sign in to {name}', lines, link, 'Sign in', foot))


def send_invite(to, link, store_name, inviter, minutes):
    name = app_name()
    site = link.split('/login/link/')[0]
    lines = [f'{inviter} added you to {store_name} on Core Credits Checker '
             f'({name}).',
             'It reads your signed shipper scans and weekly credit memos '
             'and shows which core returns were never paid. For a first '
             'run, have this week\'s credit memo PDF and your signed '
             'shipper scans ready.',
             'There is no password. Use this link to sign in.',
             f'It works once and expires in {minutes // 60} hours. After '
             f'that, go to {site} and ask for a new link with this email '
             f'address.']
    foot = 'If you were not expecting this, ignore this email.'
    text = '\n\n'.join(lines + [link, foot])
    send(to, f'Sign in to Core Credits Checker for {store_name}', text,
         _wrap(f'{store_name} on Core Credits Checker', lines, link,
               'Sign in', foot))
