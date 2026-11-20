# core-credits-checker
Core return auditor for CDJR parts managers.

Four tools:

| Page | What it does |
|---|---|
| **Quick Check** (`/`) | One GCRS shipper PDF + credit memo PDF(s) → the shipped cores with no credit (unclaimed), oldest shipper date first. |
| **Full Reconciliation** (`/reconcile`) | The weekly core-returns cycle: updates the Core Returns master, builds the Mopar credit request with marked signed shipper pages, the re-scan list, and the master-vs-DealerCONNECT comparison. |
| **Unpaid Cores** (`/cores`, store accounts only) | The unpaid tickets of the saved master as a list to work: what is ready for a credit request, what was requested and is waiting, what is not due yet, with a status and a note per ticket. |
| **Shortage Check** (`/shortages`, store accounts only) | Mopar parts invoices against the receipts in the DMS: what was billed and not received, with a claim deadline, status, claim number and note per line. |

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
| `Core_Recon_Report_<date>.xlsx` | Summary, Matched, Partial Credits, Uncredited, Unmatched Credits, Credit Request, New Shipments, Page Totals, Scan Review, Data Changes. |

### Rules
- Shippers and credits are matched on the **control ticket** (unique), never the part number.
- A credit memo line writes its CC memo # on the ticket. DealerCONNECT's `#000202022` and memo `CC00202022` are treated as the same memo, so re-running a memo never double-applies it.
- Credit memo lines are added up and checked against the memo's NET CREDIT AMOUNT every run.
- DealerCONNECT is authoritative for part #, amount and warranty claim; the master is authoritative for shipment date.
- Shipper scans are OCR'd (tesseract, 200 DPI) with word positions. Each page's `GCRS SHP#` becomes the Shipment ID. OCR misreads of the leading C (0, 6, ©, €), glued qty/amount and stray check marks are handled. A scanned ticket that's already on the master keeps the master's data. A part number on a new ticket that's one letter off a part you've returned before gets snapped to it and flagged.
- **Every shipper page is checked against its printed `TOTAL <count> <amount>`.** A page that adds tickets, or that does not add up on the first read, is read twice more (enlarged and smoothed two ways) and the reads are compared row by row: a part this store has returned before wins, otherwise what most reads agree on; an amount is taken from the reads so the page adds up to its TOTAL; a row the first read missed is added when two reads have it, when the ticket is on the master / DealerCONNECT / this run's memos, or when the page only adds up with it. Everything changed is flagged on the row (New Shipments › Check). Each page ends up *Matches*, *Does not match* or *TOTAL not read* (the count is usually circled by hand, which can hide the number) on the **Page Totals** sheet and on the results page; pages that added tickets and do not match are also listed under "Check these". A crossed-out line shows up as a page that is short of its TOTAL.
- Only the table is read: rows start under the column headings and stop at the TOTAL line, the PICKUP VERIFICATION block or a gap of several lines, so a driver's scan receipt or hand-written ticket numbers elsewhere on the page are not taken for shipper lines.
- A credit for a ticket that is not on the master is added with no Shipment ID. When an uploaded page is short of its TOTAL and holds tickets in the same number range, the run says so, and fills the Shipment ID when the page is short by exactly that one line and amount.
- The dealer code printed on the documents (`DEALER: 45558000`) wins over the one entered for the run; a difference is reported.
- The master is written with its cell formatting: Arial 10, thin gray grid, rows with no credit memo yellow, credited rows banded white / gray, A–B left, C–E centered, F right as dollars, G–H centered. *Unpaid Cores* is plain (grid only).
- "Write today's date in Credit Requested" is off unless checked: the date belongs on the master once the request email has gone to Mopar. With store accounts the date can be written afterwards on Unpaid Cores.
- With accounts off nothing is stored. Uploads are deleted when the run ends; result files are deleted after 2 hours.

## Unpaid Cores — the saved master as a list to work

Needs store accounts. Nothing is uploaded: the page reads the store's saved master each time it opens.

- **Where a ticket stands.** Credited (it has a credit memo), Requested (it has a Credit Requested date), Ready to request (its shipment date is the store's threshold of days back, 60 until an admin changes it, the same count the run uses), Not due yet (with the day it becomes ready), or No ship date.
- **Status and note.** Review, Denied or Written off, and a note of up to 500 characters, per ticket. They are kept in the database by store and control ticket (`core_notes`), not in the workbook, so a run or a replaced master does not lose them. A ticket marked Written off leaves the three lists of work to do and stays on the master.
- **Mark requested.** Writes the date a credit request was sent in Credit Requested on the picked unpaid tickets, or clears it. The master is saved as a new version (History shows it as "Request dates"), written the way a run writes it; the version before it stays under Master versions. It is refused while a run is working for the store, and when the saved master changed after the page was loaded.
- **Credited tickets** are fetched only when the Credited filter is turned on.
- **Excel.** `Unpaid_Cores_<date>.xlsx`: the unpaid tickets with age, where each stands, the day it is due for a request, status and note.

## Shortage Check — billed against received

Needs store accounts (it keeps its work in the database). Package `shortage/`.

- **Files.** One drop box takes the weekly Mopar parts invoices (W and WA, PDFs with a text layer, saved from DealerCONNECT) and the Reynolds parts transaction file from Dynamic Reporting (CSV or Excel). Each file is answered with what became of it: loaded, already loaded, loaded but not complete, or not loaded and why.
- **Invoice.** A run of shipper blocks (`0999900-5000002 ORD#: EA1030 O/T:E DATE: 2026-10-30`: PDC, shipper, order, order type, order date), each with part lines, charges that are not parts (transportation, deposits, a chargeback) and a SUBTOTAL. The last page has SUMMARY. An invoice is complete when its part lines after discount plus its charges equal NET INVOICE AMOUNT. A copy with no SUMMARY page, or one that does not add up, is marked not complete. A fuller copy replaces a cut-off one and keeps the statuses, claim numbers and notes.
- **Receipts.** The rows with `Trx_Type` PUR. `Invoice` holds the order number, `Shipping_Number` the shipper. `RECID` is the key, so the same file can be loaded every day. The DMS make code (`CH68524020-AB`) and leading zeros are taken off before part numbers are compared.
- **Matching.** Same part and same shipper, then same part and same order, then on the same shipper or order the part number the invoice says was superseded, then a part that differs only in its last letter. A receipt is used once. An order number that is only close is never counted (daily order numbers end in the date); it shows as a hint. Core charges (U lines), lines billed at $0.00 and parts on the store's ignored list are not counted. A shortage is valued at what was billed for it after discount.
- **Deadline.** Invoice date plus the store's days: 14, and 7 for the facing PDC, until an admin changes them. The numbers are the store's to set.
- **Lines.** Filters with live totals, status (Review, Filed, Paid, Denied, Found), claim number, note, bulk changes, Excel export. A claim number on a line with no status files it.

## Store accounts (sign-in, saved master, history)

Off by default. Set `DATABASE_URL` (Postgres) and the app turns into a
signed-in service:

- **Front page** with Sign in and Sign up your store. Nothing else is
  reachable signed out.
  Signed in, the site's address opens Full Reconciliation (the weekly run);
  Quick Check is at `/quick`.
- **Sign-in by emailed link.** No passwords. A link works once and expires in
  20 minutes; the browser stays signed in for 30 days. Only a person on a
  store gets a sign-in link.
- **Sign-up.** A store signs itself up at `/signup` with its name, its dealer
  code and a work email. The emailed link (72 hours) makes the store when the
  person uses it, with that person as its admin. No store is made before that,
  and the email carries nothing that the person typed. The owner gets an email
  for each new store. At most 50 sign-up emails go out in a day.
- **Approval.** A store that signed itself up is marked Waiting. Nobody can
  work in it, and it sends no invitations, until the owner approves it on the
  Stores page. Its admin then gets an email.
- **One store for a dealer code.** A sign-up for a dealer code that already
  has a store makes nothing. The page answers the same as for a new store, and
  the email tells the person to ask the admin of their store. The page thus
  does not show which dealer codes are customers.
- **Delete a store.** The owner deletes a store on the Stores page. The page
  shows what goes (people, runs, master, files) and asks for the name of the
  store, typed. Nothing brings a deleted store back.
- **Terms and Privacy** pages at `/terms` and `/privacy`, linked at the foot of
  every page and on the sign-up form.
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
  An admin can also replace the saved master without a run, from History
  ("Replace the saved master"): the workbook is saved as it is, as a new
  version, and the next run starts from it. Marking tickets as requested on
  Unpaid Cores saves a new version too.
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
| `APP_BASE_URL` | The public address, e.g. `https://partsmanagersolutions.com`. Emailed links point here. A request at any other address of the site is sent here. |
| `RESEND_API_KEY` | API key for [Resend](https://resend.com), which sends the sign-in emails. |
| `MAIL_FROM` | Sender, e.g. `Parts Manager Solutions <signin@partsmanagersolutions.com>`. The domain must be verified with Resend. |
| `MAIL_REPLY_TO` | Optional. Where a reply to a sign-in email goes. |
| `SUPPORT_EMAIL` | Optional. Shown at the foot of every page as where to write for help. |
| `APP_NAME` | Name shown on the front page and in emails. Default `Parts Manager Solutions`. |
| `SIGNUPS` | Optional. `off` closes the sign-up page. The owner then adds each store on the Stores page. |
| `SIGNUP_APPROVAL` | Optional. `off` opens a store that signs itself up at once, without the owner's approval. |

The tables are created on first start (`accounts/schema.sql`) and brought up
to date whenever that file changes. The key that signs the session cookie is
generated once and kept in the database.

**Start-up check.** `python -m accounts.preflight` runs before the web server
(see `Dockerfile`). With accounts off it does nothing. With accounts on, the
server does not start unless the database answers, and on a database nobody
has signed in on yet, unless sign-in can work (`OWNER_EMAIL`, `RESEND_API_KEY`
and `APP_BASE_URL` set). A wrong setting therefore fails the deploy with a
plain reason in the log and the previous version keeps running. The password
in `DATABASE_URL` is never printed.

**If the database goes away** while the site is up, pages answer "Back in a
minute" (HTTP 503) within seconds instead of hanging, `/healthz` keeps
answering, and everything recovers by itself when the database is back. A run
that finishes during an outage keeps its files downloadable for 2 hours and
says it could not be saved.

Not stored: the scans and memos you upload (deleted when a run ends).
Stored per store: the master (every version), each run's result files and
numbers, who is on the store, the status and note typed on a core ticket.
Result files are stored in 2 MB pieces, so a
large scan-heavy PDF never needs much database memory at once; files over
40 MB are not kept. The owner's Stores page shows how much is stored.

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
