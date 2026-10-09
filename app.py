"""
Core Credits Checker – Flask web app
=====================================
Compares GCRS Dealer Shipper PDFs (scanned) against Mopar Weekly Global
Core Return Credit Memo PDFs (text-based) to find unclaimed core returns.

Memory-safe for a 512 MB–2 GB instance:
- One pdftoppm + one tesseract per chunk; page images deleted immediately
- Long edge capped at 1650 px (150 DPI letter) so a bad page box cannot balloon
- Page count via pdfinfo; scan check via pdftotext (no pdfplumber on the OCR path)
- Shipper scans over MAX_OCR_PAGES are split into chunks, OCR'd, then merged
- One shipper PDF per request
"""
from flask import (Flask, render_template, request, jsonify, send_file,
                   abort, g, redirect, url_for)
from werkzeug.utils import secure_filename
import datetime
import io
import subprocess
import re
import tempfile
import os
import gc
import shutil
import time
import uuid

import accounts
from accounts import db as acct_db
from accounts import storage as acct_storage
from accounts import web as acct_web
from recon import engine as recon_engine
from recon import jobs as recon_jobs
from recon.master import MasterError
from recon.shipper import _shipment_from_header, read_shipper_pdf

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024  # 100 MB max upload
# Store accounts (sign-in, saved master, run history). Inert unless
# DATABASE_URL is set; without it the app runs open, as it always has.
accounts.init_app(app)

# Cap OCR pages so a huge scan can't run until OOM/timeout (Starter-safe default)
MAX_OCR_PAGES = 20
OCR_DPI = 150  # was 200; lower RAM/time with small accuracy tradeoff
# Letter at 150 DPI is 1650 px on the long edge. Cap so a bad page box
# cannot rasterize into a multi-hundred-MB bitmap. Normal scans unchanged.
OCR_SCALE_TO = 1650
# Seconds to wait between tries when a finished run cannot be saved because
# the database is away (about half a minute in all).
SAVE_RETRY_WAITS = (5, 10, 15)

# ── regex ────────────────────────────────────────────────────────────────
CTRL_RE = re.compile(r'^([Cc©€6][A-Za-z0-9]\d{7,8})\b')
AMT_RE = re.compile(r'([\d,]+\.\d{2})\s*$')
WRAP_RE = re.compile(r'^\s*(\d+)\s+([\d,]+\.\d{2})\s*$')
CLAIM_RE = re.compile(r'^\d{5,6}$')
# The memo number is printed after a numeric prefix (03181000CC00203680).
# Any digits are accepted there, so the prefix does not have to match.
CM_HDR_RE = re.compile(r'CREDIT MEMO NUMBER:\s*\d*?(CC\d+)')
CM_REF_RE = re.compile(r'REFERENCE/CONTROL\s+NUMBER\s+(\S+.*)')
# Optional leading U, 7–9 digits, 1–2 letters. Letter I is never valid;
# normalize_part_number maps I → 1 before this check.
_PART_RE = re.compile(r'^U?\d{7,9}[A-Z]{1,2}$')
# Near-miss (when a token is already a part-number candidate): flag, don't drop.
_PART_NEAR_RE = re.compile(r'^U?\d{5,12}[A-Z]{1,3}$')

SKIP = frozenset([
    'checkoff', 'ticket number', 'part number', 'description',
    'quantity usd', 'fca/mopar', 'fca/', 'eca/mopar', 'gcrs dealer',
    'dealer:', 'covert chrysler', 'research blvd', 'austin, tx',
    'ship to:', 'core center', 'east shelby', 'memphis, tn',
    'check mark', 'picked up', 'crossed through', 'reflecting only',
    'pickup verif', 'driver', 'signature', 'date total', 'dds /',
    'gcrs shp#', 'part checkoff', 'omnisource', 'detroit ave',
    'toledo, oh', 'total parts',
])


def _is_scanned(path):
    """True when the PDF has no selectable text (image-only scan).

    Uses pdftotext on the first pages only so a scanned shipper is not
    also opened by pdfplumber (that second parse is pure RAM).
    """
    try:
        r = subprocess.run(
            ['pdftotext', '-f', '1', '-l', '3', '-layout', path, '-'],
            capture_output=True, text=True, timeout=30,
        )
        return len((r.stdout or '').strip()) <= 100
    except Exception:
        return True


def _page_count(path):
    """Page count via pdfinfo — avoids loading the PDF in Python."""
    try:
        r = subprocess.run(
            ['pdfinfo', path],
            capture_output=True, text=True, timeout=30,
        )
        m = re.search(r'Pages:\s+(\d+)', r.stdout or '')
        if m:
            return int(m.group(1))
    except Exception:
        pass
    return 1


def _ocr_pages(path):
    """OCR every page via one pdftoppm and one tesseract, files on disk.

    A single tesseract process loads the model once (per-page CLI startup
    was burning the 300s worker budget). Page images are removed before return.
    """
    n = _page_count(path)
    if n < 1:
        return ''
    td = tempfile.mkdtemp(prefix=f'ocr_{os.getpid()}_')
    try:
        prefix = os.path.join(td, 'p')
        subprocess.run(
            ['pdftoppm', '-gray', '-r', str(OCR_DPI),
             '-scale-to', str(OCR_SCALE_TO), path, prefix],
            capture_output=True, timeout=max(60, 15 * n),
        )
        images = []
        for name in os.listdir(td):
            if name.endswith('.pgm'):
                images.append(os.path.join(td, name))
        images.sort(key=_pgm_ord)
        if not images:
            return ''
        list_path = os.path.join(td, 'pages.txt')
        with open(list_path, 'w') as fh:
            for img in images:
                fh.write(img + '\n')
        env = os.environ.copy()
        env['OMP_THREAD_LIMIT'] = '1'
        r = subprocess.run(
            ['tesseract', list_path, 'stdout', '--psm', '6'],
            capture_output=True, text=True,
            timeout=max(90, 25 * n),
            env=env,
        )
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout
        return ''
    except Exception:
        return ''
    finally:
        shutil.rmtree(td, ignore_errors=True)
        gc.collect()


def _pgm_ord(path):
    m = re.search(r'-(\d+)\.pgm$', os.path.basename(path))
    return int(m.group(1)) if m else 0


def _ocr_text(path):
    """OCR a scanned PDF; auto-split into ≤MAX_OCR_PAGES chunks when needed."""
    n = _page_count(path)
    if n <= MAX_OCR_PAGES:
        return _ocr_pages(path)

    # One chunk at a time: write it, OCR it, delete it. Do not keep every
    # chunk PDF (or its page images) alive for the whole document.
    from pypdf import PdfReader, PdfWriter
    reader = PdfReader(path)
    total = len(reader.pages)
    parts = []
    try:
        for start in range(0, total, MAX_OCR_PAGES):
            end = min(start + MAX_OCR_PAGES, total)
            writer = PdfWriter()
            for i in range(start, end):
                writer.add_page(reader.pages[i])
            chunk_path = os.path.join(
                tempfile.gettempdir(),
                f'ocr_chunk_{os.getpid()}_{start}_{end}.pdf',
            )
            try:
                with open(chunk_path, 'wb') as fh:
                    writer.write(fh)
                del writer
                text = _ocr_pages(chunk_path)
                if text:
                    parts.append(text)
            finally:
                _rm(chunk_path)
                gc.collect()
    finally:
        del reader
        gc.collect()
    return '\n'.join(parts)


def _pdftotext(path):
    """Extract text via pdftotext -layout (poppler-utils)."""
    r = subprocess.run(
        ['pdftotext', '-layout', path, '-'],
        capture_output=True, text=True, timeout=120,
    )
    if r.returncode == 0 and r.stdout.strip():
        return r.stdout
    return None


def _pdfplumber_text(path):
    """Fallback: extract with pdfplumber. Imported only if pdftotext fails."""
    import pdfplumber
    parts = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            parts.append(page.extract_text() or '')
    return '\n'.join(parts)


def _norm(raw):
    s = raw.upper().replace('©', 'C').replace('€', 'C')
    # OCR often misreads leading C as 6 on shipper scans (e.g. 6455183028)
    if s.startswith('6') and len(s) >= 9:
        s = 'C' + s[1:]
    return s


def _norm_ticket(raw):
    """Same ticket cleanup for shipper controls and credit REFERENCE/CONTROL numbers."""
    return _norm((raw or '').strip())


def normalize_part_number(raw):
    """Normalize a core part number so shipper and credit memo forms compare equal.

    Order: uppercase, strip non-alphanumeric, letter I → digit 1 in the
    digit body (OCR misread), then validate. The two-letter suffix keeps
    its letters — AI is a real Mopar suffix. Shape is an optional leading U
    (kept — it is not noise), 7–9 digits, then 1–2 letters. Some numbers
    have no leading U. If the shape does not match, the cleaned value is
    kept and flagged rather than dropped.

    Returns (normalized, flagged).
    """
    s = re.sub(r'[^A-Z0-9]', '', (raw or '').upper())
    if not s:
        return '', False
    # I -> 1 in the digit body only; the two-letter suffix is kept as read
    # (Mopar does use suffixes such as AI: 68085908AI, U8090720AI).
    if len(s) > 2:
        s = s[:-2].replace('I', '1') + s[-2:]
    return s, _PART_RE.fullmatch(s) is None


def parts_match(a, b):
    """True when both part numbers normalize to the same valid value."""
    na, fa = normalize_part_number(a)
    nb, fb = normalize_part_number(b)
    if fa or fb or not na or not nb:
        return False
    return na == nb


def _fix_qty(s):
    s = s.replace('i', '1').replace('l', '1').replace('L', '1')
    s = s.replace('O', '0').replace('o', '0')
    try:
        int(s)
        return s
    except ValueError:
        return '1'


def parse_shipper(text):
    results = []
    lines = text.split('\n')
    i = 0
    shp = ''
    while i < len(lines):
        line = lines[i].strip()
        i += 1
        if not line:
            continue
        if 'SHP' in line.upper():
            found = _shipment_from_header(line.upper())
            if found:
                shp = found
        low = line.lower()
        if any(kw in low for kw in SKIP):
            continue
        if low.startswith('total'):
            continue
        ct = CTRL_RE.match(line)
        if not ct:
            continue
        control = _norm_ticket(ct.group(1))
        rest = line[ct.end():].strip()
        amt = AMT_RE.search(rest)
        if amt:
            amount = amt.group(1)
            before = rest[:amt.start()].strip()
            tokens = before.split()
            if not tokens:
                results.append(_row(control, '', '', '', '1', amount,
                                    shipper_date=shp))
                continue
            qty = _fix_qty(tokens.pop())
            claim = ''
            if tokens and CLAIM_RE.match(tokens[0]):
                claim = tokens.pop(0)
            part, part_flagged = normalize_part_number(
                tokens.pop(0) if tokens else '')
            desc = ' '.join(tokens)
            results.append(_row(
                control, claim, part, desc, qty, amount, part_flagged,
                shipper_date=shp))
        else:
            qty, amount = '1', '0.00'
            if i < len(lines):
                wm = WRAP_RE.match(lines[i].strip())
                if wm:
                    qty, amount = wm.group(1), wm.group(2)
                    i += 1
            tokens = rest.split()
            claim = ''
            if tokens and CLAIM_RE.match(tokens[0]):
                claim = tokens.pop(0)
            part, part_flagged = normalize_part_number(
                tokens.pop(0) if tokens else '')
            desc = ' '.join(tokens)
            results.append(_row(
                control, claim, part, desc, qty, amount, part_flagged,
                shipper_date=shp))
    return results


def _scan_rows(path):
    """Scanned shipper -> rows, using the same word-box OCR reader as the
    Full Reconciliation page (200 DPI; handles a C read as 0/6/©, glued
    qty/amount, stray check marks, and reads each page's GCRS SHP# date)."""
    rows = []
    seen = set()
    for page in read_shipper_pdf(path):
        for ln in page['lines']:
            if ln['ticket'] in seen:
                continue
            seen.add(ln['ticket'])
            amt = ln['amount']
            rows.append(_row(
                ln['ticket'], str(ln['claim'] or ''), ln['part'],
                ln['description'], str(ln['qty'] or 1),
                f'{amt:,.2f}' if amt is not None else '0.00',
                not ln['part_ok'], shipper_date=page['shipment_id'] or ''))
    return rows


def _row(ctrl, claim, part, desc, qty, amt, part_flagged=False,
         shipper_date=''):
    return dict(control=ctrl, claim=claim, part=part,
                description=desc, qty=qty, amount=amt,
                part_flagged=bool(part_flagged),
                shipper_date=shipper_date or '')


def parse_credits(text):
    credits = {}
    blocks = CM_HDR_RE.split(text)
    idx = 1
    while idx < len(blocks):
        memo = blocks[idx]
        body = blocks[idx + 1] if idx + 1 < len(blocks) else ''
        idx += 2
        for m in re.finditer(
                r'REFERENCE/CONTROL\s+NUMBER\s+(\S+)', body):
            ticket = _norm_ticket(m.group(1))
            if ticket not in credits:
                credits[ticket] = memo
    return credits


def parse_credits_fallback(text):
    tickets = set()
    for line in text.split('\n'):
        m = re.search(
            r'REFERENCE/CONTROL\s+NUMBER\s+(C[A-Z0-9]\d{7,8})', line, re.I)
        if m:
            tickets.add(_norm_ticket(m.group(1)))
            continue
        m = re.search(r'RM:(C[A-Z0-9]\d{7,8})', line, re.I)
        if m:
            tickets.add(_norm_ticket(m.group(1)))
    return tickets


def _consider_part_token(tok):
    """(normalized, flagged) if tok is a part number, else None.

    Credit-memo ids starting with 0318 are not part numbers.
    """
    norm, flagged = normalize_part_number(tok)
    if not norm or norm.startswith('0318'):
        return None
    if not flagged:
        return norm, False
    if _PART_NEAR_RE.fullmatch(norm):
        return norm, True
    return None


def credit_part_numbers(text):
    """Map control ticket → (normalized part, flagged) from credit-memo text.

    Uses the same normalize_part_number() as the shipper parser so a later
    comparison is on normalized forms. Checks the REFERENCE/CONTROL line and
    the next line. A valid part wins over a flagged near-miss.
    """
    lines = (text or '').split('\n')
    out = {}
    ref_re = re.compile(r'REFERENCE/CONTROL\s+NUMBER\s+(\S+)', re.I)
    rm_re = re.compile(r'RM:(C[A-Z0-9]\d{7,8})', re.I)
    for i, line in enumerate(lines):
        m = ref_re.search(line) or rm_re.search(line)
        if not m:
            continue
        ticket = _norm_ticket(m.group(1))
        if ticket in out:
            continue
        found = []
        for tok in line.split():
            hit = _consider_part_token(tok)
            if hit:
                found.append(hit)
        # Part number sometimes wraps onto the next line. Do not look ahead
        # once this line already has one, so we don't steal the next ticket's.
        if not found and i + 1 < len(lines):
            for tok in lines[i + 1].split():
                hit = _consider_part_token(tok)
                if hit:
                    found.append(hit)
        if not found:
            continue
        good = [h for h in found if not h[1]]
        out[ticket] = good[0] if good else found[0]
    return out


@app.route('/')
def index():
    if accounts.enabled() and not g.user:
        return render_template('account/landing.html')
    if accounts.enabled() and not g.store:
        if g.stores:        # on several stores, none picked yet
            return redirect(url_for('accounts.choose_store'))
        if g.user['is_owner']:
            return redirect(url_for('accounts.owner'))
        return render_template(
            'account/message.html', title='No store yet',
            lines=['Your email is not on a store right now.',
                   'Ask your store admin to add you again.']), 403
    return render_template('index.html', active='quick')


@app.route('/favicon.ico')
def favicon():
    return '', 204, {'Cache-Control': 'public, max-age=86400'}


def _account():
    """(store_id, actor) of the signed-in person, or (None, None) when
    accounts are off."""
    if not accounts.enabled():
        return None, None
    return g.store['id'], acct_web.actor()


@app.route('/api/check-cores', methods=['POST'])
def check_cores():
    try:
        shipper_files = [f for f in request.files.getlist('shipper_pdf')
                         if f.filename]
        credit_files = [f for f in request.files.getlist('credit_pdf')
                        if f.filename]

        if not shipper_files:
            return jsonify(
                {'error': 'Upload at least one GCRS shipper PDF.'}), 400
        if not credit_files:
            return jsonify(
                {'error': 'Upload at least one credit memo PDF.'}), 400

        # Free Render: one shipper per request keeps OCR under RAM limits
        if len(shipper_files) > 1:
            return jsonify({
                'error': 'On the free server, upload only 1 shipper PDF at a '
                         'time (plus your credit memos), then run again for '
                         'the next shipper.'
            }), 400

        with tempfile.TemporaryDirectory() as tmp:
            all_items = []
            ocr_used = False
            for sf in shipper_files:
                p = os.path.join(tmp, _safe(sf.filename))
                sf.save(p)
                if _is_scanned(p):
                    ocr_used = True
                    all_items.extend(_scan_rows(p))
                else:
                    text = _pdftotext(p) or _pdfplumber_text(p)
                    if text:
                        all_items.extend(parse_shipper(text))
                _rm(p)
                gc.collect()

            if not all_items:
                return jsonify({
                    'error': 'No core-return line items found in the shipper '
                             'PDF(s). Verify these are GCRS Dealer Shipper '
                             'Documents.'
                }), 400

            all_credited = set()
            credit_details = {}
            credit_part_map = {}
            for cf in credit_files:
                p = os.path.join(tmp, _safe(cf.filename))
                cf.save(p)
                text = _pdftotext(p)
                if text and 'CREDIT MEMO NUMBER' in text:
                    creds = parse_credits(text)
                    for t, memo in creds.items():
                        all_credited.add(t)
                        credit_details.setdefault(t, memo)
                elif text:
                    all_credited.update(parse_credits_fallback(text))
                else:
                    text = _ocr_text(p)
                    if text:
                        all_credited.update(parse_credits_fallback(text))
                if text:
                    for t, pair in credit_part_numbers(text).items():
                        credit_part_map.setdefault(t, pair)
                _rm(p)
                gc.collect()

        unclaimed, claimed = [], []
        for item in all_items:
            cp = credit_part_map.get(item['control'])
            if cp:
                # Shipper part was normalized in parse_shipper; credit part
                # in credit_part_numbers. Compare those normalized forms.
                item['credit_part'] = cp[0]
                item['credit_part_flagged'] = cp[1]
                item['part_match'] = parts_match(item.get('part'), cp[0])
            if item['control'] in all_credited:
                item['memo'] = credit_details.get(item['control'], '')
                claimed.append(item)
            else:
                unclaimed.append(item)

        def _sum(lst):
            return round(sum(
                float(it.get('amount', '0').replace(',', '') or '0')
                for it in lst), 2)

        # Standing rule: oldest shipper document date first (the core
        # return window runs from the ship date), not by dollar amount.
        unclaimed.sort(key=lambda x: (x.get('shipper_date') or '9999',
                                      x.get('control', '')))

        summary = {
            'shipper_count': len(all_items),
            'credited_count': len(claimed),
            'unclaimed_count': len(unclaimed),
            'shipper_total': _sum(all_items),
            'credited_total': _sum(claimed),
            'unclaimed_total': _sum(unclaimed),
        }
        store_id, actor = _account()
        if store_id:
            try:    # the history line is a nice-to-have, never a blocker
                acct_storage.log_quick_check(uuid.uuid4().hex, store_id,
                                             actor, summary)
            except Exception:
                import traceback
                traceback.print_exc()

        return jsonify(dict(
            summary, success=True, ocr_used=ocr_used,
            credit_tickets=len(all_credited), results=unclaimed))
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'error': f'Processing error: {str(e)}'}), 500


# ── Full Reconciliation ────────────────────────────────────────────────
@app.route('/reconcile')
def reconcile_page():
    store_id, _ = _account()
    saved = acct_storage.current_master(store_id) if store_id else None
    return render_template('reconcile.html', active='reconcile',
                           saved_master=saved)


def _today_central():
    try:
        from zoneinfo import ZoneInfo
        return datetime.datetime.now(ZoneInfo('America/Chicago')).date()
    except Exception:
        return (datetime.datetime.utcnow() -
                datetime.timedelta(hours=5)).date()


def _flag(name, default):
    v = request.form.get(name)
    if v is None:
        return default
    return v.strip().lower() in ('1', 'true', 'on', 'yes')


def _save_uploads(files, folder, allowed, label):
    saved = []
    for n, f in enumerate(files):
        if not f or not f.filename:
            continue
        ext = os.path.splitext(f.filename)[1].lower()
        if ext not in allowed:
            raise ValueError(f'{f.filename}: {label} must be '
                             f'{" / ".join(sorted(allowed))}.')
        safe = secure_filename(f.filename) or ('upload' + ext)
        path = os.path.join(folder, f'{n:02d}_{safe}')
        f.save(path)
        saved.append((path, os.path.basename(f.filename)))
    return saved


@app.route('/api/reconcile', methods=['POST'])
def api_reconcile():
    recon_jobs.cleanup()
    store_id, actor = _account()
    # Looked up before anything is created, so a database hiccup here
    # leaves no half-made job behind.
    saved_now = acct_storage.current_master(store_id) if store_id else None
    admin = bool(store_id) and acct_web.is_admin()
    job_id, d = recon_jobs.new_job(
        meta={'store_id': store_id} if store_id else None)
    inbox = os.path.join(d, 'in')
    try:
        master = _save_uploads([request.files.get('master')], inbox,
                               {'.xlsx', '.xlsm'}, 'The master workbook')
        memos = _save_uploads(request.files.getlist('memos'), inbox,
                              {'.pdf'}, 'Credit memos')
        shippers = _save_uploads(request.files.getlist('shippers'), inbox,
                                 {'.pdf'}, 'Shipper scans')
        dc = _save_uploads(request.files.getlist('dc'), inbox,
                           {'.csv', '.txt', '.xlsx', '.xlsm', '.xls'},
                           'DealerCONNECT files')
    except ValueError as e:
        recon_jobs.discard(job_id)
        return jsonify({'error': str(e)}), 400
    if store_id and master and not admin and saved_now:
        # Swapping the store's saved master for another workbook is an
        # admin's call; everyone else runs from the saved one.
        recon_jobs.discard(job_id)
        return jsonify({'error': 'Only a store admin can replace the '
                                 'saved master. Run without a workbook to '
                                 'use the saved one.'}), 403
    dc_text = request.form.get('dc_text', '')
    if not (master or memos or shippers or dc or dc_text.strip()):
        recon_jobs.discard(job_id)
        return jsonify({'error': 'Add at least one file: the master '
                                 'workbook, a credit memo, a shipper scan '
                                 'or DealerCONNECT data.'}), 400
    try:
        asof = datetime.date.fromisoformat(request.form.get('asof', ''))
    except ValueError:
        asof = _today_central()
    try:
        threshold = max(1, min(365, int(request.form.get('threshold',
                                                         60))))
    except ValueError:
        threshold = 60
    dealer = re.sub(r'\D', '', request.form.get('dealer', ''))[:8]
    if store_id and not dealer:
        dealer = g.store.get('dealer_code') or ''
    opts = {
        'asof': asof, 'threshold': threshold, 'dealer': dealer,
        'mark_requested': _flag('mark_requested', True),
        'apply_dc': _flag('apply_dc', True),
        'include_prev': _flag('include_prev', False),
    }
    inputs = {'master': master[0] if master else None, 'memos': memos,
              'shippers': shippers, 'dc': dc, 'dc_text': dc_text}
    out_dir = os.path.join(d, 'out')
    state = {'master_in': None}
    replacing = bool(store_id and master and saved_now)

    def work(progress):
        # With accounts on and no workbook uploaded, the run starts from
        # the store's saved master. It is read here, when the run actually
        # starts (runs go one at a time), so two runs in a row never both
        # start from the same old copy.
        if replacing:
            try:    # this workbook is about to replace a saved master
                acct_storage.check_master_upload(inputs['master'][0])
            except acct_storage.NotAMaster as e:
                raise RuntimeError(str(e))
        if store_id and inputs['master'] is None:
            row, content = acct_storage.master_file(store_id)
            if row:
                os.makedirs(inbox, exist_ok=True)
                name = secure_filename(row['filename']) or \
                    'Core_Returns_RECONCILED.xlsx'
                path = os.path.join(inbox, 'saved_' + name)
                with open(path, 'wb') as fh:
                    fh.write(content)
                inputs['master'] = (path, row['filename'])
                state['master_in'] = row['id']
        try:
            return recon_engine.run(inputs, opts, out_dir, progress)
        except MasterError as e:
            raise RuntimeError(str(e))

    if not store_id:
        recon_jobs.start(job_id, work)
        return jsonify({'job_id': job_id})

    def on_done(jid, result, out):
        # A run can take minutes. If the database happens to be away at
        # the moment it ends, wait for it rather than lose the save. Only
        # when nothing was sent yet (Unavailable), so a save is never
        # applied twice.
        for wait in SAVE_RETRY_WAITS + (None,):
            try:
                saved = acct_storage.finish_run(
                    jid, store_id, actor, result, out,
                    master_in=state['master_in'], may_replace=admin)
                break
            except acct_db.Unavailable:
                if wait is None:
                    raise
                time.sleep(wait)
        return {'saved': True, 'master_held': saved['held']}

    def on_error(jid, message):
        acct_storage.fail_run(jid, message)

    try:
        acct_storage.begin_run(
            job_id, store_id, actor, 'reconcile', asof=asof,
            options=dict(opts, asof=asof.isoformat(),
                         uploaded_master=bool(master)))
    except Exception:
        import traceback
        traceback.print_exc()
        recon_jobs.discard(job_id)
        return jsonify({'error': 'Could not reach your store\'s account '
                                 'just now. Nothing was started. Try again '
                                 'in a minute.'}), 503
    recon_jobs.start(job_id, work, on_done=on_done, on_error=on_error)
    return jsonify({'job_id': job_id})


def _saved_run(job_id):
    """A run from this store's history, shaped like a live job."""
    if not recon_jobs.JOB_ID_RE.match(job_id or ''):
        return None
    run = acct_storage.get_run(job_id, g.store['id'])
    if not run or run['kind'] != 'reconcile':
        return None
    status, error = run['status'], run['error']
    if status == 'running':
        # not in this server's memory any more: it restarted mid-run
        status, error = 'error', acct_storage.INTERRUPTED
    result = run['result']
    if result is not None:
        result = dict(result, from_history=True)
    return {'id': run['id'], 'status': status, 'error': error,
            'message': 'Done' if status == 'done' else 'Stopped',
            'log': [], 'elapsed': 0, 'result': result}


@app.route('/api/jobs/<job_id>')
def api_job(job_id):
    gone = (jsonify({'error': 'That run has expired or does not '
                              'exist. Start a new run.'}), 404)
    job = recon_jobs.get(job_id)
    if not accounts.enabled():
        return jsonify(job) if job else gone
    # A run belongs to one store. Anyone else gets "does not exist".
    if job:
        owner = recon_jobs.meta(job_id)
        if not owner or owner.get('store_id') != g.store['id']:
            return gone
        return jsonify(job)
    saved = _saved_run(job_id)
    return jsonify(saved) if saved else gone


@app.route('/api/jobs/<job_id>/files/<path:name>')
def api_job_file(job_id, name):
    if not accounts.enabled():
        path = recon_jobs.output_path(job_id, name)
        if not path:
            abort(404)
        return send_file(path, as_attachment=True, download_name=name)
    owner = recon_jobs.meta(job_id)
    if owner is not None:
        if owner.get('store_id') != g.store['id']:
            abort(404)
        path = recon_jobs.output_path(job_id, name)
        if path:
            return send_file(path, as_attachment=True, download_name=name)
    # not on this server any more (or never was): the store's history
    saved = _saved_run(job_id)
    if not saved or saved['status'] != 'done':
        abort(404)
    listed = {f['name']: f for f in (saved['result'] or {}).get('files', [])}
    if name not in listed:
        abort(404)
    if listed[name].get('kind') == 'zip':
        content = acct_storage.run_zip(job_id)
    else:
        content = acct_storage.run_file(job_id, name)
    if content is None:
        abort(404)
    return send_file(io.BytesIO(content), as_attachment=True,
                     download_name=name)


def _safe(fn):
    name = secure_filename(fn)
    return name if name else 'upload.pdf'


def _rm(path):
    try:
        os.remove(path)
    except OSError:
        pass


if __name__ == '__main__':
    app.run(debug=True)
