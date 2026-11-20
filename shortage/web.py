"""Pages and calls of Shortage Check. Sign-in, the page token and the
store check are done for every request by accounts.web before any of
these run; each query below is tied to the store that is open."""
import datetime
import io
import os
import re
import tempfile

from flask import (Blueprint, abort, flash, g, jsonify, redirect,
                   render_template, request, send_file, url_for)

import accounts
from accounts import web as acct_web

from . import export, invoice, receipts, store
from .match import HOW_TEXT

bp = Blueprint('shortage', __name__)

MAX_FILES = 40
FILE_MAX = 40 * 1024 * 1024
CALENDAR_WEEKS = 5
XLSX = ('application/vnd.openxmlformats-officedocument.spreadsheetml.'
        'sheet')


def _store_id():
    if not accounts.enabled():
        abort(404)
    return g.store['id']


def _today():
    try:
        from zoneinfo import ZoneInfo
        return datetime.datetime.now(ZoneInfo('America/Chicago')).date()
    except Exception:
        return (datetime.datetime.utcnow() -
                datetime.timedelta(hours=5)).date()


def _calendar(store_id, today):
    """The last few weeks, Monday first, with the DMS rows on file for
    each day."""
    start = today - datetime.timedelta(
        days=today.weekday() + 7 * (CALENDAR_WEEKS - 1))
    have = store.days(store_id, start, today)
    weeks = []
    for w in range(CALENDAR_WEEKS):
        week = []
        for d in range(7):
            day = start + datetime.timedelta(days=7 * w + d)
            rows, got = have.get(day, (0, 0))
            week.append({'day': day, 'rows': rows, 'receipts': got,
                         'future': day > today, 'weekend': d >= 5})
        weeks.append(week)
    return weeks


@bp.route('/shortages')
def home():
    sid = _store_id()
    today = _today()
    settings = store.get_settings(sid)
    return render_template(
        'shortage/home.html', active='shortage', today=today,
        facts=store.facts(sid, today), invoices=store.invoice_list(sid),
        uploads=store.recent_uploads(sid), weeks=_calendar(sid, today),
        coverage=store.coverage(sid), settings=settings,
        pdc_text='\n'.join(f'{k} = {v}' for k, v in sorted(
            settings['pdc_days'].items())),
        ignored_text=', '.join(settings['ignored_parts']))


def _load_invoice(sid, name, content, actor):
    fd, path = tempfile.mkstemp(suffix='.pdf')
    try:
        with os.fdopen(fd, 'wb') as fh:
            fh.write(content)
        try:
            found = invoice.read_invoice_pdf(path)
        except invoice.NotAnInvoice as e:
            return [('invoice', 'refused', str(e))]
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    out = []
    for inv in found:
        if not inv['lines']:
            out.append(('invoice', 'refused',
                        f'Invoice {inv["number"]}: no part lines could be '
                        f'read from this PDF.'))
            continue
        result, message = store.save_invoice(sid, inv, name, actor)
        out.append(('invoice', result,
                    f'Invoice {inv["number"]}: {message}'))
    return out


def _load_receipts(sid, name, content):
    try:
        parsed = receipts.read_receipts(content, name)
    except receipts.NotReceipts as e:
        return [('', 'refused', str(e))]
    new = store.save_receipts(sid, parsed)
    span = (f'The file covers {parsed["first"]:%m/%d/%Y} to '
            f'{parsed["last"]:%m/%d/%Y}: {parsed["rows"]:,} rows, '
            f'{len(parsed["receipts"]):,} of them receipts.')
    if not new:
        return [('receipts', 'already',
                 'Every receipt in this file was already loaded. ' + span)]
    return [('receipts', 'loaded', f'{new:,} new receipts. ' + span)]


@bp.route('/api/shortages/upload', methods=['POST'])
def upload():
    """Invoice PDFs and the DMS file, any mix. Each file is answered with
    what became of it and why."""
    sid = _store_id()
    files = [f for f in request.files.getlist('files') if f and f.filename]
    if not files:
        return jsonify({'error': 'Choose the invoice PDFs and the DMS '
                                 'file first.'}), 400
    if len(files) > MAX_FILES:
        return jsonify({'error': f'Send {MAX_FILES} files or fewer at a '
                                 f'time.'}), 400
    actor = acct_web.actor()
    answers, changed = [], False
    for f in files:
        name = os.path.basename(f.filename.replace('\\', '/'))[:120]
        content = f.read(FILE_MAX + 1)
        if len(content) > FILE_MAX:
            got = [('', 'refused', 'This file is larger than 40 MB.')]
        elif content[:5] == b'%PDF-' or name.lower().endswith('.pdf'):
            got = _load_invoice(sid, name, content, actor)
        else:
            got = _load_receipts(sid, name, content)
        for kind, result, message in got:
            store.log_upload(sid, name, kind, result, message, actor)
            changed = changed or result in ('loaded', 'cut_off')
            answers.append({'name': name, 'kind': kind, 'result': result,
                            'message': message})
    if changed:
        store.rematch(sid)
    return jsonify({'files': answers})


def _json_rows(lines):
    """Lines in the short form the page script reads."""
    out = []
    for l in lines:
        short = max(l['qty'] - l['received'], 0) if l['state'] in (
            'short', 'partial') else 0
        out.append({
            'id': l['id'], 'inv': l['number'],
            'd': l['deadline'].isoformat() if l['deadline'] else '',
            'od': l['order_date'].isoformat() if l['order_date'] else '',
            'st': l['state'], 'part': l['part'], 'desc': l['description'],
            'ord': l['order_no'], 'ot': l['order_type'],
            'shp': l['shipper'], 'pdc': l['pdc'], 'q': l['qty'],
            'r': l['received'], 'u': float(l['unit_price']),
            'v': float(short * l['unit_price']), 'net': float(l['net']),
            'how': HOW_TEXT.get(l['how'], ''), 'hint': l['hint'],
            'det': l['detail'], 's': l['status'], 'c': l['claim_no'],
            'n': l['note']})
    return out


@bp.route('/shortages/invoice/<int:invoice_id>')
def invoice_page(invoice_id):
    sid = _store_id()
    inv = store.get_invoice(sid, invoice_id)
    if not inv:
        abort(404)
    return render_template(
        'shortage/lines.html', active='shortage', invoice=inv,
        today=_today(), rows=_json_rows(store.lines_for(sid, invoice_id)),
        export_url=url_for('shortage.invoice_export',
                           invoice_id=invoice_id))


@bp.route('/shortages/open')
def open_page():
    sid = _store_id()
    return render_template(
        'shortage/lines.html', active='shortage', invoice=None,
        today=_today(), rows=_json_rows(store.lines_for(sid)),
        export_url=url_for('shortage.open_export'))


def _xlsx(lines, name):
    return send_file(io.BytesIO(export.workbook(lines)), mimetype=XLSX,
                     as_attachment=True, download_name=name)


@bp.route('/shortages/invoice/<int:invoice_id>/export')
def invoice_export(invoice_id):
    sid = _store_id()
    inv = store.get_invoice(sid, invoice_id)
    if not inv:
        abort(404)
    safe = re.sub(r'[^A-Za-z0-9]+', '_', inv['number']).strip('_')
    return _xlsx(store.lines_for(sid, invoice_id), f'Shortages_{safe}.xlsx')


@bp.route('/shortages/open/export')
def open_export():
    sid = _store_id()
    return _xlsx(store.lines_for(sid),
                 f'Open_shortages_{_today():%Y-%m-%d}.xlsx')


@bp.route('/api/shortages/lines', methods=['POST'])
def lines_update():
    """Set status, claim number or note on one line or on many."""
    sid = _store_id()
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({'error': 'Reload the page and try again.'}), 400
    changes = {k: data[k] for k in ('status', 'claim_no', 'note')
               if isinstance(data.get(k), str)}
    try:
        n = store.update_lines(sid, data.get('ids'), changes,
                               acct_web.actor())
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    return jsonify({'updated': n})


@bp.route('/shortages/invoice/<int:invoice_id>/done', methods=['POST'])
def invoice_done(invoice_id):
    sid = _store_id()
    if not store.set_done(sid, invoice_id, request.form.get('done') == '1'):
        abort(404)
    return redirect(url_for('shortage.invoice_page', invoice_id=invoice_id))


@bp.route('/shortages/invoice/<int:invoice_id>/delete', methods=['POST'])
def invoice_delete(invoice_id):
    """Take a wrongly loaded invoice off the store. Admins only."""
    sid = _store_id()
    if not acct_web.is_admin():
        abort(403)
    number = store.delete_invoice(sid, invoice_id, acct_web.actor())
    if number is None:
        abort(404)
    store.rematch(sid)
    flash(f'Invoice {number} was removed. Its receipts are free to match '
          f'other invoices.')
    return redirect(url_for('shortage.home'))


def _pdc_days(text):
    """'03181 = 7' on each line or between commas -> {'03181': '7'}."""
    out = {}
    for piece in re.split(r'[\n,;]+', text or ''):
        if not piece.strip():
            continue
        m = re.match(r'^\s*(\S+?)\s*[=: ]\s*(\S+)\s*$', piece)
        if not m:
            raise ValueError(f'"{piece.strip()}" is not a PDC and a number '
                             f'of days. Write it like 03181 = 7.')
        out[m.group(1)] = m.group(2)
    return out


@bp.route('/shortages/settings', methods=['POST'])
def settings_save():
    sid = _store_id()
    if not acct_web.is_admin():
        abort(403)
    try:
        store.save_settings(
            sid, request.form.get('default_days', ''),
            _pdc_days(request.form.get('pdc_days', '')),
            re.split(r'[\s,;]+', request.form.get('ignored_parts', '')),
            acct_web.actor())
    except ValueError as e:
        flash(str(e), 'error')
        return redirect(url_for('shortage.home') + '#settings')
    store.rematch(sid)
    flash('Settings saved. Deadlines and ignored parts were worked out '
          'again for every invoice.')
    return redirect(url_for('shortage.home') + '#settings')
