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
from flask import Flask, render_template, request, jsonify
from werkzeug.utils import secure_filename
import subprocess
import re
import tempfile
import os
import gc
import shutil

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50 MB max upload

# Cap OCR pages so a huge scan can't run until OOM/timeout (Starter-safe default)
MAX_OCR_PAGES = 20
OCR_DPI = 150  # was 200; lower RAM/time with small accuracy tradeoff
# Letter at 150 DPI is 1650 px on the long edge. Cap so a bad page box
# cannot rasterize into a multi-hundred-MB bitmap. Normal scans unchanged.
OCR_SCALE_TO = 1650

# ── regex ────────────────────────────────────────────────────────────────
CTRL_RE = re.compile(r'^([Cc©€6][A-Za-z0-9]\d{7,8})\b')
AMT_RE = re.compile(r'([\d,]+\.\d{2})\s*$')
WRAP_RE = re.compile(r'^\s*(\d+)\s+([\d,]+\.\d{2})\s*$')
CLAIM_RE = re.compile(r'^\d{5,6}$')
CM_HDR_RE = re.compile(r'CREDIT MEMO NUMBER:\s*03181000(CC\d+)')
CM_REF_RE = re.compile(r'REFERENCE/CONTROL\s+NUMBER\s+(\S+.*)')

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
    while i < len(lines):
        line = lines[i].strip()
        i += 1
        if not line:
            continue
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
                results.append(_row(control, '', '', '', '1', amount))
                continue
            qty = _fix_qty(tokens.pop())
            claim = ''
            if tokens and CLAIM_RE.match(tokens[0]):
                claim = tokens.pop(0)
            part = tokens.pop(0) if tokens else ''
            desc = ' '.join(tokens)
            results.append(_row(control, claim, part, desc, qty, amount))
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
            part = tokens.pop(0) if tokens else ''
            desc = ' '.join(tokens)
            results.append(_row(control, claim, part, desc, qty, amount))
    return results


def _row(ctrl, claim, part, desc, qty, amt):
    return dict(control=ctrl, claim=claim, part=part,
                description=desc, qty=qty, amount=amt)


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


@app.route('/')
def index():
    return render_template('index.html')


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
                    text = _ocr_text(p)
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
                    ocr_text = _ocr_text(p)
                    if ocr_text:
                        all_credited.update(parse_credits_fallback(ocr_text))
                _rm(p)
                gc.collect()

        unclaimed, claimed = [], []
        for item in all_items:
            if item['control'] in all_credited:
                item['memo'] = credit_details.get(item['control'], '')
                claimed.append(item)
            else:
                unclaimed.append(item)

        def _sum(lst):
            return round(sum(
                float(it.get('amount', '0').replace(',', '') or '0')
                for it in lst), 2)

        unclaimed.sort(
            key=lambda x: float(
                x.get('amount', '0').replace(',', '') or '0'),
            reverse=True)

        return jsonify({
            'success': True,
            'ocr_used': ocr_used,
            'shipper_count': len(all_items),
            'credited_count': len(claimed),
            'unclaimed_count': len(unclaimed),
            'shipper_total': _sum(all_items),
            'credited_total': _sum(claimed),
            'unclaimed_total': _sum(unclaimed),
            'credit_tickets': len(all_credited),
            'results': unclaimed,
        })
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'error': f'Processing error: {str(e)}'}), 500


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
