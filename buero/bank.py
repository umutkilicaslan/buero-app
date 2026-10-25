"""Bank CSV import and matching.

Column names are detected automatically (Kontist, N26, Sparkasse, DKB, ING, Commerzbank, Postbank,
Deutsche Bank, Revolut, Wise and most other German bank exports). If detection fails, the user
maps the columns by hand.
"""
import csv
import hashlib
import io
import itertools
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import config, money
from .models import BankImport, Client, Expense, Invoice, Transaction
from .services import BusinessError, audit

FIELDS = {
    "date": ["buchungsdatum", "buchungstag", "booking date", "date", "datum", "completed date",
             "started date", "wertstellungsdatum", "valutadatum", "value date"],
    "amount": ["betrag", "amount (eur)", "amount", "betrag (€)", "betrag (eur)", "umsatz", "umsatz in eur",
               "betrag in eur"],
    "counterparty": ["empfänger", "partner name", "payee", "beguenstigter/zahlungspflichtiger",
                     "begünstigter/zahlungspflichtiger", "zahlungsempfänger*in", "auftraggeber / begünstigter",
                     "auftraggeber/empfänger", "name zahlungsbeteiligter", "gegenpartei", "description",
                     "counterparty", "empfänger/zahlungspflichtiger", "zahlungspflichtige*r"],
    "iban": ["iban", "partner iban", "kontonummer/iban", "iban zahlungsbeteiligter", "account number",
             "iban gegenkonto"],
    "purpose": ["verwendungszweck", "payment reference", "reference", "buchungstext", "beschreibung",
                "vorgang/verwendungszweck", "text"],
    "reference": ["end_to_end_id", "end-to-end-referenz", "transaction id", "mandatsreferenz", "id",
                  "kundenreferenz"],
    "currency": ["währung", "waehrung", "currency"],
}
DATE_FORMATS = ["%Y-%m-%d", "%d.%m.%Y", "%d.%m.%y", "%d/%m/%Y", "%Y-%m-%d %H:%M:%S", "%m/%d/%Y"]


@dataclass
class ParsedRow:
    booking_date: date
    amount: Decimal
    counterparty: str
    iban: str
    purpose: str
    reference: str
    currency: str


def decode(content: bytes) -> str:
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return content.decode(enc)
        except UnicodeDecodeError:
            continue
    raise BusinessError("Could not read the file encoding.")


def _sniff(text: str) -> tuple[list[str], list[list[str]]]:
    sample = text[:5000]
    delim = max([";", ",", "\t"], key=sample.count)
    rows = list(csv.reader(io.StringIO(text), delimiter=delim))
    # Some banks put account info above the header: take the first row with ≥ 3 known names.
    known = {n for names in FIELDS.values() for n in names}
    for i, row in enumerate(rows[:30]):
        if sum(1 for c in row if c.strip().lower() in known) >= 2:
            return [c.strip() for c in row], [r for r in rows[i + 1:] if any(x.strip() for x in r)]
    raise BusinessError("No header row found. Map the columns manually.")


def detect_mapping(header: list[str]) -> dict[str, int]:
    lower = [h.lower() for h in header]
    mapping = {}
    for field, names in FIELDS.items():
        for name in names:  # first name in the list wins (priority order)
            if name in lower and lower.index(name) not in mapping.values():
                mapping[field] = lower.index(name)
                break
    return mapping


def parse_date(text: str) -> date:
    text = text.strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise BusinessError(f"Unknown date format: {text!r}")


def parse(content: bytes, mapping: dict[str, int] | None = None) -> tuple[list[ParsedRow], list[str], dict]:
    header, rows = _sniff(decode(content))
    mapping = mapping or detect_mapping(header)
    missing = [f for f in ("date", "amount") if f not in mapping]
    if missing:
        raise BusinessError("Could not find the column(s): " + ", ".join(missing) + ". Map the columns manually.")
    out = []
    for r in rows:
        def col(f):
            i = mapping.get(f)
            return r[i].strip() if i is not None and i < len(r) else ""
        if not col("date") or not col("amount"):
            continue
        out.append(ParsedRow(parse_date(col("date")), money.parse_decimal(col("amount")), col("counterparty"),
                             col("iban").replace(" ", ""), col("purpose"), col("reference"),
                             (col("currency") or "EUR").upper()[:3]))
    if not out:
        raise BusinessError("The file contains no transactions.")
    return out, header, mapping


def dedupe_keys(rows: list[ParsedRow]) -> list[str]:
    """Same file imported twice → same keys. Two identical transactions on one day stay distinct
    because the occurrence number is part of the key."""
    seen: dict[str, int] = {}
    keys = []
    for r in rows:
        if r.reference and r.reference.upper() not in ("NOTPROVIDED", "NONREF", "-"):
            base = f"ref|{r.reference}|{r.booking_date}|{r.amount}"
        else:
            base = f"{r.booking_date}|{r.amount}|{r.counterparty.lower()}|{r.iban}|{r.purpose.lower()}"
        n = seen.get(base, 0)
        seen[base] = n + 1
        keys.append(hashlib.sha256(f"{base}|{n}".encode()).hexdigest())
    return keys


def import_csv(s: Session, filename: str, content: bytes, mapping: dict[str, int] | None = None) -> BankImport:
    rows, header, mapping = parse(content, mapping)
    keys = dedupe_keys(rows)
    existing = set(s.scalars(select(Transaction.dedupe_key).where(Transaction.dedupe_key.in_(keys))))
    digest = hashlib.sha256(content).hexdigest()
    (config.imports_dir() / f"{datetime.now():%Y-%m-%d_%H%M%S}_{digest[:8]}.csv").write_bytes(content)
    imp = BankImport(filename=filename, sha256=digest, profile=",".join(f"{k}={header[v]}" for k, v in mapping.items()),
                     rows_total=len(rows), rows_new=0)
    s.add(imp)
    s.flush()
    for r, k in zip(rows, keys):
        if k in existing:
            continue
        s.add(Transaction(import_id=imp.id, booking_date=r.booking_date, amount=r.amount, currency=r.currency,
                          counterparty=r.counterparty, iban=r.iban, purpose=r.purpose, reference=r.reference,
                          dedupe_key=k, status="open"))
        imp.rows_new += 1
    s.flush()
    audit(s, "bank_import", imp.id, "imported", file=filename, total=len(rows), new=imp.rows_new)
    autoclassify(s)
    return imp


# ---------- suggestions ----------

TAX_OFFICE = re.compile(r"finanzamt|finanzkasse|landeshauptkasse|bundeskasse", re.I)


def autoclassify(s: Session) -> None:
    """Only obvious, reversible tags: payments to/from the Finanzamt are proposed as tax."""
    for tx in s.scalars(select(Transaction).where(Transaction.status == "open")):
        if TAX_OFFICE.search(f"{tx.counterparty} {tx.purpose}"):
            text = tx.purpose.lower()
            tx.tax_kind = "ust" if re.search(r"\bust\b|umsatzsteuer|ust-va|ustva", text) else "est"
            m = re.search(r"(20\d\d)", tx.purpose)
            tx.tax_year = int(m.group(1)) if m else tx.booking_date.year


NUMBER_RE = re.compile(r"(?<!\d)(20\d\d)\s*[-/_.]\s*(\d{1,4})(?!\d)|(?<!\d)(\d{1,4})\s*[-/_.]\s*(20\d\d)(?!\d)")


def invoice_numbers_in(text: str) -> set[tuple[int, int]]:
    found = set()
    for m in NUMBER_RE.finditer(text or ""):
        if m.group(1):
            found.add((int(m.group(1)), int(m.group(2))))
        else:
            found.add((int(m.group(4)), int(m.group(3))))
    return found


def _similar(a: str, b: str) -> bool:
    words = lambda x: {w for w in re.findall(r"[a-zäöüß0-9]{3,}", x.lower())} - {"gmbh", "ggmbh", "gug", "e.v", "und"}
    return bool(words(a) & words(b))


@dataclass
class Match:
    invoices: list[Invoice]
    reason: str
    score: int


def suggest_invoices(s: Session, tx: Transaction) -> list[Match]:
    if tx.amount <= 0:
        return []
    open_inv = list(s.scalars(select(Invoice).where(Invoice.kind == "invoice", Invoice.status == "issued")))
    if not open_inv:
        return []
    matches: list[Match] = []
    nums = invoice_numbers_in(tx.purpose)
    by_number = [i for i in open_inv if (i.year, i.seq) in nums]
    if by_number:
        total = sum(i.outstanding for i in by_number)
        exact = total == tx.amount
        matches.append(Match(by_number, "Invoice number in the payment reference"
                             + (" and amount matches" if exact else f" (amount differs: {money.fmt_eur(total)})"),
                             100 if exact else 70))
    same_amount = [i for i in open_inv if i.outstanding == tx.amount and i not in by_number]
    for i in same_amount:
        named = _similar(i.client.name, tx.counterparty)
        matches.append(Match([i], "Same amount" + (" and client name" if named else ""), 80 if named else 50))
    # Several invoices of one client paid together
    if not by_number:
        by_client: dict[int, list[Invoice]] = {}
        for i in open_inv:
            if _similar(i.client.name, tx.counterparty):
                by_client.setdefault(i.client_id, []).append(i)
        for invs in by_client.values():
            invs = sorted(invs, key=lambda i: i.issue_date)[:12]
            for n in range(2, min(len(invs), 6) + 1):
                for combo in itertools.combinations(invs, n):
                    if sum(i.outstanding for i in combo) == tx.amount:
                        matches.append(Match(list(combo), f"{n} invoices of this client add up to the amount", 75))
                        break
    seen, unique = set(), []
    for m in sorted(matches, key=lambda m: -m.score):
        key = tuple(sorted(i.id for i in m.invoices))
        if key not in seen:
            seen.add(key)
            unique.append(m)
    return unique[:5]


def suggest_expense(s: Session, tx: Transaction) -> dict:
    """Prefill for 'turn into expense': reuse category and VAT of the last expense from this vendor."""
    prev = s.scalar(select(Expense).join(Transaction, Expense.transaction_id == Transaction.id)
                    .where(Transaction.counterparty == tx.counterparty, Expense.voided.is_(False))
                    .order_by(Expense.id.desc()))
    if prev is None:
        prev = s.scalar(select(Expense).where(Expense.vendor == tx.counterparty, Expense.voided.is_(False))
                        .order_by(Expense.id.desc()))
    return {"vendor": tx.counterparty or tx.purpose[:60], "date": tx.booking_date, "gross_eur": -tx.amount,
            "description": tx.purpose, "category": prev.category if prev else "",
            "vat_rate": prev.vat_rate if prev else None, "business_share": prev.business_share if prev else Decimal(100),
            "reverse_charge": prev.reverse_charge if prev else ""}


def apply_match(s: Session, tx: Transaction, invoices: list[Invoice]) -> None:
    from .services import mark_paid
    if tx.status != "open":
        raise BusinessError("This transaction is already assigned.")
    remaining = tx.amount
    for inv in sorted(invoices, key=lambda i: i.issue_date):
        part = min(inv.outstanding, remaining)
        if part <= 0:
            break
        mark_paid(s, inv, tx.booking_date, part, transaction_id=tx.id)
        remaining -= part
    if remaining > 0:
        raise BusinessError(f"{money.fmt_eur(remaining)} of this payment is not covered by the selected invoices. "
                            "Select more invoices or book the rest as other income.")
    tx.status = "invoice"
    audit(s, "transaction", tx.id, "matched", invoices=[i.number for i in invoices])


def set_status(s: Session, tx: Transaction, status: str, tax_kind: str = "", tax_year: int | None = None,
               note: str = "") -> None:
    if status not in ("open", "private", "tax", "transfer", "ignored"):
        raise BusinessError("Unknown status.")
    if tx.status in ("invoice", "expense", "income") and status != tx.status:
        raise BusinessError("Undo the linked invoice payment, expense or income first.")
    if status == "tax" and tax_kind not in ("est", "ust", "other"):
        raise BusinessError("Choose which tax this is.")
    before = [tx.status, tx.tax_kind, tx.tax_year]
    tx.status, tx.tax_kind, tx.tax_year, tx.note = status, tax_kind if status == "tax" else "", \
        tax_year if status == "tax" else None, note
    audit(s, "transaction", tx.id, "status", before=before, after=[status, tax_kind, tax_year], note=note)


def ignore_open_before(s: Session, before: date, note: str) -> int:
    """Bulk-mark open transactions before a date as ignored (e.g. already booked in another tool)."""
    if not note.strip():
        raise BusinessError("Give a reason, e.g. 'already booked in Accountable'.")
    rows = list(s.scalars(select(Transaction).where(Transaction.status == "open", Transaction.booking_date < before)))
    for tx in rows:
        set_status(s, tx, "ignored", note=note.strip())
    audit(s, "transaction", None, "bulk_ignored", before=before.isoformat(), count=len(rows), note=note.strip())
    return len(rows)


def missing_receipts(s: Session) -> list[Expense]:
    """Expenses that should have a receipt but don't (flat rates excluded)."""
    q = select(Expense).where(Expense.voided.is_(False)).order_by(Expense.date.desc())
    return [e for e in s.scalars(q) if not e.receipts and e.category not in ("meals_travel", "car_km")]
