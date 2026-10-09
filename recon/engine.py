"""Weekly core-return reconciliation — the same cycle the Cowork
"Core Reconciliation" project runs, as plain code.

Order of work (each step only touches the in-memory master):
  1. Read the master workbook (or start an empty one).
  2. OCR the signed GCRS shipper scans: add new tickets with their SHP#
     date, fill blanks on existing rows, and remember which page each
     ticket is on.
  3. Apply the weekly credit memos: write the CC memo # on each credited
     ticket (credits for tickets we never tracked are added as paid rows).
  4. Apply DealerCONNECT payment data (optional): paid per Mopar, data
     fixes, and the master-vs-Mopar comparison.
  5. Age the unpaid tickets, build the Credit Request (>= threshold days,
     not requested before), the marked shipper-page PDF and the re-scan
     list.
"""
import collections
import datetime
import os
import re
import zipfile

from . import dealerconnect as DC
from . import master as M
from . import memo as MEMO
from . import outputs as OUT
from . import shipper as SH
from .common import (clean_key, hamming1, is_valid_ticket, money_out,
                     part_looks_valid, shipment_date)


class Ledger:
    def __init__(self, rows):
        self.rows = rows
        self.index = collections.defaultdict(list)
        self.by_len = collections.defaultdict(set)
        for r in rows:
            self._idx(r)

    def _idx(self, r):
        k = clean_key(r['ticket'])
        self.index[k].append(r)
        self.by_len[len(k)].add(k)

    def find(self, ticket):
        return self.index.get(clean_key(ticket), [])

    def add(self, row):
        self.rows.append(row)
        self._idx(row)
        return row

    def near(self, ticket):
        k = clean_key(ticket)
        return [c for c in self.by_len.get(len(k), ()) if hamming1(c, k)]


class PartCorrector:
    """Snap an OCR'd part number to one this dealer has returned before.

    Cores repeat, so the master's history is a good dictionary: an exact
    hit is kept; otherwise a unique known part with the same body (only the
    two-letter suffix misread), or a unique known part one character away,
    is used instead.
    """

    def __init__(self, parts):
        self.count = collections.Counter(p for p in parts if p)
        self.by_body = collections.defaultdict(list)
        for p in self.count:
            if len(p) > 2:
                self.by_body[p[:-2]].append(p)

    def fix(self, part):
        if not part or part in self.count:
            return part, False
        same_body = self.by_body.get(part[:-2], [])
        if same_body:
            best = max(same_body, key=lambda p: self.count[p])
            return best, True
        near = [p for p in self.count if len(p) == len(part) and
                sum(a != b for a, b in zip(p, part)) == 1]
        if len(near) == 1:
            return near[0], True
        if len(part) > 8:   # one extra character read (CSSMZ461AZA)
            drops = {part[:i] + part[i + 1:] for i in range(len(part))}
            hits = [d for d in drops if d in self.count]
            if len(hits) == 1:
                return hits[0], True
        return part, False


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _same_amount(a, b):
    a, b = _num(a), _num(b)
    return a is not None and b is not None and abs(a - b) < 0.005


def _memo_num(m):
    digits = ''.join(ch for ch in str(m or '') if ch.isdigit())
    return int(digits) if digits else None


def same_memo(a, b):
    """CC00202022 and DealerCONNECT's #000202022 are the same memo."""
    if not a or not b:
        return False
    if str(a).strip().upper() == str(b).strip().upper():
        return True
    na, nb = _memo_num(a), _memo_num(b)
    return na is not None and na == nb


def _claim_s(c):
    return '' if c is None else str(c).strip().upper()


def _plausible_shipment(sid, asof):
    """Fix an OCR'd SHP# year (2036 -> 2026) and reject future dates."""
    d = shipment_date(sid)
    if not d:
        return None
    if d > asof + datetime.timedelta(days=2):
        for back in (10, 1):
            try:
                d2 = d.replace(year=d.year - back)
            except ValueError:
                continue
            if asof - datetime.timedelta(days=730) <= d2 <= asof:
                return d2.isoformat() + str(sid)[10:]
        return None
    if d < asof - datetime.timedelta(days=3 * 365):
        return None
    return sid


class Reconciler:
    def __init__(self, opts, progress=None):
        self.o = opts
        self.asof = opts['asof']
        self.progress = progress or (lambda *a, **k: None)
        self.r = {
            'warnings': [], 'inputs': {}, 'new_shipments': [],
            'scan_fills': [], 'ocr_review': [], 'matched': [],
            'already_applied': 0, 'partial': [], 'unmatched_credits': [],
            'dup_credits': [], 'other_credits': [], 'memo_totals': [],
            'data_fixes': [], 'paid_per_mopar': [], 'comparison': [],
            'comparison_summary': [], 'unpaid': [], 'aging': [],
            'credit_request': [], 'prev_requested': [], 'rescan': [],
            'files': [], 'pages_scanned': 0,
        }
        self.page_refs = collections.defaultdict(list)
        self.dc = {}
        self.dealer = (opts.get('dealer') or '').strip() or None
        # Every ticket exactly as read from the uploaded shipper pages, and
        # every ticket credited on the uploaded memos. A ticket that is in
        # the paperwork under its own number is never treated as a misread
        # of its neighbour (GCRS numbers run in sequence, so real tickets
        # one digit apart sit next to each other all the time).
        self.read_on = collections.defaultdict(list)
        self.read_tickets = set()
        self.credit_tickets = set()
        self.parts = PartCorrector([])

    # ── step 1 ──────────────────────────────────────────────────────────
    def load_master(self, path, name):
        if path:
            rows, info = M.read_master(path)
            self.r['inputs']['master'] = name
            self.r['inputs']['master_rows'] = len(rows)
        else:
            rows = []
            self.r['inputs']['master'] = '(new master)'
            self.r['inputs']['master_rows'] = 0
        self.ledger = Ledger(rows)
        dups = [k for k, v in self.ledger.index.items() if len(v) > 1]
        if dups:
            self.r['warnings'].append(
                f'The master has {len(dups)} control tickets listed more '
                f'than once (e.g. {", ".join(sorted(dups)[:3])}). They were '
                f'left as they are.')

    # ── step 2 ──────────────────────────────────────────────────────────
    def read_shippers(self, files):
        pages = []
        names = []
        for path, name in files:
            names.append(name)
            self.progress('ocr', f'Reading shipper scan {name}')
            try:
                got = SH.read_shipper_pdf(
                    path, file_label=name,
                    progress=lambda done, total, label:
                    self.progress('ocr', f'OCR {label}: page {done} of '
                                         f'{total}'))
            except Exception:
                import traceback
                traceback.print_exc()
                got = []
            if not any(pg['lines'] for pg in got):
                # A memo dropped in the wrong box, a damaged or empty file:
                # without this the run would quietly show zeros.
                self.r['warnings'].append(
                    f'{name}: no control tickets could be read from this '
                    f'file, so nothing from it was used. Check that it is '
                    f'the scan of the signed GCRS Dealer Shipper Document '
                    f'and that it went in the shipper scans box.')
            pages.extend(got)
        self.r['inputs']['shippers'] = names
        self.r['pages_scanned'] = len(pages)
        self.pages = pages
        for p in pages:
            if p.get('dealer') and not self.dealer:
                self.dealer = p['dealer']
        self.parts = PartCorrector(
            [clean_key(r.get('part')) for r in self.ledger.rows] +
            [d['part'] for d in self.dc.values() if d.get('part')])
        self._resolve_lines()
        self._apply_scans()

    def _evidence(self, ln, part, amount):
        """How a scanned line compares with what is on record for a
        ticket: (things that agree, things that differ), over the part
        number and the amount. A blank or unreadable value counts for
        neither."""
        same = diff = 0
        known = clean_key(part)
        read = clean_key(ln.get('part'))
        if known and read and part_looks_valid(read):
            if read == known or self.parts.fix(read)[0] == known:
                same += 1
            else:
                diff += 1
        if _num(amount) is not None and ln.get('amount') is not None:
            if _same_amount(amount, ln['amount']):
                same += 1
            else:
                diff += 1
        return same, diff

    @staticmethod
    def _others(page, skip):
        """The tickets on a page as read, leaving one out."""
        return {clean_key(ln['ticket']) for ln in page['lines']} - {skip}

    def _same_sheet(self, p, t, q, k):
        """True when pages p and q look like two copies of one shipper
        page: same shipment, and apart from the two tickets in question
        (t on p, k on q) they list mostly the same tickets."""
        if q is p or q.get('shipment') != p.get('shipment'):
            return False
        mine, theirs = self._others(p, t), self._others(q, k)
        if not mine and not theirs:
            return True
        return bool(mine and theirs and len(mine & theirs) * 2 >=
                    min(len(mine), len(theirs)))

    def _could_be(self, t, p, k):
        """Could ticket t, read on page p, be a misread of ticket k?

        Not when the paperwork shows k under its own number somewhere
        that is not a second copy of this very page: GCRS numbers run in
        sequence, so real tickets one digit apart are everywhere. Yes
        when k is on no uploaded page, or only on another copy of p."""
        where = self.read_on.get(k, [])
        if not where:
            return True
        if any(q is p for q in where):
            return False
        return all(self._same_sheet(p, t, q, k) for q in where)

    def _resolve_lines(self):
        by_shp = collections.defaultdict(set)
        for k, rows in self.ledger.index.items():
            if rows[0].get('shipment'):
                by_shp[rows[0]['shipment']].add(k)
        self.read_on = collections.defaultdict(list)
        for p in self.pages:
            for ln in p['lines']:
                if not any(q is p for q in self.read_on[clean_key(
                        ln['ticket'])]):
                    self.read_on[clean_key(ln['ticket'])].append(p)
        self.read_tickets = set(self.read_on)
        # 1. tickets already on the master, and each page's shipment
        for p in self.pages:
            exact = set()
            for ln in p['lines']:
                ln['match'] = None
                ln['how'] = None
                if self.ledger.find(ln['ticket']):
                    ln['match'], ln['how'] = ln['ticket'], 'exact'
                    exact.add(ln['ticket'])
            votes = collections.Counter()
            for t in exact:
                sid = self.ledger.find(t)[0].get('shipment')
                if sid:
                    votes[sid] += 1
            if votes:
                p['shipment'] = votes.most_common(1)[0][0]
                p['shipment_source'] = 'master'
            else:
                p['shipment'] = _plausible_shipment(p['shipment_id'],
                                                    self.asof)
                p['shipment_source'] = 'scan' if p['shipment'] else None
        # 2. lines one digit away from a master ticket on the same shipment
        for p in self.pages:
            same_shp = by_shp.get(p['shipment'], set()) \
                if p['shipment'] else set()
            taken = {ln['match'] for ln in p['lines'] if ln['match']}
            for ln in p['lines']:
                if ln['match']:
                    continue
                t = clean_key(ln['ticket'])
                cands = [c for c in self.ledger.near(t)
                         if c in same_shp and c not in taken and
                         self._could_be(t, p, c)]
                if len(cands) != 1:
                    continue
                row = self.ledger.find(cands[0])[0]
                same, diff = self._evidence(ln, row.get('part'),
                                            row.get('amount'))
                if diff:
                    # the part or amount on the line says it is not that
                    # ticket: leave it to be added as its own
                    self.r['ocr_review'].append({
                        'file': p['file'], 'page': p['page'],
                        'ticket': ln['ticket'],
                        'issue': f'Scan read {ln["ticket"]}. The master has '
                                 f'{cands[0]} on the same shipment, one '
                                 f'digit different, but with a different '
                                 f'part or amount, so {ln["ticket"]} was '
                                 f'kept as its own ticket. Check the page.'})
                    continue
                ln['match'], ln['how'] = cands[0], 'ocr-fix'
                taken.add(cands[0])
            for ln in p['lines']:
                if ln['match']:
                    self.page_refs[ln['match']].append((p, ln))

    @staticmethod
    def _page_tickets(page, skip):
        return {ln.get('match') or ln['ticket'] for ln in page['lines']} - \
            {skip}

    def _copy_twin(self, t, page, ln, seen_new, new_page):
        """A ticket added from another copy of the same shipper page that
        differs from t by one digit, or None. The part and amount on the
        line must not say it is a different ticket."""
        mine = self._page_tickets(page, t)
        hits = []
        for n in sorted(seen_new):
            q = new_page[n]
            if q is page or not hamming1(n, t) or \
                    q.get('shipment') != page.get('shipment'):
                continue
            theirs = self._page_tickets(q, n)
            if not ((not mine and not theirs) or (
                    mine and theirs and len(mine & theirs) * 2 >=
                    min(len(mine), len(theirs)))):
                continue
            row = self.ledger.find(n)[0]
            if self._evidence(ln, row.get('part'), row.get('amount'))[1]:
                continue
            hits.append(n)
        if not hits:
            return None
        # the misread copy is the ticket that is missing from this page
        absent = [n for n in hits if n not in mine]
        return (absent or hits)[0]

    def _apply_scans(self):
        seen_new = set()
        new_page = {}
        for p in self.pages:
            sid = p.get('shipment')
            for ln in p['lines']:
                where = f'{p["file"]} p.{p["page"]}'
                if ln['match']:
                    row = self.ledger.find(ln['match'])[0]
                    if ln['how'] == 'ocr-fix':
                        self.r['ocr_review'].append({
                            'file': p['file'], 'page': p['page'],
                            'ticket': ln['match'],
                            'issue': f'Scan read {ln["ticket"]}; matched to '
                                     f'{ln["match"]} on the same shipment.'})
                    self._fill_from_scan(row, ln, sid, where)
                    continue
                t = ln['ticket']
                if t in seen_new:
                    continue
                d = self.dc.get(t)
                if d is None:
                    # DealerCONNECT has a ticket one digit away that is on
                    # no uploaded page under its own number: maybe the scan
                    # misread it. Believed only when the part or amount on
                    # the line agrees with DealerCONNECT and neither
                    # differs; otherwise the ticket stays as read.
                    near_dc = [k for k in self.dc
                               if hamming1(k, t) and not self.ledger.find(k)
                               and self._could_be(t, p, k)]
                    if len(near_dc) == 1:
                        cand = self.dc[near_dc[0]]
                        same, diff = self._evidence(ln, cand.get('part'),
                                                    cand.get('amount'))
                        if same and not diff:
                            d = cand
                            self.r['ocr_review'].append({
                                'file': p['file'], 'page': p['page'],
                                'ticket': d['ticket'],
                                'issue': f'Scan read {t}; DealerCONNECT has '
                                         f'{d["ticket"]} with the same part '
                                         f'and amount — used that.'})
                            t = d['ticket']
                            ln['ticket'] = t
                        elif not diff:
                            # nothing on the line to tell them apart
                            self.r['ocr_review'].append({
                                'file': p['file'], 'page': p['page'],
                                'ticket': t,
                                'issue': f'Scan read {t}. DealerCONNECT has '
                                         f'{cand["ticket"]}, one digit '
                                         f'different, and the part and '
                                         f'amount on this line could not be '
                                         f'read to tell them apart. Kept as '
                                         f'{t}. If the scan misread it, '
                                         f'correct the ticket on the '
                                         f'master.'})
                if not is_valid_ticket(t):
                    self.r['ocr_review'].append({
                        'file': p['file'], 'page': p['page'], 'ticket': t,
                        'issue': 'Unreadable control ticket — not added. '
                                 'Check the page and add it by hand.'})
                    continue
                if not sid:
                    self.r['ocr_review'].append({
                        'file': p['file'], 'page': p['page'], 'ticket': t,
                        'issue': 'Could not read the GCRS SHP# date on this '
                                 'page — ticket not added. Re-scan the page '
                                 'or add it by hand.'})
                    continue
                twin = self._copy_twin(t, p, ln, seen_new, new_page)
                if twin:
                    # the same page was scanned twice and one copy misread
                    # a digit (consecutive tickets on one page are kept)
                    self.r['ocr_review'].append({
                        'file': p['file'], 'page': p['page'], 'ticket': t,
                        'issue': f'Looks like {twin} (one digit off) from '
                                 f'another copy of this page — not added '
                                 f'twice.'})
                    continue
                flags = []
                part = (d or {}).get('part')
                if not part:
                    part, fixed = self.parts.fix(ln['part'])
                    if fixed:
                        flags.append(f'part read as {ln["part"]}, matched '
                                     f'to {part} (a part already on record)')
                amount = (d or {}).get('amount')
                if amount is None:
                    amount = ln['amount']
                claim = (d or {}).get('claim') or ln['claim']
                if d is None:
                    if not part_looks_valid(part or ''):
                        flags.append('part # unclear — check the page')
                    if ln['amount'] is None:
                        flags.append('amount unreadable — check the page')
                if p.get('shipment_source') == 'scan' and p['shipment_id'] \
                        and p['shipment_id'] != sid:
                    flags.append(f'SHP# read as {p["shipment_id"]}, '
                                 f'corrected to {sid}')
                row = self.ledger.add({
                    'memo': None, 'shipment': sid, 'ticket': t,
                    'part': part or None, 'claim': claim,
                    'amount': money_out(amount), 'requested': None})
                ln['match'] = t
                ln['how'] = 'new'
                self.page_refs[t].append((p, ln))
                seen_new.add(t)
                new_page[t] = p
                self.r['new_shipments'].append({
                    'shipment': sid, 'ticket': t, 'part': row['part'],
                    'claim': claim, 'amount': row['amount'],
                    'source': 'DealerCONNECT' if d else 'scan',
                    'file': p['file'], 'page': p['page'],
                    'flags': '; '.join(flags)})

    def _fill_from_scan(self, row, ln, sid, where):
        fills = []
        if not row.get('shipment') and sid:
            row['shipment'] = sid
            fills.append(('Shipment ID', '', sid))
        if row.get('amount') is None and ln.get('amount') is not None:
            row['amount'] = money_out(ln['amount'])
            fills.append(('Credit Amount', '', row['amount']))
        if not row.get('part') and ln.get('part'):
            row['part'] = ln['part']
            fills.append(('Core Part #', '', ln['part']))
        if row.get('claim') is None and ln.get('claim'):
            row['claim'] = ln['claim']
            fills.append(('Claim #', '', ln['claim']))
        for field, old, new in fills:
            self.r['scan_fills'].append({'ticket': row['ticket'],
                                         'field': field, 'value': new,
                                         'where': where})

    # ── step 3 ──────────────────────────────────────────────────────────
    def apply_memos(self, files):
        names = []
        parsed = []
        for path, name in files:
            names.append(name)
            self.progress('memo', f'Reading credit memo {name}')
            try:
                res = MEMO.parse_memo_pdf(path, source=name)
            except Exception:
                import traceback
                traceback.print_exc()
                self.r['warnings'].append(
                    f'{name}: could not be read, so no credits from it '
                    f'were applied. Check that it is the Mopar weekly core '
                    f'return credit memo PDF.')
                continue
            parsed.append((name, res))
        # every ticket credited in this batch, before any is applied
        self.credit_tickets = {clean_key(c['ticket'])
                               for _, res in parsed
                               for c in res['credits'] if c['ticket']}
        for name, res in parsed:
            if res.get('dealer') and not self.dealer:
                self.dealer = res['dealer']
            self.r['warnings'].extend(res['warnings'])
            for m in res['memos'].values():
                ok = m['net_total'] is not None and \
                    abs(m['net_total'] - m['lines_total']) < 0.005
                self.r['memo_totals'].append({
                    'memo': m['memo'], 'date': m['date'].isoformat()
                    if m['date'] else '', 'file': name,
                    'net_total': m['net_total'],
                    'lines_total': round(m['lines_total'], 2), 'ties': ok,
                    'ocr': res.get('ocr', False)})
            for c in res['credits']:
                if c['ticket']:
                    self._apply_credit(c, res.get('ocr', False))
                else:
                    self.r['other_credits'].append(c)
        self.r['inputs']['memos'] = names

    def _apply_credit(self, c, from_ocr):
        t = clean_key(c['ticket'])
        rows = self.ledger.find(t)
        if not rows and from_ocr:
            # A scanned (not text) memo can misread a digit. Snap to an
            # unpaid master ticket one digit away only when that ticket is
            # not credited under its own number in this batch and the
            # amount is the same; and say so.
            cands = [k for k in self.ledger.near(t)
                     if k not in self.credit_tickets and any(
                         not r['memo'] and _same_amount(r.get('amount'),
                                                        c['amount'])
                         for r in self.ledger.find(k))]
            if len(cands) == 1:
                self.r['warnings'].append(
                    f'Credit memo {c["memo"]} is a scan: ticket read as '
                    f'{t}, applied to {cands[0]} (one digit different, '
                    f'same amount, unpaid on the master). Check the memo.')
                t = cands[0]
                rows = self.ledger.find(t)
        amount = money_out(c['amount'])
        memo_date = c['memo_date'].isoformat() if c['memo_date'] else ''
        if rows:
            if any(same_memo(r.get('memo'), c['memo']) for r in rows):
                self.r['already_applied'] += 1
                return
            target = next((r for r in rows if not r.get('memo')), None)
            if target is None:
                prior = sorted({r['memo'] for r in rows if r.get('memo')})
                self.r['dup_credits'].append({
                    'memo': c['memo'], 'memo_date': memo_date, 'ticket': t,
                    'amount': amount, 'prior': ', '.join(prior)})
                self.ledger.add({
                    'memo': c['memo'], 'shipment': rows[0].get('shipment'),
                    'ticket': t, 'part': c['part'] or rows[0].get('part'),
                    'claim': c['claim'], 'amount': amount,
                    'requested': None})
                return
            expected = target.get('amount')
            target['memo'] = c['memo']
            if expected is None:
                target['amount'] = amount
            elif not _same_amount(expected, amount):
                self.r['partial'].append({
                    'memo': c['memo'], 'memo_date': memo_date,
                    'shipment': target.get('shipment'), 'ticket': t,
                    'part': target.get('part'), 'expected': expected,
                    'credited': amount,
                    'diff': round((amount or 0) - (_num(expected) or 0), 2)})
                target['amount'] = amount
            if c['part'] and not from_ocr and \
                    clean_key(target.get('part')) != c['part']:
                if target.get('part'):
                    self.r['data_fixes'].append({
                        'ticket': t, 'field': 'Core Part #',
                        'old': target.get('part'), 'new': c['part'],
                        'source': f'credit memo {c["memo"]}'})
                target['part'] = c['part']
            if c['claim'] is not None and \
                    _claim_s(target.get('claim')) != _claim_s(c['claim']):
                if target.get('claim') is not None and \
                        str(target.get('claim')).strip() != '':
                    self.r['data_fixes'].append({
                        'ticket': t, 'field': 'Claim #',
                        'old': target.get('claim'), 'new': c['claim'],
                        'source': f'credit memo {c["memo"]}'})
                target['claim'] = c['claim']
            self.r['matched'].append({
                'memo': c['memo'], 'memo_date': memo_date,
                'shipment': target.get('shipment'), 'ticket': t,
                'part': target.get('part'), 'claim': target.get('claim'),
                'expected': expected, 'credited': amount})
        else:
            self.ledger.add({'memo': c['memo'], 'shipment': None,
                             'ticket': t, 'part': c['part'] or None,
                             'claim': c['claim'], 'amount': amount,
                             'requested': None})
            self.r['unmatched_credits'].append({
                'memo': c['memo'], 'memo_date': memo_date, 'ticket': t,
                'part': c['part'], 'claim': c['claim'], 'amount': amount,
                'description': c.get('description', '')})

    # ── step 4 ──────────────────────────────────────────────────────────
    def load_dc(self, files, text):
        rows = []
        names = []
        for path, name in files:
            names.append(name)
            try:
                rows.extend(DC.read_dc_file(path, source=name))
            except DC.DCError as e:
                self.r['warnings'].append(str(e))
            except Exception:
                import traceback
                traceback.print_exc()
                self.r['warnings'].append(
                    f'{name}: could not be opened, so it was left out. If '
                    f'it is an Excel file, open it in Excel, choose Save As '
                    f'> Excel Workbook (.xlsx) or CSV, and add it again.')
        if text and text.strip():
            names.append('pasted rows')
            try:
                rows.extend(DC.read_dc_text(text))
            except DC.DCError as e:
                self.r['warnings'].append(str(e))
        self.r['inputs']['dealerconnect'] = names
        # Pull date from file names like unpaid_cores_12345_2026-08-03.csv
        pulls = []
        for n in names:
            m = re.search(r'(20\d{2})[-_]?(\d{2})[-_]?(\d{2})', n)
            if m:
                try:
                    pulls.append(datetime.date(int(m.group(1)),
                                               int(m.group(2)),
                                               int(m.group(3))))
                except ValueError:
                    pass
        self.dc_pull = max(pulls) if pulls else None
        self.r['dc_pull'] = self.dc_pull.isoformat() if self.dc_pull else ''
        self.dc_rows = rows
        self.dc = DC.merge(rows)
        self.dc_has_paid = any(r['status'] == 'PAID' for r in rows)
        self.dc_has_unpaid = any(r['status'] == 'UNPAID' for r in rows)

    def apply_dc(self):
        if not self.dc:
            return
        apply = self.o.get('apply_dc', True)
        comp = []
        for t, d in self.dc.items():
            rows = self.ledger.find(t)
            open_rows = [r for r in rows if not r.get('memo')]
            row = open_rows[0] if open_rows else (rows[0] if rows else None)
            base = {
                'ticket': t, 'date': (row or {}).get('shipment', '') and
                str(row['shipment'])[:10],
                'age': self._age(row), 'part_ours': (row or {}).get('part'),
                'part_dc': d['part'], 'claim': d['claim'],
                'amount_ours': (row or {}).get('amount'),
                'amount_dc': d['amount'],
                'requested': (row or {}).get('requested')}
            if d['status'] == 'PAID':
                if open_rows:
                    note = (f'Mopar shows this paid on {d["received"] or "?"}'
                            f' (document {d["document"] or "?"}).')
                    if apply:
                        row['memo'] = d['document'] or (
                            f'PAID {d["received"]}' if d['received']
                            else 'PAID (DealerCONNECT)')
                        if row.get('amount') is None and \
                                d['amount'] is not None:
                            row['amount'] = money_out(d['amount'])
                        note += ' Marked paid on the master.'
                    self.r['paid_per_mopar'].append(dict(base, note=note))
                    comp.append(dict(base, status='PAID PER MOPAR',
                                     note=note))
                continue
            if row is None:
                comp.append(dict(base, status='ADD TO MASTER', note=(
                    "Mopar shows this unpaid but it is not on the master. "
                    "Scan its signed shipper page and run again so it is "
                    "added with its shipment date.")))
                continue
            if not open_rows:
                note = (f'Master shows credit {row["memo"]} but Mopar still '
                        'lists it unpaid.')
                if self.dc_pull:
                    note += (f' Expected if that credit came after this '
                             f'DealerCONNECT pull ({self.dc_pull}).')
                comp.append(dict(base, status='CHECK - CREDITED ON MASTER',
                                 note=note))
                continue
            diffs = []
            if d['part'] and clean_key(row.get('part')) != d['part']:
                diffs.append(('Core Part #', row.get('part'), d['part']))
            if d['amount'] is not None and \
                    not _same_amount(row.get('amount'), d['amount']):
                diffs.append(('Credit Amount', row.get('amount'),
                              money_out(d['amount'])))
            if d['claim'] is not None and \
                    _claim_s(row.get('claim')) != _claim_s(d['claim']):
                diffs.append(('Claim #', row.get('claim'), d['claim']))
            if not diffs:
                comp.append(dict(base, status='AGREE', note=''))
                continue
            text = '; '.join(
                f'{f} {"blank" if o in (None, "") else o} → {n}'
                for f, o, n in diffs)
            if apply:
                for f, o, n in diffs:
                    key = {'Core Part #': 'part', 'Credit Amount': 'amount',
                           'Claim #': 'claim'}[f]
                    row[key] = n
                    self.r['data_fixes'].append({
                        'ticket': t, 'field': f, 'old': o, 'new': n,
                        'source': 'DealerCONNECT'})
                comp.append(dict(base, status='FIXED FROM DEALERCONNECT',
                                 note=text))
            else:
                comp.append(dict(base, status='FIX DATA', note=text))
        self.r['dc_after_pull'] = 0
        if self.dc_has_unpaid:
            for r in self.ledger.rows:
                if r.get('memo') or clean_key(r['ticket']) in self.dc:
                    continue
                sd = shipment_date(r.get('shipment'))
                if self.dc_pull and sd and sd >= self.dc_pull:
                    self.r['dc_after_pull'] += 1     # can't be on it yet
                    continue
                age = self._age(r)
                base = {'ticket': r['ticket'],
                        'date': str(r.get('shipment') or '')[:10],
                        'age': age, 'part_ours': r.get('part'),
                        'part_dc': None, 'claim': r.get('claim'),
                        'amount_ours': r.get('amount'), 'amount_dc': None,
                        'requested': r.get('requested')}
                recent = age is not None and age < 7
                if self.dc_has_paid:
                    note = ('Mopar has no record of this in the paid or '
                            'unpaid data uploaded. Escalate with the signed '
                            'shipper copy.')
                    status = 'MOPAR HAS NO RECORD'
                else:
                    note = ('Not on the DealerCONNECT unpaid list. Upload the '
                            'Paid export too to tell "paid" from "no '
                            'record".')
                    status = 'NOT ON MOPAR UNPAID LIST'
                if recent:
                    note = ('Shipped in the last 7 days — Mopar may not '
                            'have posted it yet. ') + note
                comp.append(dict(base, status=status, note=note))
        order = ['MOPAR HAS NO RECORD', 'NOT ON MOPAR UNPAID LIST',
                 'ADD TO MASTER', 'CHECK - CREDITED ON MASTER', 'FIX DATA',
                 'FIXED FROM DEALERCONNECT', 'PAID PER MOPAR', 'AGREE']
        comp.sort(key=lambda x: (order.index(x['status']),
                                 x['date'] or '9999', x['ticket']))
        self.r['comparison'] = comp
        meaning = {
            'MOPAR HAS NO RECORD': ("Unpaid on the master, absent from "
                                    "Mopar's paid and unpaid data",
                                    'The real problem list — escalate with '
                                    'the signed shipper copies.'),
            'NOT ON MOPAR UNPAID LIST': ("Unpaid on the master, not on "
                                         "Mopar's unpaid list",
                                         'Upload the Paid export to tell paid '
                                         'from no-record.'),
            'ADD TO MASTER': ("On Mopar's unpaid list but not on the master",
                              'Scan the signed shipper and run again.'),
            'CHECK - CREDITED ON MASTER': ('Credited on the master, Mopar '
                                           'still shows unpaid',
                                           'Check the credit memo — possible '
                                           'false credit on the master.'),
            'FIX DATA': ("Unpaid on both, our data doesn't match Mopar's",
                         'Correct the master (DealerCONNECT wins).'),
            'FIXED FROM DEALERCONNECT': ('Unpaid on both; master corrected '
                                         'from DealerCONNECT',
                                         'No action — fixed in the new '
                                         'master.'),
            'PAID PER MOPAR': ('Unpaid on the master, Mopar shows it paid',
                               'Marked paid with the DealerCONNECT document '
                               '#.'),
            'AGREE': ('Unpaid on both, everything matches',
                      'No action. Correctly pending with Mopar.'),
        }
        summ = []
        for s in order:
            items = [x for x in comp if x['status'] == s]
            if not items:
                continue
            amt = sum(_num(x['amount_dc'] if x['amount_dc'] is not None
                           else x['amount_ours']) or 0 for x in items)
            summ.append({'status': s, 'meaning': meaning[s][0],
                         'count': len(items), 'amount': round(amt, 2),
                         'action': meaning[s][1]})
        self.r['comparison_summary'] = summ
        self.r['dc_paid_not_on_master'] = sum(
            1 for t, d in self.dc.items()
            if d['status'] == 'PAID' and not self.ledger.find(t))

    # ── step 5 ──────────────────────────────────────────────────────────
    def _age(self, row):
        d = shipment_date((row or {}).get('shipment'))
        return (self.asof - d).days if d else None

    def build_request(self):
        th = int(self.o.get('threshold', 60))
        today = self.asof.isoformat()
        include_prev = self.o.get('include_prev', False)
        rows = M.sort_rows(self.ledger.rows)
        buckets = collections.OrderedDict(
            (k, [0, 0.0]) for k in ['Under 30 days', '30-59 days',
                                    '60-89 days', '90+ days',
                                    'No shipment date'])
        for r in rows:
            if r.get('memo'):
                continue
            age = self._age(r)
            amt = _num(r.get('amount')) or 0.0
            b = ('No shipment date' if age is None else
                 'Under 30 days' if age < 30 else
                 '30-59 days' if age < 60 else
                 '60-89 days' if age < 90 else '90+ days')
            buckets[b][0] += 1
            buckets[b][1] += amt
            listed = {
                'shipment': r.get('shipment'), 'ticket': r['ticket'],
                'part': r.get('part'), 'claim': r.get('claim'),
                'amount': r.get('amount'), 'age': age,
                'requested': r.get('requested')}
            self.r['unpaid'].append(listed)
            if age is None or age < th:
                continue
            prev = r.get('requested')
            if prev and prev != today and not include_prev:
                self.r['prev_requested'].append({
                    'shipment': r.get('shipment'), 'ticket': r['ticket'],
                    'part': r.get('part'), 'amount': r.get('amount'),
                    'age': age, 'requested': prev})
                continue
            refs = self._best_refs(r['ticket'])
            self.r['credit_request'].append({
                'date': str(r['shipment'])[:10], 'ticket': r['ticket'],
                'part': r.get('part'), 'amount': r.get('amount'),
                'age': age, 'previously_requested': prev
                if prev and prev != today else '',
                'page_found': bool(refs),
                'page': f'{refs[0][0]["file"]} p.{refs[0][0]["page"]}'
                if refs else ''})
            if self.o.get('mark_requested', True):
                r['requested'] = today
                listed['requested'] = today     # as the master now shows
        self.r['aging'] = [{'bucket': k, 'count': v[0],
                            'amount': round(v[1], 2)}
                           for k, v in buckets.items()]
        missing = collections.OrderedDict()
        for x in self.r['credit_request']:
            if not x['page_found']:
                missing.setdefault(x['date'], []).append(x['ticket'])
        self.r['rescan'] = [{'date': d, 'tickets': t}
                            for d, t in missing.items()]

    # ── outputs ─────────────────────────────────────────────────────────
    def write_outputs(self, out_dir, master_name):
        self.progress('files', 'Building the workbooks and PDFs')
        stamp = self.asof.isoformat()
        files = []

        def add(name, label, kind):
            files.append({'name': name, 'label': label, 'kind': kind,
                          'size': os.path.getsize(os.path.join(out_dir,
                                                               name))})

        from werkzeug.utils import secure_filename
        base = os.path.splitext(secure_filename(master_name or ''))[0]
        master_name = (base or 'Core_Returns_RECONCILED') + '.xlsx'
        M.write_master(self.ledger.rows, os.path.join(out_dir, master_name))
        add(master_name, 'Updated master (Core Returns + Unpaid Cores)',
            'master')

        req = self.r['credit_request']
        if req:
            OUT.write_credit_request_xlsx(
                req, self.dealer, os.path.join(out_dir, 'Credit_Request.xlsx'))
            add('Credit_Request.xlsx', 'Credit Request list for Mopar',
                'request')
            specs = self._page_specs(req)
            if specs:
                OUT.write_credit_request_pdf(
                    specs, os.path.join(out_dir, 'Credit_Request.pdf'))
                add('Credit_Request.pdf',
                    'Signed shipper pages marked "UnPaid"', 'request')

        scans = bool(self.r['pages_scanned'])
        if req:
            if not scans:
                note = ('No shipper scans were uploaded this run. These are '
                        'the signed shipper pages needed for the credit '
                        'request.')
            elif self.r['rescan']:
                note = ('These credit-request tickets were not found on any '
                        'uploaded shipper scan. Find or re-scan the signed '
                        'pages.')
            else:
                note = (f'No credit-request shipments are missing paperwork '
                        f'as of {stamp} — every requested ticket was found '
                        f'on an uploaded signed shipper page.')
            OUT.write_rescan_xlsx(self.r['rescan'], note,
                                  os.path.join(out_dir,
                                               'Shippers_To_Rescan.xlsx'))
            OUT.write_rescan_pdf(self.r['rescan'], note, stamp,
                                 os.path.join(out_dir,
                                              'Shippers_To_Rescan.pdf'))
            add('Shippers_To_Rescan.xlsx', 'Shippers To Re-Scan', 'rescan')
            add('Shippers_To_Rescan.pdf', 'Shippers To Re-Scan (print)',
                'rescan')
            self.r['rescan_note'] = note

        if self.r['comparison']:
            name = f'Unpaid_Comparison_{stamp}.xlsx'
            info = (f'Master: {self.r["inputs"].get("master")}   |   '
                    f'DealerCONNECT: '
                    f'{", ".join(self.r["inputs"].get("dealerconnect", []))} '
                    f'({len(self.dc_rows)} lines)')
            OUT.write_comparison_xlsx(self.r['comparison'],
                                      self.r['comparison_summary'], info,
                                      os.path.join(out_dir, name))
            add(name, 'Master vs. DealerCONNECT comparison', 'compare')

        name = f'Core_Recon_Report_{stamp}.xlsx'
        OUT.write_report_xlsx(self._report_sheets(),
                              os.path.join(out_dir, name))
        add(name, 'Run report (Matched, Partial Credits, Uncredited, '
                  'Unmatched Credits, ...)', 'report')

        zname = f'Core_Reconciliation_{stamp}.zip'
        with zipfile.ZipFile(os.path.join(out_dir, zname), 'w',
                             zipfile.ZIP_DEFLATED) as z:
            for f in files:
                z.write(os.path.join(out_dir, f['name']), f['name'])
        add(zname, 'Everything in one zip', 'zip')
        self.r['files'] = files

    def _best_refs(self, ticket):
        refs = self.page_refs.get(clean_key(ticket), [])
        return sorted(refs, key=lambda pl: (
            'credit_request' in pl[0]['file'].lower().replace(' ', '_'),
            pl[1].get('how') == 'ocr-fix'))

    def _page_specs(self, req):
        wanted = collections.OrderedDict()
        for x in req:
            refs = self._best_refs(x['ticket'])
            if not refs:
                continue
            p, ln = refs[0]
            key = (p['path'], p['page'])
            spec = wanted.setdefault(key, {
                'path': p['path'], 'page': p['page'], 'width': p['width'],
                'height': p['height'], 'checkoff_x': p.get('checkoff_x'),
                'boxes': [], 'sort': (str(p.get('shipment') or ''),
                                      p['file'], p['page'])})
            spec['boxes'].append(ln['box'])
        return sorted(wanted.values(), key=lambda s: s['sort'])

    def _report_sheets(self):
        r = self.r
        tot_unpaid = sum(_num(x['amount']) or 0 for x in r['unpaid'])
        tot_req = sum(_num(x['amount']) or 0 for x in r['credit_request'])
        credited = sum(_num(x['credited']) or 0 for x in r['matched'])
        summary = [
            ['CORE RETURNS RECONCILIATION', ''],
            ['As of', self.asof.isoformat()],
            ['Dealer code', self.dealer or ''],
            ['Master uploaded', r['inputs'].get('master', '')],
            ['Credit memos', ', '.join(r['inputs'].get('memos', []))],
            ['Shipper scans', ', '.join(r['inputs'].get('shippers', []))],
            ['DealerCONNECT', ', '.join(r['inputs'].get('dealerconnect',
                                                        []))],
            ['', ''],
            ['THIS RUN', ''],
            ['Credits applied (count)', len(r['matched'])],
            ['Credits applied ($)', round(credited, 2)],
            ['Credits already on the master (count)', r['already_applied']],
            ['Partial / over credits (count)', len(r['partial'])],
            ['Credits for tickets not on the master (count)',
             len(r['unmatched_credits'])],
            ['New shipments added from scans (count)',
             len(r['new_shipments'])],
            ['Marked paid from DealerCONNECT (count)',
             len(r['paid_per_mopar'])],
            ['Data fixes (count)', len(r['data_fixes'])],
            ['', ''],
            ['UNPAID AFTER THIS RUN', ''],
            ['Unpaid tickets (count)', len(r['unpaid'])],
            ['Unpaid total ($)', round(tot_unpaid, 2)],
        ]
        for a in r['aging']:
            summary.append([f'  {a["bucket"]} (count / $)',
                            f'{a["count"]} / ${a["amount"]:,.2f}'])
        summary += [
            ['', ''],
            ['CREDIT REQUEST', ''],
            [f'Unpaid {self.o.get("threshold", 60)}+ days, not requested '
             f'before (count)', len(r['credit_request'])],
            ['Credit request total ($)', round(tot_req, 2)],
            ['Previously requested, still unpaid (count)',
             len(r['prev_requested'])],
            ['Tickets needing a re-scan (count)',
             sum(len(g['tickets']) for g in r['rescan'])],
        ]
        if r['memo_totals']:
            summary += [['', ''], ['CREDIT MEMO CHECK', '']]
            for m in r['memo_totals']:
                summary.append([
                    f'{m["memo"]} ({m["date"]})',
                    f'lines ${m["lines_total"]:,.2f} vs memo net '
                    f'${(m["net_total"] or 0):,.2f} — '
                    f'{"ties" if m["ties"] else "DOES NOT TIE"}'])
        if r['warnings']:
            summary += [['', ''], ['WARNINGS', '']]
            summary += [[w, ''] for w in r['warnings']]
        sheets = [('Summary', None, summary, [52, 70], ())]
        sheets.append(('Matched', [
            'Credit Memo #', 'Memo Date', 'Shipment ID', 'Control Ticket',
            'Core Part #', 'Claim #', 'Expected', 'Credited'],
            [[x['memo'], x['memo_date'], x['shipment'], x['ticket'],
              x['part'], x['claim'], x['expected'], x['credited']]
             for x in sorted(r['matched'], key=lambda x: (
                 str(x['shipment'] or ''), x['ticket']))],
            [14, 12, 22, 14, 14, 10, 12, 12], (6, 7)))
        sheets.append(('Partial Credits', [
            'Credit Memo #', 'Memo Date', 'Shipment ID', 'Control Ticket',
            'Core Part #', 'Expected', 'Credited', 'Difference'],
            [[x['memo'], x['memo_date'], x['shipment'], x['ticket'],
              x['part'], x['expected'], x['credited'], x['diff']]
             for x in r['partial']],
            [14, 12, 22, 14, 14, 12, 12, 12], (5, 6, 7)))
        sheets.append(('Uncredited', [
            'Shipment ID', 'Control Ticket', 'Core Part #', 'Claim #',
            'Credit Amount', 'Age (days)', 'Credit Requested'],
            [[x['shipment'], x['ticket'], x['part'], x['claim'],
              x['amount'], x['age'], x['requested']] for x in r['unpaid']],
            [22, 14, 14, 10, 14, 11, 16], (4,)))
        sheets.append(('Unmatched Credits', [
            'Credit Memo #', 'Memo Date', 'Control Ticket', 'Core Part #',
            'Claim #', 'Amount', 'Description'],
            [[x['memo'], x['memo_date'], x['ticket'], x['part'], x['claim'],
              x['amount'], x['description']]
             for x in r['unmatched_credits']],
            [14, 12, 14, 14, 10, 12, 30], (5,)))
        sheets.append(('Credit Request', [
            'GCRS Shipper Date', 'Control Ticket', 'Core Part #', 'Amount',
            'Age (days)', 'Previously Requested', 'Signed Page Found',
            'Page'],
            [[x['date'], x['ticket'], x['part'], x['amount'], x['age'],
              x['previously_requested'], 'Yes' if x['page_found'] else 'NO',
              x['page']] for x in r['credit_request']],
            [18, 14, 14, 12, 11, 20, 17, 44], (3,)))
        if r['prev_requested']:
            sheets.append(('Requested - Still Unpaid', [
                'Shipment ID', 'Control Ticket', 'Core Part #', 'Amount',
                'Age (days)', 'Credit Requested'],
                [[x['shipment'], x['ticket'], x['part'], x['amount'],
                  x['age'], x['requested']] for x in r['prev_requested']],
                [22, 14, 14, 12, 11, 16], (3,)))
        if r['new_shipments']:
            sheets.append(('New Shipments', [
                'Shipment ID', 'Control Ticket', 'Core Part #', 'Claim #',
                'Amount', 'Data From', 'Scan File', 'Page', 'Check'],
                [[x['shipment'], x['ticket'], x['part'], x['claim'],
                  x['amount'], x['source'], x['file'], x['page'],
                  x['flags']] for x in r['new_shipments']],
                [22, 14, 14, 10, 12, 14, 36, 6, 40], (4,)))
        if r['ocr_review']:
            sheets.append(('Scan Review', [
                'Scan File', 'Page', 'Control Ticket', 'Issue'],
                [[x['file'], x['page'], x['ticket'], x['issue']]
                 for x in r['ocr_review']], [36, 6, 14, 90], ()))
        if r['data_fixes'] or r['scan_fills']:
            rows = [[x['ticket'], x['field'], x['old'], x['new'],
                     x['source']] for x in r['data_fixes']]
            rows += [[x['ticket'], x['field'], '', x['value'],
                      f'shipper scan {x["where"]}']
                     for x in r['scan_fills']]
            sheets.append(('Data Changes', [
                'Control Ticket', 'Field', 'Was', 'Now', 'Source'], rows,
                [14, 16, 16, 16, 44], ()))
        if r['dup_credits']:
            sheets.append(('Duplicate Credits', [
                'Credit Memo #', 'Memo Date', 'Control Ticket', 'Amount',
                'Already Credited On'],
                [[x['memo'], x['memo_date'], x['ticket'], x['amount'],
                  x['prior']] for x in r['dup_credits']],
                [14, 12, 14, 12, 30], (3,)))
        if r['other_credits']:
            sheets.append(('No-Ticket Credits', [
                'Credit Memo #', 'Memo Date', 'Core Part #', 'Description',
                'Amount'],
                [[x['memo'], x['memo_date'].isoformat()
                  if x['memo_date'] else '', x['part'], x['description'],
                  money_out(x['amount'])] for x in r['other_credits']],
                [14, 12, 14, 30, 12], (4,)))
        return sheets


def run(inputs, opts, out_dir, progress=None):
    """inputs: master=(path,name)|None, memos/shippers/dc=[(path,name)],
    dc_text=str. Returns the result dict (also written to out_dir)."""
    rec = Reconciler(opts, progress)
    progress = rec.progress
    progress('master', 'Reading the master workbook' if inputs.get('master')
             else 'Starting a new master')
    rec.load_master(*(inputs.get('master') or (None, None)))
    rec.load_dc(inputs.get('dc', []), inputs.get('dc_text', ''))
    if inputs.get('shippers'):
        rec.read_shippers(inputs['shippers'])
    else:
        rec.pages = []
        rec.r['inputs']['shippers'] = []
    if inputs.get('memos'):
        rec.apply_memos(inputs['memos'])
    else:
        rec.r['inputs']['memos'] = []
    progress('dc', 'Comparing with DealerCONNECT')
    rec.apply_dc()
    progress('request', 'Aging unpaid cores and building the credit request')
    rec.build_request()
    master_name = (inputs.get('master') or (None, None))[1]
    rec.write_outputs(out_dir, master_name)
    rec.r['dealer'] = rec.dealer
    rec.r['asof'] = rec.asof.isoformat()
    rec.r['threshold'] = int(opts.get('threshold', 60))
    return rec.r
