"""Business operations. All writes go through here so they are checked and audited."""
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import money, vat
from .models import AuditLog, Client, Invoice, InvoiceLine, NumberSequence, Payment, Settings, Transaction, VatYear


class BusinessError(Exception):
    """A rule was violated; the message is shown to the user."""


def audit(s: Session, entity: str, entity_id: int | None, action: str, **details) -> None:
    s.add(AuditLog(entity=entity, entity_id=entity_id, action=action,
                   details=json.dumps(details, default=str, ensure_ascii=False)))


def get_settings(s: Session) -> Settings:
    return s.get(Settings, 1)


# ---------- clients ----------

CLIENT_FIELDS = ["name", "contact_person", "street", "postal_code", "city", "country", "is_business",
                 "vat_id", "email", "default_hourly_rate", "exemption", "exemption_evidence", "notes", "leitweg_id"]


def save_client(s: Session, client: Client | None, data: dict) -> Client:
    if not data.get("name", "").strip():
        raise BusinessError("Client name is required.")
    data["country"] = (data.get("country") or getattr(client, "country", None) or "DE").strip().upper()
    if len(data["country"]) != 2:
        raise BusinessError("Country must be a 2-letter code, e.g. DE, AT, US.")
    if data.get("exemption", "") not in ("", "4_25", "4_21"):
        raise BusinessError("Unknown exemption.")
    is_new = client is None
    client = client or Client()
    changes = {}
    for f in CLIENT_FIELDS:
        if f in data and getattr(client, f, None) != data[f]:
            changes[f] = [getattr(client, f, None), data[f]]
            setattr(client, f, data[f])
    s.add(client)
    s.flush()
    audit(s, "client", client.id, "created" if is_new else "updated", changes=changes)
    return client


def save_settings(s: Session, data: dict) -> Settings:
    st = get_settings(s)
    if "kleinunternehmer" in data and data["kleinunternehmer"] != st.kleinunternehmer:
        # Earlier years keep the status they had; only the current year changes.
        from .models import Expense
        years = {y for (y,) in s.execute(select(Invoice.year).where(Invoice.year.is_not(None)))}
        years |= {d.year for (d,) in s.execute(select(Expense.date))}
        for y in sorted(years):
            if y < date.today().year and s.get(VatYear, y) is None:
                set_vat_year(s, y, st.kleinunternehmer)
        set_vat_year(s, date.today().year, data["kleinunternehmer"])
    changes = {}
    for k, v in data.items():
        if getattr(st, k) != v:
            changes[k] = [getattr(st, k), v]
            setattr(st, k, v)
    audit(s, "settings", 1, "updated", changes=changes)
    return st


# ---------- numbering ----------

def format_number(year: int, seq: int, digits: int) -> str:
    return f"{year}-{seq:0{digits}d}"


def next_number_preview(s: Session, year: int) -> int:
    row = s.get(NumberSequence, year)
    return row.next_number if row else 1


def set_next_number(s: Session, year: int, next_number: int) -> None:
    """Continue numbering from another system (e.g. Accountable). Only allowed before the first
    invoice of that year is issued here, so no gap or duplicate can be created."""
    if next_number < 1:
        raise BusinessError("Number must be at least 1.")
    issued = s.scalar(select(func.count()).select_from(Invoice).where(Invoice.year == year,
                                                                      Invoice.number.is_not(None)))
    if issued:
        raise BusinessError(f"{year} already has issued invoices in this app; the sequence can no longer be changed.")
    row = s.get(NumberSequence, year) or NumberSequence(year=year)
    old = row.next_number
    row.next_number = next_number
    s.add(row)
    audit(s, "number_sequence", year, "set_start", old=old, new=next_number)


def _take_number(s: Session, year: int, digits: int) -> tuple[str, int]:
    row = s.get(NumberSequence, year)
    if row is None:
        row = NumberSequence(year=year, next_number=1)
        s.add(row)
    seq = row.next_number
    row.next_number = seq + 1
    return format_number(year, seq, digits), seq


@dataclass
class NumberProblem:
    year: int
    kind: str
    detail: str


def check_numbering(s: Session) -> list[NumberProblem]:
    """Gaps and duplicates among issued invoices, per year, starting from the first number issued here."""
    problems = []
    rows = s.execute(select(Invoice.year, Invoice.seq).where(Invoice.seq.is_not(None))).all()
    by_year: dict[int, list[int]] = {}
    for y, n in rows:
        by_year.setdefault(y, []).append(n)
    for y, nums in sorted(by_year.items()):
        seen = set()
        for n in nums:
            if n in seen:
                problems.append(NumberProblem(y, "duplicate", f"number {n} used twice"))
            seen.add(n)
        missing = sorted(set(range(min(nums), max(nums) + 1)) - seen)
        if missing:
            problems.append(NumberProblem(y, "gap", "missing: " + ", ".join(map(str, missing))))
    return problems


# ---------- drafts ----------

def new_draft(s: Session, client: Client, today: date | None = None) -> Invoice:
    st = get_settings(s)
    inv = Invoice(client=client, status="draft", kind="invoice", language=st.invoice_language)
    s.add(inv)
    s.flush()
    audit(s, "invoice", inv.id, "draft_created", client=client.name)
    return inv


def update_draft(s: Session, inv: Invoice, header: dict, lines: list[dict]) -> Invoice:
    _require_draft(inv)
    if inv.kind == "storno":
        raise BusinessError("A cancellation invoice is generated automatically and cannot be edited.")
    before = draft_snapshot(inv)
    for k in ("client_id", "language", "issue_date", "service_from", "service_to", "intro_text", "vat_override"):
        if k in header:
            setattr(inv, k, header[k])
    if header.get("client_id"):
        inv.client = s.get(Client, header["client_id"])
    inv.lines.clear()
    s.flush()
    for i, ln in enumerate(lines, start=1):
        qty, price = ln["quantity"], ln["unit_price"]
        inv.lines.append(InvoiceLine(position=i, description=ln["description"], quantity=qty,
                                     unit=ln.get("unit", ""), unit_price=price,
                                     line_net=money.line_net(qty, price)))
    _recalc(s, inv)
    s.flush()
    audit(s, "invoice", inv.id, "draft_updated", before=before, after=draft_snapshot(inv))
    return inv


def delete_draft(s: Session, inv: Invoice) -> None:
    _require_draft(inv)
    audit(s, "invoice", inv.id, "draft_deleted", snapshot=draft_snapshot(inv))
    s.delete(inv)


def draft_snapshot(inv: Invoice) -> dict:
    return {
        "client_id": inv.client_id, "issue_date": inv.issue_date, "service_from": inv.service_from,
        "service_to": inv.service_to, "language": inv.language, "vat_override": inv.vat_override,
        "intro_text": inv.intro_text,
        "lines": [[ln.description, str(ln.quantity), ln.unit, str(ln.unit_price)] for ln in inv.lines],
    }


def is_kleinunternehmer(s: Session, year: int) -> bool:
    row = s.get(VatYear, year)
    return row.kleinunternehmer if row else get_settings(s).kleinunternehmer


def set_vat_year(s: Session, year: int, kleinunternehmer: bool) -> None:
    row = s.get(VatYear, year) or VatYear(year=year, kleinunternehmer=kleinunternehmer)
    old = row.kleinunternehmer if row in s else None
    row.kleinunternehmer = kleinunternehmer
    s.add(row)
    audit(s, "vat_year", year, "set", old=old, kleinunternehmer=kleinunternehmer)


def vat_decision(s: Session, inv: Invoice) -> vat.VatDecision:
    st = get_settings(s)
    year = (inv.issue_date or date.today()).year
    return vat.decide(seller_kleinunternehmer=is_kleinunternehmer(s, year), seller_vat_id=st.vat_id,
                      client=inv.client, override=inv.vat_override)


def _recalc(s: Session, inv: Invoice) -> None:
    d = vat_decision(s, inv)
    inv.vat_treatment, inv.vat_rate = d.treatment, d.rate
    inv.net_total, inv.vat_total, inv.gross_total = money.totals([ln.line_net for ln in inv.lines], d.rate)


def _require_draft(inv: Invoice) -> None:
    if not inv.is_draft:
        raise BusinessError("This invoice has been issued and can no longer be changed (GoBD). "
                            "Use a cancellation invoice (Storno) instead.")


# ---------- issuing ----------

def issue_problems(s: Session, inv: Invoice) -> list[str]:
    """Everything § 14 Abs. 4 UStG needs that is still missing."""
    st = get_settings(s)
    c = inv.client
    p = []
    if not st.complete:
        p.append("Your own details in Settings are incomplete (name, address, Steuernummer or USt-IdNr, IBAN).")
    if not (c.name and c.street and c.postal_code and c.city):
        p.append("Client's full address is missing.")
    if not inv.lines:
        p.append("The invoice has no line items.")
    if not inv.service_from:
        p.append("Date or period of service (Leistungszeitpunkt / Leistungszeitraum) is missing.")
    if inv.service_from and inv.service_to and inv.service_to < inv.service_from:
        p.append("Service period ends before it starts.")
    if inv.kind == "invoice" and inv.gross_total <= 0:
        p.append("Total must be greater than zero.")
    d = vat_decision(s, inv)
    if d.treatment == "reverse_charge" and not (st.vat_id and c.vat_id):
        p.append("Reverse charge needs both your USt-IdNr and the client's VAT ID.")
    return p


def issue(s: Session, inv: Invoice, today: date | None = None) -> Invoice:
    _require_draft(inv)
    today = today or date.today()
    inv.issue_date = inv.issue_date or today
    problems = issue_problems(s, inv)
    if problems:
        raise BusinessError(" ".join(problems))
    st = get_settings(s)
    d = vat_decision(s, inv)
    _recalc(s, inv)
    inv.number, inv.seq = _take_number(s, inv.issue_date.year, st.number_digits)
    inv.year = inv.issue_date.year
    inv.vat_note = d.note(inv.language)
    inv.due_date = inv.issue_date + timedelta(days=st.payment_terms_days)
    inv.seller_snapshot = json.dumps(seller_snapshot(st), ensure_ascii=False)
    inv.client_snapshot = json.dumps(client_snapshot(inv.client, d.show_vat_ids), ensure_ascii=False)
    inv.issued_at = datetime.now()
    inv.status = "issued"
    s.flush()
    audit(s, "invoice", inv.id, "issued", number=inv.number, gross=inv.gross_total,
          vat_treatment=inv.vat_treatment, warnings=d.warnings)
    return inv


def seller_snapshot(st: Settings) -> dict:
    return {k: getattr(st, k) for k in ("name", "business_name", "street", "postal_code", "city", "email",
                                        "phone", "tax_number", "vat_id", "bank_name", "iban", "bic",
                                        "payment_terms_days", "profession")}


def client_snapshot(c: Client, show_vat_id: bool) -> dict:
    return {"name": c.name, "contact_person": c.contact_person, "street": c.street,
            "postal_code": c.postal_code, "city": c.city, "country": c.country,
            "vat_id": c.vat_id if (show_vat_id or c.vat_id) else "", "email": c.email,
            "leitweg_id": c.leitweg_id}


def attach_pdf(s: Session, inv: Invoice, pdf_bytes: bytes, path) -> None:
    """Store the archived PDF once. It is never regenerated or overwritten afterwards."""
    if inv.pdf_sha256:
        raise BusinessError("This invoice already has its archived PDF.")
    path.write_bytes(pdf_bytes)
    inv.pdf_path = str(path)
    inv.pdf_sha256 = hashlib.sha256(pdf_bytes).hexdigest()
    audit(s, "invoice", inv.id, "pdf_archived", path=str(path), sha256=inv.pdf_sha256)


def pdf_filename(inv: Invoice) -> str:
    client = json.loads(inv.client_snapshot or "{}").get("name", "client")
    slug = re.sub(r"[^A-Za-z0-9]+", "_", client).strip("_")[:40]
    prefix = "Storno_" if inv.kind == "storno" else "Rechnung_"
    return f"{prefix}{inv.number}_{slug}.pdf"


# ---------- payment & cancellation ----------

def mark_paid(s: Session, inv: Invoice, paid_date: date, amount: Decimal | None = None,
              transaction_id: int | None = None) -> Payment:
    """Record a payment. Without an amount the full outstanding amount is assumed.
    The invoice counts as paid once the payments cover the total."""
    if inv.kind != "invoice" or inv.status != "issued":
        raise BusinessError("Only issued, unpaid invoices can receive payments.")
    if inv.issue_date and paid_date < inv.issue_date - timedelta(days=365):
        raise BusinessError("Payment date looks wrong (more than a year before the invoice date).")
    amount = inv.outstanding if amount is None else amount
    if amount <= 0:
        raise BusinessError("Payment amount must be positive.")
    p = Payment(invoice=inv, date=paid_date, amount=amount, transaction_id=transaction_id)
    s.add(p)
    s.flush()
    _refresh_payment_state(inv)
    audit(s, "invoice", inv.id, "payment", date=paid_date, amount=amount, transaction_id=transaction_id,
          status=inv.status)
    return p


def _refresh_payment_state(inv: Invoice) -> None:
    total = sum((p.amount for p in inv.payments), Decimal("0"))
    inv.paid_amount = total or None
    inv.paid_date = max((p.date for p in inv.payments), default=None)
    if inv.status in ("issued", "paid"):
        inv.status = "paid" if total >= inv.gross_total else "issued"


def unmark_paid(s: Session, inv: Invoice) -> None:
    """Remove all payments of an invoice (e.g. a wrong match). Logged with the old values."""
    if not inv.payments:
        raise BusinessError("Invoice has no payments.")
    removed = [[p.date, p.amount, p.transaction_id] for p in inv.payments]
    for p in list(inv.payments):
        if p.transaction_id:
            tx = s.get(Transaction, p.transaction_id)
            if tx and tx.status == "invoice" and all(q.invoice_id == inv.id for q in tx.payments):
                tx.status = "open"
        inv.payments.remove(p)
        s.delete(p)
    s.flush()
    _refresh_payment_state(inv)
    audit(s, "invoice", inv.id, "payments_removed", payments=removed)


def cancel(s: Session, original: Invoice, today: date | None = None, reason: str = "") -> Invoice:
    """Create and issue a Stornorechnung with negated lines; the original becomes 'cancelled'."""
    if original.kind != "invoice" or original.status not in ("issued", "paid"):
        raise BusinessError("Only issued invoices can be cancelled.")
    today = today or date.today()
    st = get_settings(s)
    lang = original.language
    storno = Invoice(kind="storno", status="draft", client_id=original.client_id, cancels_invoice_id=original.id,
                     language=lang, issue_date=today, service_from=original.service_from,
                     service_to=original.service_to)
    intro = {"de": f"Stornorechnung zu Rechnung Nr. {original.number} vom {original.issue_date:%d.%m.%Y}.",
             "en": f"Cancellation of invoice no. {original.number} dated {original.issue_date:%d.%m.%Y}."}[lang]
    storno.intro_text = intro + (f" {reason}" if reason else "")
    for ln in original.lines:
        storno.lines.append(InvoiceLine(position=ln.position, description=ln.description, quantity=-ln.quantity,
                                        unit=ln.unit, unit_price=ln.unit_price, line_net=-ln.line_net))
    s.add(storno)
    s.flush()
    # A Storno mirrors the original exactly, including its VAT treatment.
    storno.vat_treatment, storno.vat_rate, storno.vat_note = original.vat_treatment, original.vat_rate, original.vat_note
    storno.net_total, storno.vat_total, storno.gross_total = -original.net_total, -original.vat_total, -original.gross_total
    storno.number, storno.seq = _take_number(s, today.year, st.number_digits)
    storno.year = today.year
    storno.due_date = today
    storno.seller_snapshot = json.dumps(seller_snapshot(st), ensure_ascii=False)
    storno.client_snapshot = original.client_snapshot
    storno.issued_at = datetime.now()
    storno.status = "issued"
    original.status = "cancelled"
    s.flush()
    audit(s, "invoice", storno.id, "storno_issued", number=storno.number, cancels=original.number, reason=reason)
    audit(s, "invoice", original.id, "cancelled", by=storno.number)
    return storno


def copy_as_draft(s: Session, original: Invoice) -> Invoice:
    """Start the replacement invoice after a Storno."""
    inv = new_draft(s, original.client)
    inv.language, inv.service_from, inv.service_to = original.language, original.service_from, original.service_to
    for ln in original.lines:
        inv.lines.append(InvoiceLine(position=ln.position, description=ln.description, quantity=ln.quantity,
                                     unit=ln.unit, unit_price=ln.unit_price, line_net=ln.line_net))
    _recalc(s, inv)
    audit(s, "invoice", inv.id, "draft_copied_from", source=original.number)
    return inv


# ---------- lists ----------

def open_invoices(s: Session) -> list[Invoice]:
    return list(s.scalars(select(Invoice).where(Invoice.kind == "invoice", Invoice.status == "issued")
                          .order_by(Invoice.due_date)))
