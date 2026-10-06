"""PDF text / OCR helpers built on poppler-utils and the tesseract CLI.

Memory-safe the same way the Quick Check is: pages are rendered to disk
by pdftoppm, read by one tesseract process per chunk, then deleted. Page
images never load into Python.
"""
import csv
import io
import os
import re
import shutil
import subprocess
import tempfile

OCR_DPI = 200          # 1 CPU / 2 GB plan: 200 DPI reads tickets more reliably
CHUNK_PAGES = 5        # pages rendered + OCR'd per tesseract call


def page_count(path):
    try:
        r = subprocess.run(['pdfinfo', path], capture_output=True,
                           text=True, timeout=30)
        m = re.search(r'Pages:\s+(\d+)', r.stdout or '')
        if m:
            return int(m.group(1))
    except Exception:
        pass
    return 0


def pdf_text(path, first=None, last=None, layout=True):
    """pdftotext output ('' when the PDF has no text layer)."""
    cmd = ['pdftotext']
    if layout:
        cmd.append('-layout')
    if first:
        cmd += ['-f', str(first)]
    if last:
        cmd += ['-l', str(last)]
    cmd += [path, '-']
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        return r.stdout or ''
    except Exception:
        return ''


def _render(path, first, last, dpi, out_dir, gray=True):
    prefix = os.path.join(out_dir, 'p')
    cmd = ['pdftoppm', '-r', str(dpi), '-f', str(first), '-l', str(last)]
    if gray:
        cmd.insert(1, '-gray')
    subprocess.run(cmd + [path, prefix], capture_output=True,
                   timeout=60 + 20 * (last - first + 1))
    imgs = [os.path.join(out_dir, n) for n in os.listdir(out_dir)
            if n.startswith('p') and n.endswith(('.pgm', '.ppm'))]
    imgs.sort(key=lambda p: int(re.search(r'-(\d+)\.p[gp]m$', p).group(1)))
    return imgs


def _tess_env():
    env = os.environ.copy()
    env['OMP_THREAD_LIMIT'] = '1'
    return env


def ocr_pdf(path, dpi=OCR_DPI, progress=None, label=''):
    """OCR every page; returns a list of page dicts with word boxes.

    Each page: {'page': n (1-based), 'width': px, 'height': px, 'dpi': dpi,
    'lines': [{'text', 'words': [{'text','left','top','width','height',
    'conf'}], 'left','top','right','bottom'}]}
    """
    total = page_count(path)
    pages = []
    if total < 1:
        return pages
    for start in range(1, total + 1, CHUNK_PAGES):
        end = min(total, start + CHUNK_PAGES - 1)
        td = tempfile.mkdtemp(prefix='ocr_')
        try:
            imgs = _render(path, start, end, dpi, td)
            if not imgs:
                continue
            list_path = os.path.join(td, 'pages.txt')
            with open(list_path, 'w') as fh:
                fh.write('\n'.join(imgs) + '\n')
            page_map = [int(re.search(r'-(\d+)\.p[gp]m$', p).group(1))
                        for p in imgs]
            r = subprocess.run(
                ['tesseract', list_path, 'stdout', '--psm', '6', 'tsv'],
                capture_output=True, text=True, env=_tess_env(),
                timeout=90 + 30 * len(imgs))
            pages.extend(_parse_tsv(r.stdout or '', page_map, dpi))
        finally:
            shutil.rmtree(td, ignore_errors=True)
        if progress:
            progress(end, total, label)
    return pages


def _parse_tsv(tsv, page_map, dpi):
    pages = {}
    lines = {}
    reader = csv.reader(io.StringIO(tsv), delimiter='\t',
                        quoting=csv.QUOTE_NONE)
    header = next(reader, None)
    if not header:
        return []
    idx = {name: i for i, name in enumerate(header)}
    for row in reader:
        if len(row) < len(header):
            continue
        try:
            level = int(row[idx['level']])
            seq = int(row[idx['page_num']])
            if not 1 <= seq <= len(page_map):
                continue
            pnum = page_map[seq - 1]
            left, top = int(row[idx['left']]), int(row[idx['top']])
            width, height = int(row[idx['width']]), int(row[idx['height']])
        except (ValueError, KeyError):
            continue
        if level == 1:
            pages[pnum] = {'page': pnum, 'width': width, 'height': height,
                           'dpi': dpi, 'lines': []}
            continue
        if level != 5:
            continue
        text = row[idx['text']].strip()
        if not text:
            continue
        try:
            conf = float(row[idx['conf']])
        except ValueError:
            conf = -1.0
        key = (pnum, row[idx['block_num']], row[idx['par_num']],
               row[idx['line_num']])
        lines.setdefault(key, []).append({
            'text': text, 'left': left, 'top': top, 'width': width,
            'height': height, 'conf': conf})
    for key in sorted(lines, key=lambda k: (k[0], int(k[1]), int(k[2]),
                                             int(k[3]))):
        words = sorted(lines[key], key=lambda w: w['left'])
        pnum = key[0]
        page = pages.setdefault(pnum, {'page': pnum, 'width': 0, 'height': 0,
                                       'dpi': dpi, 'lines': []})
        page['lines'].append({
            'text': ' '.join(w['text'] for w in words),
            'words': words,
            'left': min(w['left'] for w in words),
            'top': min(w['top'] for w in words),
            'right': max(w['left'] + w['width'] for w in words),
            'bottom': max(w['top'] + w['height'] for w in words),
        })
    out = [pages[k] for k in sorted(pages)]
    for p in out:
        p['lines'].sort(key=lambda ln: (ln['top'], ln['left']))
    return out


def ocr_text(path, dpi=OCR_DPI, progress=None, label=''):
    """Plain OCR text for a scanned PDF (one line per OCR line)."""
    out = []
    for page in ocr_pdf(path, dpi=dpi, progress=progress, label=label):
        out.extend(ln['text'] for ln in page['lines'])
        out.append('\f')
    return '\n'.join(out)
