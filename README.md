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
- Nothing is stored. Uploads are deleted when the run ends; result files are deleted after 2 hours.

## Run locally
```
pip install -r requirements.txt     # plus tesseract-ocr + poppler-utils
gunicorn app:app --timeout 300 --workers 1 --threads 4
```
Tests: `python -m unittest discover -s tests -v` and `python -m unittest test_part_number`.

Deployed on Render (Docker) from `main` — see `Dockerfile`.
