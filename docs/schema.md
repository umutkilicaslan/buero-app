# Database schema

SQLite, file `buero.sqlite3`. Money is stored as decimal text (e.g. `"362.50"`), never as float.

## settings (one row, id = 1)
Your business data: name, business_name, street, postal_code, city, email, phone, profession,
tax_number (Steuernummer), vat_id (USt-IdNr, empty = none), kleinunternehmer (bool),
business_type (freiberufler | gewerbe), bank_name, iban, bic, invoice_language (de | en),
number_digits (2 → 2026-01, 3 → 2026-001), payment_terms_days.

## clients
id, name, contact_person, street, postal_code, city, country (ISO code, e.g. DE, AT, US),
is_business, vat_id, email, default_hourly_rate,
exemption ("" | "4_25" | "4_21"), exemption_evidence (text), notes, created_at.

## invoices
| Column | Meaning |
|---|---|
| id | internal id |
| kind | `invoice` or `storno` |
| status | `draft` → `issued` → `paid`; or `cancelled` (after a Storno) |
| number, year, seq | e.g. `2026-07`, 2026, 7. Empty while draft. Unique. |
| client_id | → clients |
| cancels_invoice_id | for a Storno: → the cancelled invoice |
| language | de / en |
| issue_date, service_from, service_to, due_date | dates; service_to empty = single day |
| intro_text | optional text above the items |
| vat_override | "" = automatic, else a forced treatment |
| vat_treatment, vat_rate, vat_note | frozen at issue |
| seller_snapshot, client_snapshot | JSON copy of both addresses/tax ids at issue |
| net_total, vat_total, gross_total | decimal |
| pdf_path, pdf_sha256 | archived PDF and its hash, written once |
| paid_date, paid_amount | summary of the rows in `payments`, may change after issue |
| created_at, issued_at | timestamps |

Settings also hold: ist_versteuerung, ustva_period (monthly | quarterly), dauerfristverlaengerung,
church_tax, gewerbesteuer_hebesatz, einvoice (embed ZUGFeRD/Factur-X), backup_dir, reminder_fee,
ai_enabled, ai_url, ai_model (local Ollama assistant; only addresses on this computer are accepted).
Clients also hold leitweg_id (for XRechnung to public bodies).

## invoice_lines
id, invoice_id, position, description, quantity, unit, unit_price, line_net (= quantity × unit_price, rounded to cents).
VAT is calculated once on the invoice net total.

## number_sequences
year (primary key), next_number. A number is taken only when an invoice is issued, so deleting a
draft never leaves a gap. `next_number` can be set once per year before the first issue, to continue
from another system.

## payments
id, invoice_id, date, amount, transaction_id (→ transactions, empty = entered by hand).
Part payments are allowed; the invoice is `paid` when the sum reaches the gross total.

## reminders (Mahnungen, immutable)
id, invoice_id, level (1–3), date, new_due_date, fee, pdf_path, pdf_sha256.

## vat_years
year, kleinunternehmer. Your VAT status per year, so changing today's setting never changes old reports.

## expenses
id, date (payment date), vendor, description, vendor_invoice_number, category (see `catalog.py`),
payment_method, currency, amount_original, gross_eur, vat_rate, vat_eur, business_share (0–1),
reverse_charge ("" | "eu" | "other", § 13b), useful_life_years (assets), transaction_id, source
(manual | bank | einvoice), voided, void_reason, created_at. Never deleted, only voided.

## receipts (immutable)
id, expense_id, original_name, path, sha256, size, uploaded_at. The file itself is stored unchanged.

## other_income
id, date, client_id, description, gross_eur, vat_treatment, vat_eur, transaction_id, voided, created_at.

## bank_imports / transactions
bank_imports: id, filename, sha256, profile (detected column mapping), rows_total, rows_new, imported_at.
The raw CSV is kept in `bank-imports/`.

transactions: id, import_id, booking_date, amount (+ in / − out), currency, counterparty, iban, purpose,
reference, dedupe_key (unique), status (open | invoice | income | expense | private | tax | transfer | ignored),
tax_kind (est | ust | other), tax_year, note. Date, amount, text and key cannot be changed after import.

## tax_years
year, homeoffice_days, sonderausgaben (health/care insurance, pension), prepayments_other.

## audit_log (append-only)
id, ts, entity (`invoice`, `client`, `settings`, `expense`, `transaction`, …), entity_id, action, details (JSON with before/after).

## Triggers (GoBD)
- `invoices_no_delete`: no DELETE of a non-draft invoice.
- `invoices_frozen`: no change to number, dates, amounts, VAT, snapshots, client etc. once issued, and no way back to draft.
- `invoices_pdf_once`: the archived PDF hash cannot be replaced.
- `lines_*_draft_only`: invoice lines can only change while the invoice is a draft.
- `receipts_no_update`, `receipts_no_delete`, `reminders_frozen`, `reminders_no_delete`.
- `expenses_no_delete`, `transactions_no_delete`, `transactions_core_frozen`.
- `audit_no_update`, `audit_no_delete`: the audit log can only grow.
