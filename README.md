# core-credits-checker
Core return auditor for CDJR parts managers.

Two pages:

| Page | What it does |
|---|---|
| **Quick Check** (`/`) | One GCRS shipper PDF + credit memo PDF(s) → the shipped cores with no credit (unclaimed), oldest shipper date first. |
| **Full Reconciliation** (`/reconcile`) | The weekly core-returns cycle: updates the Core Returns master, builds the Mopar credit request with marked signed shipper pages, the re-scan list, and the master-vs-DealerCONNECT comparison. |

## Full Reconciliation — weekly run

Upload any mix of:

1. **Master workbook** — `Core_Returns_RECONCILED.xlsx` (sheet *Core Returns*: Credit Memo # · Shipment ID · Control Ticket · Core Part # · Claim # · Credit Amount · Age · Credit Requested). Leave empty to start a new one.
2. **Weekly credit memo PDFs** — Mopar Weekly Global Core Return Credit Memo.
3. **Signed GCRS shipper scans** — this week's, plus the ones from ~60 days back so the credit request PDF can include those pages.
4. **DealerCONNECT payment data** (optional) — Global Core Returns › Core Tracking/Inquiry › Payment Inquiry, Paid and/or UnPaid (CSV/XLSX, or paste the rows).

You get back:

| File | Contents |
|---|---|
| `Core_Returns_RECONCILED.xlsx` | Updated master. *Unpaid Cores* is rebuilt from *Core Returns* every run. Rows without a Shipment ID first, then oldest shipment first. |
| `Credit_Request.xlsx` | Dealer Code · GCRS Shipper Date · Core Return Material Ticket # — unpaid tickets at/over the day threshold (default 60) that were not requested before. |
| `Credit_Request.pdf` | The signed shipper page for each requested ticket, with **UnPaid** marked in red in the Part Checkoff column. |
| `Shippers_To_Rescan.xlsx/.pdf` | Requested tickets whose signed page wasn't in the uploaded scans. |
| `Unpaid_Comparison_<date>.xlsx` | Master vs. DealerCONNECT: MOPAR HAS NO RECORD · ADD TO MASTER · FIX DATA · PAID PER MOPAR · AGREE. |
| `Core_Recon_Report_<date>.xlsx` | Summary, Matched, Partial Credits, Uncredited, Unmatched Credits, Credit Request, New Shipments, Scan Review, Data Changes. |

### Rules
- Shippers and credits are matched on the **control ticket** (unique), never the part number.
- A credit memo line writes its CC memo # on the ticket. DealerCONNECT's `#000202022` and memo `CC00202022` are treated as the same memo, so re-running a memo never double-applies it.
- Credit memo lines are added up and checked against the memo's NET CREDIT AMOUNT every run.
- DealerCONNECT is authoritative for part #, amount and warranty claim; the master is authoritative for shipment date.
- Shipper scans are OCR'd (tesseract, 200 DPI) with word positions. Each page's `GCRS SHP#` becomes the Shipment ID. OCR misreads of the leading C (0, 6, ©, €), glued qty/amount and stray check marks are handled. A scanned ticket that's already on the master keeps the master's data. A part number on a new ticket that's one letter off a part you've returned before gets snapped to it and flagged.
- With accounts off nothing is stored. Uploads are deleted when the run ends; result files are deleted after 2 hours.

## Store accounts (sign-in, saved master, history)

Off by default. Set `DATABASE_URL` (Postgres) and the app turns into a
signed-in service:

- **Front page** with a Sign in button. Nothing else is reachable signed out.
- **Sign-in by emailed link.** No passwords. A link works once and expires in
  20 minutes; the browser stays signed in for 30 days. Only invited emails get
  a link.
- **The account is the store.** Everyone on a store sees the same master,
  history and results. One store can never open another store's runs, files
  or master.
- **Roles:** admin (adds/removes people on the Users page) and user (runs
  reconciliations). A person can be on more than one store; they pick the
  store themselves (the page shows who added them) and the app remembers it.
  It never guesses between stores.
- **The app keeps the master.** A run starts from the store's saved master and
  saves the updated one back. Every version is kept; an admin can put an
  earlier one back from History. Only an admin can swap the saved master for
  another workbook, and it has to be a real master (a "Core Returns" tab).
- **A master is held, not made current,** when it comes out with under half
  the saved master's rows, or when the saved master changed while the run was
  working. The run is still saved; an admin makes the held version current
  from History if it was right.
- **History:** every run with who ran it, when, the numbers, and its files.
- **Every action is tied to the store its page was showing.** If another tab
  switched stores since, the click is refused with "reload" instead of landing
  on the wrong store.
- **Owner** (`OWNER_EMAIL`): a Stores page to add a store with its first admin
  and to open any store. Only the owner sets a store's name and dealer code.
- **Sessions are kept on the server.** Sign out, or removal from a person's
  last store, ends them at once.

| Variable | What it is |
|---|---|
| `DATABASE_URL` | Postgres connection string. Unset = accounts off, app runs open as before. |
| `OWNER_EMAIL` | Email(s) of whoever runs the service, comma separated. They sign in like anyone else and get the Stores page. |
| `APP_BASE_URL` | The public address, e.g. `https://partsmanagersolutions.com`. Emailed links point here. |
| `RESEND_API_KEY` | API key for [Resend](https://resend.com), which sends the sign-in emails. |
| `MAIL_FROM` | Sender, e.g. `Parts Manager Solutions <signin@partsmanagersolutions.com>`. The domain must be verified with Resend. |
| `APP_NAME` | Name shown on the front page and in emails. Default `Parts Manager Solutions`. |

The tables are created on first start (`accounts/schema.sql`). The key that
signs the session cookie is generated once and kept in the database.

Not stored: the scans and memos you upload (deleted when a run ends).
Stored per store: the master (every version), each run's result files and
numbers, who is on the store.

## Run locally
```
pip install -r requirements.txt     # plus tesseract-ocr + poppler-utils
gunicorn app:app --timeout 300 --workers 1 --threads 4
```
With accounts: also set `DATABASE_URL`, `OWNER_EMAIL` and `MAIL_BACKEND=log`
(prints sign-in links to the console instead of emailing them; local use only).

Tests: `python -m unittest discover -s tests -v` and `python -m unittest test_part_number`.
The account tests need a scratch Postgres database and are skipped without one:
`TEST_DATABASE_URL=postgresql://user@localhost/scratch python -m unittest discover -s tests -v`
(everything in that database is dropped).

Deployed on Render (Docker) from `main` — see `Dockerfile`.
