"""CSV exports and a complete archive for a Steuerberater or a tax audit."""
import csv
import io
import json
import zipfile
from datetime import date, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import catalog, config, ledger
from .models import AuditLog, Client, Expense, Invoice, OtherIncome, Payment, Reminder, Transaction


def _csv(header: list[str], rows) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(header)
    for r in rows:
        w.writerow(["" if v is None else (str(v).replace(".", ",") if _is_money(v) else v) for v in r])
    return ("﻿" + buf.getvalue()).encode("utf-8")  # BOM so Excel opens umlauts correctly


def _is_money(v) -> bool:
    from decimal import Decimal
    return isinstance(v, Decimal)


def invoices_csv(s: Session, year: int | None = None) -> bytes:
    q = select(Invoice).where(Invoice.number.is_not(None)).order_by(Invoice.year, Invoice.seq)
    if year:
        q = q.where(Invoice.year == year)
    rows = []
    for i in s.scalars(q):
        c = json.loads(i.client_snapshot)
        rows.append([i.number, i.kind, i.status, i.issue_date, i.service_from, i.service_to, i.due_date, c.get("name"),
                     c.get("country"), c.get("vat_id"), i.vat_treatment, i.vat_rate, i.net_total, i.vat_total,
                     i.gross_total, i.paid_date, i.paid_amount, i.cancels.number if i.cancels else "", i.pdf_sha256])
    return _csv(["Nummer", "Art", "Status", "Rechnungsdatum", "Leistung von", "Leistung bis", "Fällig", "Kunde", "Land",
                 "USt-IdNr Kunde", "USt-Behandlung", "USt-Satz", "Netto", "USt", "Brutto", "Bezahlt am", "Bezahlt",
                 "Storno zu", "SHA-256 PDF"], rows)


def payments_csv(s: Session, year: int | None = None) -> bytes:
    rows = []
    for p in s.scalars(select(Payment).order_by(Payment.date)):
        if year and p.date.year != year:
            continue
        rows.append([p.date, p.invoice.number, p.invoice.client.name, p.amount, p.transaction_id or ""])
    return _csv(["Datum", "Rechnung", "Kunde", "Betrag", "Banktransaktion"], rows)


def expenses_csv(s: Session, year: int | None = None) -> bytes:
    rows = []
    for e in s.scalars(select(Expense).order_by(Expense.date)):
        if year and e.date.year != year:
            continue
        cat = catalog.CATEGORY_BY_KEY.get(e.category)
        rows.append([e.id, e.date, e.vendor, e.vendor_invoice_number, e.description, cat.label if cat else e.category,
                     cat.kz if cat else "", e.gross_eur, e.vat_rate, e.vat_eur, e.net_eur, e.business_share,
                     e.currency, e.amount_original, e.reverse_charge, e.useful_life_years, e.payment_method,
                     "storniert: " + e.void_reason if e.voided else "",
                     " | ".join(Path(r.path).name for r in e.receipts)])
    return _csv(["ID", "Datum", "Lieferant", "Belegnummer", "Beschreibung", "Kategorie", "EÜR-Kz", "Brutto EUR",
                 "USt-Satz", "USt EUR", "Netto EUR", "Betrieblicher Anteil %", "Währung", "Betrag Originalwährung",
                 "§ 13b", "Nutzungsdauer", "Zahlungsart", "Storno", "Belege"], rows)


def income_csv(s: Session, year: int | None = None) -> bytes:
    rows = []
    for o in s.scalars(select(OtherIncome).order_by(OtherIncome.date)):
        if year and o.date.year != year:
            continue
        rows.append([o.id, o.date, o.client.name if o.client else "", o.description, o.gross_eur, o.vat_eur,
                     o.vat_treatment, "storniert" if o.voided else ""])
    return _csv(["ID", "Datum", "Kunde", "Beschreibung", "Brutto", "USt", "USt-Behandlung", "Storno"], rows)


def transactions_csv(s: Session, year: int | None = None) -> bytes:
    rows = []
    for t in s.scalars(select(Transaction).order_by(Transaction.booking_date)):
        if year and t.booking_date.year != year:
            continue
        rows.append([t.id, t.booking_date, t.amount, t.currency, t.counterparty, t.iban, t.purpose, t.status,
                     t.tax_kind, t.tax_year, t.note])
    return _csv(["ID", "Buchungsdatum", "Betrag", "Währung", "Gegenpartei", "IBAN", "Verwendungszweck", "Zuordnung",
                 "Steuerart", "Steuerjahr", "Notiz"], rows)


def euer_csv(s: Session, year: int) -> bytes:
    r = ledger.euer(s, year)
    rows = [[kz, line.label, line.section, r.lines[kz]] for kz, line in catalog.EUER_LINES.items() if r.lines.get(kz)]
    rows += [["", "Summe Betriebseinnahmen", "", r.income_total], ["", "Summe Betriebsausgaben", "", r.expense_total],
             ["", "Gewinn", "", r.profit]]
    return _csv(["Kennzahl", "Bezeichnung", "Bereich", "Betrag EUR"], rows)


def audit_csv(s: Session) -> bytes:
    rows = [[a.id, a.ts, a.entity, a.entity_id, a.action, a.details] for a in s.scalars(select(AuditLog).order_by(AuditLog.id))]
    return _csv(["ID", "Zeit", "Objekt", "Objekt-ID", "Aktion", "Details"], rows)


def archive(s: Session, year: int, docs_dir: Path) -> bytes:
    """Everything for one year: CSVs, invoice and reminder PDFs, receipts, procedure documentation."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(f"{year}/Rechnungen.csv", invoices_csv(s, year))
        z.writestr(f"{year}/Zahlungseingaenge.csv", payments_csv(s, year))
        z.writestr(f"{year}/Ausgaben.csv", expenses_csv(s, year))
        z.writestr(f"{year}/Sonstige_Einnahmen.csv", income_csv(s, year))
        z.writestr(f"{year}/Bankumsaetze.csv", transactions_csv(s, year))
        z.writestr(f"{year}/EUER_{year}.csv", euer_csv(s, year))
        z.writestr(f"{year}/Protokoll.csv", audit_csv(s))
        for i in s.scalars(select(Invoice).where(Invoice.year == year)):
            if i.pdf_path and Path(i.pdf_path).exists():
                z.write(i.pdf_path, f"{year}/Rechnungen/{Path(i.pdf_path).name}")
        for r in s.scalars(select(Reminder)):
            if r.date.year == year and r.pdf_path and Path(r.pdf_path).exists():
                z.write(r.pdf_path, f"{year}/Mahnungen/{Path(r.pdf_path).name}")
        for e in s.scalars(select(Expense)):
            for rc in e.receipts:
                if e.date.year == year and Path(rc.path).exists():
                    z.write(rc.path, f"{year}/Belege/{Path(rc.path).name}")
        # Your filled-in copy in the data folder wins over the template shipped with the app.
        doc = next((d for d in (config.data_dir() / "Verfahrensdokumentation.md", docs_dir / "Verfahrensdokumentation.md")
                    if d.exists()), None)
        if doc:
            z.write(doc, "Verfahrensdokumentation.md")
        z.writestr("LIESMICH.txt", f"Export vom {datetime.now():%d.%m.%Y %H:%M}. CSV-Dateien: Semikolon, UTF-8, "
                                   "Dezimalkomma. SHA-256-Prüfsummen der Rechnungs-PDFs in Rechnungen.csv.\n")
    return buf.getvalue()
