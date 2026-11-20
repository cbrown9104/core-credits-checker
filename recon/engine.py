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
from . import ocr as OCR
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


# Characters OCR takes for one another on these pages (both directions).
_LOOKALIKE = set()
for _a, _b in ('S5', 'I1', 'L1', 'O0', 'D0', 'Q0', 'B8', 'Z2', 'T7', 'G6',
               'YU', 'VU', 'RA', 'RE', 'KE', 'KA', 'FP', 'HN', 'CG', 'EF'):
    _LOOKALIKE.update((_a + _b, _b + _a))


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
            'files': [], 'pages_scanned': 0, 'page_checks': [],
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
        self._memos = None          # credit memos, read before the scans
        self.dealer_entered = self.dealer

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
        self._verify_pages()
        self._resolve_lines()
        self._apply_scans()
        self._report_page_checks()

    # ── step 2a: each page against its printed TOTAL ────────────────────
    def _known_ticket(self, ticket):
        k = clean_key(ticket)
        return bool(self.ledger.find(k)) or k in self.credit_tickets or \
            k in self.dc

    def _verify_pages(self):
        """Check every page's rows against its printed "TOTAL <count>
        <amount>", and read again the pages that need it: every page with
        a ticket that is not on the master yet, and every page that does
        not add up on the first read. The reads are compared row by row;
        see _merge_reads."""
        todo = [p for p in self.pages if p['lines']]
        for i, p in enumerate(todo, start=1):
            self.progress('ocr', f'Checking page totals: page {i} of '
                                 f'{len(todo)}')
            try:
                self._verify_page(p)
            except Exception:       # the check must never stop a run
                import traceback
                traceback.print_exc()
                p['check'] = self._page_check(p, self._candidates(p))

    def _candidates(self, p, reads=(), crops=((), ())):
        """Everything the page's TOTAL was read as."""
        c = {'full': collections.Counter(), 'direct': set(), 'loose': set(),
             'counts': collections.Counter(),
             'counts_full': collections.Counter()}
        texts = list(p.get('total_texts') or [])
        for rd in reads:
            texts.extend(rd.get('total_texts') or [])
        for text in texts:
            direct, _ = SH.total_amounts(text)
            if direct:
                c['full'][round(direct[-1], 2)] += 1
                c['direct'].add(round(direct[-1], 2))
            for n in SH.total_counts(text):
                c['counts'][n] += 1
                c['counts_full'][n] += 1
        for text in crops[0]:
            direct, loose = SH.total_amounts(text)
            c['direct'].update(round(v, 2) for v in direct)
            c['loose'].update(round(v, 2) for v in loose)
        for text in crops[1]:
            for n in SH.total_counts(text):
                c['counts'][n] += 1
        return c

    @staticmethod
    def _ties(total, n, cand):
        """Does this row total match a reading of the printed TOTAL? A
        reading with a stray digit dropped only counts when the printed
        line count agrees as well."""
        total = round(total, 2)
        return total in cand['direct'] or (
            total in cand['loose'] and n in cand['counts'])

    def _page_check(self, p, cand, new=False):
        lines = p['lines']
        n = len(lines)
        complete = all(ln.get('amount') is not None for ln in lines)
        total = round(sum(ln.get('amount') or 0 for ln in lines), 2)
        amount_ok = complete and self._ties(total, n, cand)
        if not amount_ok and complete:
            # rows already on the master: its amounts are the checked ones
            alt = 0.0
            for ln in lines:
                rows = self.ledger.find(ln['ticket'])
                a = _num(rows[0].get('amount')) if rows else None
                alt += a if a is not None else (ln.get('amount') or 0)
            if self._ties(alt, n, cand):
                amount_ok, total = True, round(alt, 2)
        full = cand['full'].most_common()
        # a TOTAL amount two full reads of the line agree on
        printed_amt = full[0][0] if full and full[0][1] >= 2 else None
        if printed_amt is not None and printed_amt < total and \
                f'{total:.2f}'.endswith(f'{printed_amt:.2f}'):
            # 995.00 against rows adding to 1,995.00: the pen circle
            # around the count has hidden the front of the number. That
            # is a TOTAL that could not be read, not a page that is off.
            printed_amt = None
        # The count is usually circled by hand, so a close-up of it can
        # read the same wrong digit every time. A close-up may confirm
        # the count; only two full reads of the line agreeing can say it
        # is different.
        counts = cand['counts_full'].most_common()
        count_ok = n in cand['counts']
        printed_cnt = n if count_ok else (
            counts[0][0] if counts and counts[0][1] >= 2 else None)
        count_bad = printed_cnt is not None and printed_cnt != n
        if amount_ok and not count_bad:
            status, note = 'ok', ''
            printed_amt = total
        elif count_bad or (printed_amt is not None and
                           abs(printed_amt - total) > 0.005):
            status = 'mismatch'
            bits = []
            if count_bad:
                bits.append(f'the page TOTAL shows {printed_cnt} lines, '
                            f'{n} were read')
            if printed_amt is not None and abs(printed_amt - total) > 0.005:
                bits.append(f'the page TOTAL shows ${printed_amt:,.2f}, the '
                            f'lines read add up to ${total:,.2f}')
            if not complete:
                bits.append('an amount on the page could not be read')
            note = '; '.join(bits)
            note = note[:1].upper() + note[1:] + '.'
            if (count_bad and printed_cnt > n) or (
                    printed_amt is not None and printed_amt > total):
                note += (' A line the scan missed, or a line crossed out '
                         'by hand (not picked up), would explain it.')
            if amount_ok:
                printed_amt = total
        else:
            status = 'unread'
            seen = sorted(cand['direct'])
            note = 'The printed TOTAL could not be read well enough to ' \
                   'check this page' + (
                       ' (read as ' + ' / '.join(f'${v:,.2f}' for v in
                                                 seen[:4]) + ')'
                       if seen else '') + '.'
            if count_ok:
                note += f' The line count ({n}) matches.'
            if not complete:
                note += ' An amount on the page could not be read.'
        return {'file': p['file'], 'page': p['page'],
                'shipment': p.get('shipment_id'), 'lines': n,
                'amount': total, 'printed_count': printed_cnt,
                'printed_amount': printed_amt, 'status': status,
                'note': note, 'new': new, 'changes': p.get('changes', [])}

    def _verify_page(self, p):
        new = any(not self.ledger.find(ln['ticket']) for ln in p['lines'])
        cand = self._candidates(p)
        check = self._page_check(p, cand, new)
        if check['status'] == 'ok' and not new:
            p['check'] = check
            return
        im = OCR.page_image(p['path'], p['page'], p.get('dpi') or
                            OCR.OCR_DPI)
        crops = SH.total_crops(im, p)
        cand = self._candidates(p, (), crops)
        check = self._page_check(p, cand, new)
        if check['status'] != 'ok' or new:
            reads = SH.reread_rows(im, p)
            cand = self._candidates(p, reads, crops)
            self._merge_reads(p, reads, cand)
            new = any(not self.ledger.find(ln['ticket'])
                      for ln in p['lines'])
            check = self._page_check(p, cand, new)
        p['check'] = check

    @staticmethod
    def _yc(row):
        b = row['line_box']
        return (b[1] + b[3]) / 2.0

    def _vote_part(self, ln, reads):
        """Settle a row's part number from its reads: a part this store
        has returned before wins; otherwise what most reads agree on.
        Reads that disagree, with nothing on record to settle it, are
        flagged for a look at the page."""
        raw = [r['part'] for r in reads if r.get('part')]
        if not raw:
            return
        first = ln.get('part') or ''
        known = [k for k in (self.parts.fix(x)[0] for x in raw)
                 if k in self.parts.count]
        seen = list(dict.fromkeys(raw))
        flag = None
        if known:
            top = collections.Counter(known).most_common()
            pick = top[0][0]
            if len(top) > 1:
                flag = ('part # read as ' + ' / '.join(seen) + ' — more '
                        'than one part on record fits, check the page')
            elif pick != first:
                flag = (f'part read as {first}, matched to {pick} (a part '
                        f'already on record)')
            if len(raw) >= 3 and len(set(raw)) == 1 and \
                    part_looks_valid(raw[0]) and raw[0] != pick and \
                    len(raw[0]) == len(pick) and not any(
                        a + b in _LOOKALIKE for a, b in zip(raw[0], pick)
                        if a != b):
                # Every read shows the same part, and where it differs
                # from the part on record the characters do not look
                # alike (53010749AA against 53010748AA). Three reads do
                # not all mistake a 9 for an 8: it is a different part,
                # not a misread.
                flag = (f'part read as {raw[0]} on every read; {pick} is '
                        f'the nearest part on record — kept as read, '
                        f'check the page')
                pick = raw[0]
        else:
            valid = [x for x in raw if part_looks_valid(x)]
            top = collections.Counter(valid).most_common()
            if top and top[0][1] >= 2 and (len(top) == 1 or
                                           top[0][1] > top[1][1]):
                pick = top[0][0]
            else:
                pick = first if part_looks_valid(first) or not valid \
                    else valid[0]
            if len(set(raw)) > 1:
                flag = ('part # read differently on re-reads (' +
                        ' / '.join(seen) + ') and is not a part on record '
                        '— check the page')
        if pick != first:
            ln['part'] = pick
            ln['part_ok'] = part_looks_valid(pick)
        ln['part_settled'] = True
        if flag:
            ln.setdefault('flags', []).append(flag)

    def _merge_reads(self, p, reads, cand):
        """Compare the first read of a page with its re-reads, row by row
        (rows are lined up by where they sit on the page).

        - Part numbers: see _vote_part.
        - Amounts: where the reads differ, the set of amounts that makes
          the page add up to its printed TOTAL is used; failing that, the
          amount most reads agree on.
        - A row the first read missed is added when two reads have it,
          when its ticket is on the master, DealerCONNECT or this run's
          credit memos, or when the page only adds up with it.
        Everything changed here is flagged on the row."""
        met = SH._row_metrics(p)
        if not met or not reads:
            return
        pitch = met[2]
        groups = [{'ln': ln, 'y': self._yc(ln), 'reads': [ln]}
                  for ln in p['lines']]
        extras = []
        for rd in reads:
            for row in rd['rows']:
                y = self._yc(row)
                g = min(groups, key=lambda g: abs(g['y'] - y))
                if abs(g['y'] - y) <= pitch * 0.45:
                    g['reads'].append(row)
                    continue
                e = min(extras, key=lambda e: abs(e['y'] - y), default=None)
                if e is not None and abs(e['y'] - y) <= pitch * 0.45:
                    e['reads'].append(row)
                else:
                    extras.append({'y': y, 'reads': [row]})
        for g in groups:
            self._vote_part(g['ln'], g['reads'])
            g['opts'] = self._amount_options(g['reads'])
            alt_claims = [r['claim'] for r in g['reads'][1:] if r.get('claim')]
            if g['ln'].get('claim') is None and len(alt_claims) >= 2 and \
                    len(set(alt_claims)) == 1:
                g['ln']['claim'] = alt_claims[0]
        # rows only a re-read has
        maybe = []
        for e in extras:
            tickets = collections.Counter(
                r['ticket'] for r in e['reads'] if is_valid_ticket(r['ticket']))
            if not tickets:
                continue
            t = tickets.most_common(1)[0][0]
            if any(clean_key(ln['ticket']) == clean_key(t)
                   for ln in p['lines']):
                continue
            row = next(r for r in e['reads'] if r['ticket'] == t)
            if not self._known_ticket(t) and not any(
                    r.get('amount') is not None and
                    part_looks_valid(r.get('part') or '')
                    for r in e['reads']):
                continue        # no part and amount with it: not a row
            self._vote_part(row, e['reads'])
            e.update(ln=row, opts=self._amount_options(e['reads']),
                     sure=len(e['reads']) >= 2 or self._known_ticket(t))
            maybe.append(e)
        sure = [e for e in maybe if e['sure']]
        unsure = [e for e in maybe if not e['sure']]
        picked = self._fit_to_total(groups + sure, unsure, cand)
        changes = p.setdefault('changes', [])
        used = groups + sure + [e for e in unsure if e.get('use')]
        for g in used:
            ln = g['ln']
            first = ln.get('amount')
            amount = g.get('pick', g['opts'][0] if g['opts'] else None)
            if g not in groups:
                ln.setdefault('flags', []).append(
                    'this line was missed on the first read of the page '
                    'and picked up on a re-read — check the page')
                ln['added_on_reread'] = True
                changes.append(f'{ln["ticket"]}: line added from a re-read')
            if amount is None and g.get('fill') is not None:
                amount = g['fill']
                ln.setdefault('flags', []).append(
                    f'amount unreadable; ${amount:,.2f} is what the page '
                    f'TOTAL leaves for this line — check the page')
                changes.append(f'{ln["ticket"]}: amount taken from the page '
                               f'TOTAL (${amount:,.2f})')
            elif g in groups and amount is not None and first is not None \
                    and abs(amount - first) > 0.005:
                why = 'so the page adds up to its printed TOTAL' \
                    if picked else 'the amount the re-reads agree on'
                ln.setdefault('flags', []).append(
                    f'amount first read as ${first:,.2f}; ${amount:,.2f} '
                    f'used ({why}) — check the page')
                changes.append(f'{ln["ticket"]}: amount ${first:,.2f} → '
                               f'${amount:,.2f}')
            elif g in groups and len(g['opts']) > 1 and not picked:
                ln.setdefault('flags', []).append(
                    'amount read differently on re-reads (' + ' / '.join(
                        f'${v:,.2f}' for v in g['opts']) + ') — check the '
                    'page')
            ln['amount'] = amount
        if len(used) > len(groups):
            p['lines'] = [g['ln'] for g in
                          sorted(used, key=lambda g: g['y'])]
        for e in unsure:
            if not e.get('use'):
                self.r['ocr_review'].append({
                    'file': p['file'], 'page': p['page'],
                    'ticket': e['ln']['ticket'],
                    'issue': f'One re-read of this page shows a line for '
                             f'{e["ln"]["ticket"]} that the other reads do '
                             f'not have. It was not added. Check the page.'})

    @staticmethod
    def _amount_options(reads):
        """A row's amounts over its reads, most agreed-on first (the first
        read wins a tie)."""
        vals = [r['amount'] for r in reads if r.get('amount') is not None]
        count = collections.Counter(vals)
        return sorted(count, key=lambda v: (-count[v], vals.index(v)))

    def _fit_to_total(self, rows, optional, cand):
        """Pick, for each row, the amount that makes the page add up to
        its printed TOTAL. rows are on the page for certain; optional are
        rows only one re-read has. Sets g['pick'] (and g['use'] on an
        optional row, g['fill'] on the one row with no amount at all).
        Returns True when the page was made to add up; with no way, or
        more than one way, nothing is forced and each row keeps the
        amount most reads agree on."""
        def total(choice, extra):
            vals = [g['opts'][i] for g, i in zip(rows, choice) if g['opts']]
            vals += [e['opts'][0] for e in extra if e['opts']]
            return round(sum(vals), 2)

        blanks = [g for g in rows if not g['opts']]
        base = [0] * len(rows)
        if not blanks and self._ties(total(base, ()), len(rows), cand):
            return True
        # the fewest changes from the agreed amounts that tie: swap one
        # row's amount, add one optional row, or both
        swaps = [(i, j) for i, g in enumerate(rows)
                 for j in range(1, len(g['opts']))]
        tries = []
        if not blanks:
            for i, j in swaps:
                tries.append(([(i, j)], ()))
            for e in optional:
                if e['opts']:
                    tries.append(([], (e,)))
            for a in range(len(swaps)):
                for b in range(a + 1, len(swaps)):
                    if swaps[a][0] != swaps[b][0] and len(tries) < 4000:
                        tries.append(([swaps[a], swaps[b]], ()))
            for i, j in swaps:
                for e in optional:
                    if e['opts']:
                        tries.append(([(i, j)], (e,)))
        hits = []
        for level in (1, 2):
            for sw, extra in tries:
                if len(sw) + len(extra) != level:
                    continue
                choice = list(base)
                for i, j in sw:
                    choice[i] = j
                if self._ties(total(choice, extra), len(rows) + len(extra),
                              cand):
                    hits.append((choice, extra))
            if hits:
                break
        if len(hits) == 1:
            choice, extra = hits[0]
            for g, i in zip(rows, choice):
                g['pick'] = g['opts'][i]
            for e in extra:
                e['use'] = True
            return True
        if len(blanks) == 1 and not hits:
            # one row with no readable amount: the printed TOTAL, when two
            # full reads of that line agree on it, says what is left
            full = cand['full'].most_common(1)
            if full and full[0][1] >= 2:
                left = round(full[0][0] - total(base, ()), 2)
                if left > 0:
                    blanks[0]['fill'] = left
                    return True
        return False

    def _report_page_checks(self):
        """Put each page's TOTAL check in the results, and say so up top
        for pages that added tickets this run and do not check out."""
        checks = [p['check'] for p in self.pages if p.get('check')]
        self.r['page_checks'] = checks
        bad = [c for c in checks if c['status'] == 'mismatch']
        for c in bad:
            self.r['ocr_review'].append({
                'file': c['file'], 'page': c['page'], 'ticket': '',
                'issue': 'Page does not match its printed TOTAL. ' +
                         c['note'] + ' Check the page against the master.'})
        loud = [c for c in bad if c['new']]
        if loud:
            self.r['warnings'].append(
                f'{len(loud)} shipper page(s) that added tickets do not '
                f'match their printed TOTAL: ' + '; '.join(
                    f'{c["file"]} p.{c["page"]} ({c["note"].rstrip(".")})'
                    for c in loud[:6]) +
                ('; ...' if len(loud) > 6 else '') +
                '. Check those pages against the master before sending '
                'the credit request.')
        unread = [c for c in checks if c['status'] == 'unread' and c['new']]
        if unread:
            self.r['warnings'].append(
                f'{len(unread)} shipper page(s) that added tickets could '
                f'not be checked against their printed TOTAL (it could not '
                f'be read): ' + ', '.join(
                    f'{c["file"]} p.{c["page"]}' for c in unread[:8]) +
                ('...' if len(unread) > 8 else '') +
                '. They are listed on the Page Totals sheet of the report.')

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
                if not part and ln.get('part_settled'):
                    part = ln['part']       # settled over several reads
                elif not part:
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
                flags.extend(f for f in ln.get('flags', [])
                             if f not in flags)
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
    def read_memos(self, files):
        """Read the credit memos (nothing is applied yet). Done before
        the shipper scans so the scan step knows which tickets this run's
        memos credit."""
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
        self._memos = (names, parsed)

    def apply_memos(self, files=None):
        if self._memos is None:
            self.read_memos(files or [])
        names, parsed = self._memos
        self.memo_dealers = [res['dealer'] for _, res in parsed
                             if res.get('dealer')]
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
        self._place_memo_only()

    @staticmethod
    def _series(ticket):
        """('CB', 846220) for CB00846220; ('C', 456393855) for a GCRS
        ticket; None for anything else."""
        m = re.match(r'^(C[ABW]?)(\d+)$', clean_key(ticket))
        return (m.group(1), int(m.group(2))) if m else None

    def _place_memo_only(self):
        """A credit for a ticket that is not on the master is added with
        no Shipment ID. When an uploaded shipper page is short (by its
        printed TOTAL) and holds tickets in the same number range, that
        page is very likely where the scan dropped the ticket: say so,
        and fill the Shipment ID when the page is short by exactly this
        credit."""
        pages = [p for p in getattr(self, 'pages', [])
                 if p.get('check') and p.get('shipment') and p['lines']]
        if not pages:
            return
        loose = []
        for u in self.r['unmatched_credits']:
            ser = self._series(u['ticket'])
            rows = self.ledger.find(u['ticket'])
            if not ser or not rows or rows[0].get('shipment'):
                continue
            hits = []
            for p in pages:
                c = p['check']
                if c['status'] == 'ok':
                    continue
                nums = [self._series(ln['ticket']) for ln in p['lines']]
                nums = [n[1] for n in nums if n and n[0] == ser[0]]
                if not nums:
                    continue
                reach = max(500, max(nums) - min(nums))
                if not min(nums) - reach <= ser[1] <= max(nums) + reach:
                    continue
                short_n = c['printed_count'] - c['lines'] \
                    if c['printed_count'] is not None else None
                short_amt = round(c['printed_amount'] - c['amount'], 2) \
                    if c['printed_amount'] is not None else None
                if (short_n is not None and short_n < 1) or \
                        (short_amt is not None and short_amt <= 0):
                    continue
                exact = short_n == 1 and _same_amount(short_amt,
                                                      u['amount'])
                hits.append((p, c, exact))
            if not hits:
                continue
            exact = [h for h in hits if h[2]]
            if len(exact) == 1:
                p, c, _ = exact[0]
                rows[0]['shipment'] = p['shipment']
                u['shipment'] = p['shipment']
                issue = (f'Credit memo {u["memo"]} paid {u["ticket"]}, '
                         f'which was not on the master. This page is one '
                         f'line and ${_num(u["amount"]) or 0:,.2f} short of '
                         f'its printed TOTAL and holds tickets in the same '
                         f'number range, so the ticket was given this '
                         f'page\'s Shipment ID ({p["shipment"]}). Check '
                         f'the page.')
                self.r['data_fixes'].append({
                    'ticket': u['ticket'], 'field': 'Shipment ID',
                    'old': None, 'new': p['shipment'],
                    'source': f'{p["file"]} p.{p["page"]} (short of its '
                              f'printed TOTAL by this ticket)'})
            else:
                p, c, _ = hits[0]
                where = ', '.join(f'{q["file"]} p.{q["page"]} (shipment '
                                  f'{q["shipment"]})' for q, _, _ in hits[:3])
                issue = (f'Credit memo {u["memo"]} paid {u["ticket"]}, '
                         f'which was not on the master, so it has no '
                         f'Shipment ID. It may have been dropped from a '
                         f'scan: {where} did not check out against the '
                         f'printed TOTAL and holds tickets in the same '
                         f'number range. If the ticket is on that page, '
                         f'put its Shipment ID on the master.')
                loose.append(f'{u["ticket"]} ({p["file"]} p.{p["page"]})')
            self.r['ocr_review'].append({
                'file': p['file'], 'page': p['page'], 'ticket': u['ticket'],
                'issue': issue})
        if loose:
            self.r['warnings'].append(
                f'{len(loose)} credited ticket(s) were not on the master '
                f'and have no Shipment ID. Each may have been dropped from '
                f'a scan: a page in the same number range did not check '
                f'out against its printed TOTAL. ' + ', '.join(loose[:8]) +
                ('...' if len(loose) > 8 else '') + '. If the ticket is on '
                'that page, put the page\'s Shipment ID on the master '
                '(details under Scan review).')

    def check_dealer(self):
        """The dealer code is printed on every shipper page (DEALER:
        45558000) and on the memos. When the code entered for the run (the
        store's saved code) is not the one the documents print, the
        documents win and the difference is reported."""
        seen = collections.Counter(
            p['dealer'] for p in getattr(self, 'pages', [])
            if p.get('dealer'))
        for d in getattr(self, 'memo_dealers', []):
            seen[str(d)] += 1
        self.r['dealer_entered'] = self.dealer_entered or ''
        if not seen:
            return
        top = seen.most_common(2)
        code, n = top[0]
        clear = n >= 2 and (len(top) == 1 or n >= 3 * top[1][1])
        self.r['dealer_on_documents'] = code if clear else ''
        if not self.dealer_entered:
            if clear:
                self.dealer = code
            return
        if clear and code != self.dealer_entered:
            self.dealer = code
            self.r['warnings'].insert(0, (
                f'Dealer code: this store is set up as '
                f'{self.dealer_entered}, but the documents in this run '
                f'print {code} ({n} of {sum(seen.values())} pages and '
                f'memos read). {code} was used on the credit request. '
                f'Have the site owner correct the store\'s dealer code '
                f'(Users page, Store details).'))

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
            ['Shipper pages checked against their printed TOTAL (count)',
             len(r['page_checks'])],
            ['  Pages that match / do not match / TOTAL not read',
             ' / '.join(str(sum(1 for c in r['page_checks']
                                if c['status'] == k))
                        for k in ('ok', 'mismatch', 'unread'))],
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
        if r['page_checks']:
            label = {'ok': 'Matches', 'mismatch': 'DOES NOT MATCH',
                     'unread': 'TOTAL not read'}
            sheets.append(('Page Totals', [
                'Scan File', 'Page', 'Shipment ID', 'Lines Read',
                'Amount Read', 'Printed Count', 'Printed Amount', 'Result',
                'Added Tickets', 'Note'],
                [[c['file'], c['page'], c['shipment'], c['lines'],
                  c['amount'], c['printed_count'], c['printed_amount'],
                  label[c['status']], 'Yes' if c['new'] else '',
                  ' '.join([c['note']] + (['Changed on re-read: ' +
                                           '; '.join(c['changes']) + '.']
                                          if c['changes'] else [])).strip()]
                 for c in sorted(r['page_checks'], key=lambda c: (
                     {'mismatch': 0, 'unread': 1, 'ok': 2}[c['status']],
                     str(c['shipment'] or ''), c['file'], c['page']))],
                [36, 6, 22, 11, 13, 13, 14, 16, 13, 90], (4, 6)))
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
    if inputs.get('memos'):
        rec.read_memos(inputs['memos'])
    if inputs.get('shippers'):
        rec.read_shippers(inputs['shippers'])
    else:
        rec.pages = []
        rec.r['inputs']['shippers'] = []
    if inputs.get('memos'):
        rec.apply_memos()
    else:
        rec.r['inputs']['memos'] = []
    rec.check_dealer()
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
