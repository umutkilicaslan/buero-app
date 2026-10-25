# Büro – local invoicing, bookkeeping and tax checks

A small web app that runs only on your Mac. No cloud, no account, no external APIs.
Your data stays in one folder. Nothing is ever sent to ELSTER: the app shows the numbers,
you type them into Mein ELSTER yourself.

What it does:

- **Invoices** with all § 14 Abs. 4 UStG fields, automatic VAT note, gapless numbers, PDF archive,
  Storno, payments, reminders (Mahnungen), ZUGFeRD/Factur-X e-invoices and XRechnung.
- **Expenses** with receipts (stored unchanged), EÜR categories, GWG / depreciation (AfA),
  Bewirtung 70/30, foreign currency, home office flat rate. Incoming XRechnung/ZUGFeRD can be read in.
- **Other income** (e.g. Übungsleiterhonorar without an invoice).
- **Bank CSV import** (Kontist, N26, Sparkasse, DKB, ING, … auto-detected) with duplicate check,
  invoice matching, "make expense from this", tax office payments, missing-receipt warnings.
- **Reports**: EÜR (with Anlage EÜR line numbers), income tax estimate with "set aside" amount,
  Kleinunternehmer check (25.000 € / 100.000 €), UStVA, ZM, asset list, CSV/PDF/ZIP exports.
- **Dashboard** with to-dos, open invoices, deadlines and the tax estimate.
- **AI assistant** (optional, local via Ollama) that prepares drafts and bookings for you to confirm.

## AI assistant (optional, runs on your Mac)

Write what you want in Turkish, German or English, e.g. "GrenzKultur'a eylül için 12,5 saatlik fatura
taslağı hazırla" or "Mark the Vodafone payments as expenses". The assistant looks things up and shows
each change as a card. **Nothing is saved until you press Apply**, and it can never issue, cancel or
delete anything. It uses [Ollama](https://ollama.com), so your data never leaves the computer.

1. Install Ollama from https://ollama.com and open it once.
2. In Terminal: `ollama pull qwen3:8b` (about 5 GB; on a Mac with 8 GB RAM use `qwen3:4b`).
3. Settings → "AI assistant (local)" → switch it on. "Assistant" then appears in the menu.

Small local models make mistakes: read every card before you press Apply.

## Easiest: the Mac app

No Terminal needed: download **Buero-mac** (a ZIP with Buero.dmg) from the latest successful "Mac app" run under the
repository's **Actions** tab (or from a Release), open the DMG and drag Büro to Applications.
Step-by-step guide to send to friends: [docs/INSTALL-MAC.md](docs/INSTALL-MAC.md).

## Install from source (once)

1. Install Homebrew if you don't have it: https://brew.sh
2. In Terminal:
   ```
   brew install uv pango
   ```
   (`pango` is needed by WeasyPrint to make PDFs.)
3. Put this folder somewhere, e.g. `~/Code/buero`, then:
   ```
   cd ~/Code/buero
   uv sync
   ```

## Start

```
cd ~/Code/buero
uv run buero
```

The browser opens at http://127.0.0.1:8000. Stop with `Ctrl+C`.
The app only listens on your own computer (127.0.0.1), nobody else on the network can open it.

### If PDFs or the invoice preview do not work

The app shows a red note at the top when it cannot make PDFs. Then:

1. In Terminal: `brew install pango`
2. Stop the app (`Ctrl+C`) and start it again with `uv run buero`.

On Apple Silicon Macs Homebrew installs Pango in `/opt/homebrew/lib`. The app adds that folder to the
library path by restarting itself once at launch, so no extra setup is needed. Until PDFs work, "Preview PDF"
shows the invoice as a web page instead.

## First steps

1. **Settings**: your name, address, Steuernummer, IBAN, Kleinunternehmer yes/no, Ist- or
   Soll-Versteuerung, church tax, backup folder.
2. **Continue numbering**: if you already issued invoices this year in Accountable, set the next
   free number (e.g. 2026 → 19). The app then continues without a gap.
3. **VAT status per year**: if your Kleinunternehmer status was different in earlier years,
   set it per year in Settings, so old reports stay correct.
4. **Clients**: mark youth welfare organisations (e.g. GrenzKultur gGmbH / CABUWAZI) as
   "§ 4 Nr. 25" and schools as "§ 4 Nr. 21 b", and note where the proof is
   (e.g. "Anerkennungsbescheid § 75 SGB VIII, PDF in Mail vom …").
5. **Bank**: import your Kontist CSV. Transactions you already booked in Accountable can be skipped
   in one step ("Already booked these in Accountable?", choose a date). Everything can be undone.

## Where your data is

Default: `~/Documents/Büro/` (installs that already use `~/Freelance/buero-daten/` keep that folder) (change with `BUERO_DATA_DIR=/some/folder uv run buero`).

```
buero-daten/
  buero.sqlite3          database
  invoices/2026/         archived invoice and Storno PDFs, written once, never overwritten
  receipts/2026/         your receipts, stored byte-for-byte as uploaded
  reminders/2026/        archived reminder PDFs
  bank-imports/          every imported bank CSV, unchanged
  backups/               a dated copy of the database on every start
```

None of this is inside the code folder, and `.gitignore` excludes it anyway.

## Backup

- On every start the app copies the database to `backups/`. Old backups are never deleted.
- **Full backup** (database + all PDFs + receipts as one tested ZIP):
  Settings → "Backup now", or in Terminal:
  ```
  uv run buero-backup /Volumes/MyDisk/buero-backups
  ```
- Keep at least one copy outside the laptop (Time Machine, external disk, encrypted cloud folder).
- Restore: copy the `buero-daten` folder back (from a ZIP: unzip it into your data folder).

Retention: invoices and receipts 8 years, books and reports 10 years. The app never deletes anything.

## Rules the app enforces (GoBD / § 14 UStG)

- A draft can be edited and deleted. It has no number yet.
- **Issue** checks the mandatory fields, assigns the next number (`YYYY-NN`, per year, no gaps,
  no duplicates), freezes your and the client's data as they are at that moment, writes the PDF
  to `invoices/YYYY/` and stores its SHA-256 checksum.
- Issued invoices, receipts, reminders and the audit log can never be changed or deleted.
  Expenses and income are "voided" with a reason instead of deleted. This is enforced in the app
  **and** by triggers inside the SQLite database.
- Corrections: **Storno** issues a cancellation invoice with its own number and a reference to the
  original, then optionally opens a new draft with the same items.
- Every change is written to the **Audit log**.
- Process description for the tax office: [docs/Verfahrensdokumentation.md](docs/Verfahrensdokumentation.md)
  Copy it into your data folder as `Verfahrensdokumentation.md` and fill in the gaps marked `[…]`;
  the yearly export ZIP then includes your filled-in copy.

## VAT note on invoices

Chosen automatically, in this order (can be overridden per draft, with a warning):

| Situation | Rate | Note on the invoice |
|---|---|---|
| Client marked "§ 4 Nr. 25" (recognised Träger der Jugendhilfe) | 0 % | Steuerfreie Leistung der Kinder- und Jugendhilfe gemäß § 4 Nr. 25 … UStG |
| Client marked "§ 4 Nr. 21 b" (certified school/education institution) | 0 % | Umsatzsteuerfrei nach § 4 Nr. 21 Buchst. b UStG |
| EU business with VAT ID | 0 % | Steuerschuldnerschaft des Leistungsempfängers (Reverse Charge), both VAT IDs |
| Business outside the EU | 0 % | Nicht im Inland steuerbare Leistung (§ 3a Abs. 2 UStG) |
| You are Kleinunternehmer | 0 % | Gemäß § 19 UStG wird keine Umsatzsteuer berechnet. |
| Otherwise | 19 % | – |

Exemptions warn you until you record the evidence.

## How the reports count

- **EÜR** is cash based (Zufluss/Abfluss, § 11 EStG): income counts on the payment date, expenses
  on the payment date. Equipment over 800 € net is depreciated over its useful life (pro rata by
  month); up to 800 € net it is a GWG and fully deductible. Bewirtung: 70 % deductible, 30 % shown
  separately as not deductible. Home office: days × 6 €, max. 210 days.
- **Income tax estimate**: § 32a EStG tariff of that year, single person, plus Soli and church tax
  (if set) and Gewerbesteuer for a Gewerbe. A guide for setting money aside, not a Steuerbescheid.
- **Kleinunternehmer check**: turnover = taxable domestic turnover. Tax-exempt work (§ 4 Nr. 25,
  Nr. 21) and work taxed abroad do not count (§ 19 Abs. 2 UStG).
- **UStVA / ZM** only matter when you are not Kleinunternehmer, or if you have EU clients/suppliers.

Line numbers (Kz / Zeilen) follow the 2025 forms. Forms change every year, so compare the labels
with what Mein ELSTER shows before typing numbers in.

This is a tool, not tax advice.

## Tests

```
uv run pytest
```

Covers money rounding, all VAT cases, numbering, immutability (including direct SQL), Storno,
payments, expenses/AfA/GWG, EÜR, UStVA, ZM, Kleinunternehmer limits, income tax tariff,
bank import (incl. a real Kontist export format), e-invoices (XSD validated), reminders,
deadlines, backup/export and every page of the web app.

## Database schema

See [docs/schema.md](docs/schema.md).
