"""Local AI assistant: a chat that reads your books and *proposes* changes.

It talks to a local Ollama server (default http://127.0.0.1:11434), so nothing leaves the computer.
The model can only:
  - read data through the read tools below, and
  - propose a change (draft invoice, client, expense, other income, bank assignment).
A proposal is shown as a card. Nothing is written until you press "Apply" on it, and every applied
proposal goes through the same service functions and checks as the normal forms. The assistant can
never issue, cancel or delete anything.
"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import bookkeeping, catalog, ledger, money, services, taxcalc, vat
from .models import Client, Expense, Invoice, OtherIncome, Transaction
from .services import BusinessError

DEFAULT_URL = "http://127.0.0.1:11434"
DEFAULT_MODEL = "qwen3:8b"
NUM_CTX = 16384          # Ollama's default context is too small for the tool list
MAX_ROUNDS = 8           # tool rounds per user message
TIMEOUT = 300.0          # a laptop can be slow on the first answer while the model loads


class AssistantError(Exception):
    pass


# ---------- conversations (in memory; this is a single-user local app) ----------

@dataclass
class Proposal:
    id: str
    kind: str
    title: str
    rows: list[tuple[str, str]]
    data: dict
    notes: list[str] = field(default_factory=list)
    status: str = "pending"      # pending | applied | dismissed | failed
    url: str = ""
    error: str = ""


@dataclass
class Conversation:
    id: str
    messages: list[dict] = field(default_factory=list)      # Ollama message history (without system)
    display: list[dict] = field(default_factory=list)       # what the chat page shows
    proposals: dict[str, Proposal] = field(default_factory=dict)


CONVERSATIONS: dict[str, Conversation] = {}


def conversation(cid: str | None) -> Conversation:
    if cid and cid in CONVERSATIONS:
        return CONVERSATIONS[cid]
    conv = Conversation(id=uuid.uuid4().hex[:12])
    CONVERSATIONS[conv.id] = conv
    return conv


def find_proposal(pid: str) -> tuple[Conversation, Proposal]:
    for conv in CONVERSATIONS.values():
        if pid in conv.proposals:
            return conv, conv.proposals[pid]
    raise BusinessError("This suggestion is no longer available (the app was restarted). Ask again.")


# ---------- small helpers ----------

def _s(x) -> str | None:
    """Money/decimal as plain string for the model."""
    return None if x is None else format(x, "f")


def _dec(v, name: str) -> Decimal:
    if isinstance(v, bool) or v is None or v == "":
        raise BusinessError(f"'{name}' is missing.")
    try:
        if isinstance(v, (int, float)):
            return Decimal(str(v))
        return money.parse_decimal(str(v))
    except (InvalidOperation, ValueError):
        raise BusinessError(f"'{name}' is not a number: {v!r}") from None


def _date(v, name: str, required: bool = True) -> date | None:
    if not v:
        if required:
            raise BusinessError(f"'{name}' is missing (format YYYY-MM-DD).")
        return None
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        raise BusinessError(f"'{name}' must be a date like 2026-09-30, got {v!r}.") from None


def _int(v, name: str) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        raise BusinessError(f"'{name}' must be a number (an id from a list tool), got {v!r}.") from None


def _get(s: Session, model, id_, what: str):
    obj = s.get(model, _int(id_, f"{what}_id"))
    if obj is None:
        raise BusinessError(f"There is no {what} with id {id_}. Look it up with a list tool first.")
    return obj


def _client_row(c: Client) -> dict:
    return {"id": c.id, "name": c.name, "city": c.city, "country": c.country, "business": c.is_business,
            "vat_id": c.vat_id or None, "exemption": c.exemption or None,
            "default_hourly_rate": _s(c.default_hourly_rate), "email": c.email or None}


def _invoice_row(i: Invoice) -> dict:
    return {"id": i.id, "number": i.number or "(draft)", "status": i.status, "client": i.client.name,
            "client_id": i.client_id, "issue_date": str(i.issue_date) if i.issue_date else None,
            "service": f"{i.service_from or ''}..{i.service_to or ''}", "gross": _s(i.gross_total),
            "outstanding": _s(i.outstanding) if i.status in ("issued",) else None}


def _tx_row(t: Transaction) -> dict:
    return {"id": t.id, "date": str(t.booking_date), "amount": _s(t.amount), "counterparty": t.counterparty,
            "purpose": (t.purpose or "")[:140], "status": t.status}


# ---------- read tools ----------

def t_overview(s: Session) -> dict:
    st = services.get_settings(s)
    y = date.today().year
    ku = ledger.ku_status(s, y)
    return {"today": str(date.today()), "your_name": st.name, "business_type": st.business_type,
            "kleinunternehmer_this_year": services.is_kleinunternehmer(s, y),
            "turnover_this_year_for_kleinunternehmer_limit": _s(ku.current_total),
            "turnover_last_year": _s(ku.prior_total),
            "open_invoices": len(services.open_invoices(s)),
            "bank_transactions_to_do": len(list(s.scalars(select(Transaction.id).where(Transaction.status == "open")))),
            "payment_terms_days": st.payment_terms_days}


def t_find_clients(s: Session, query: str = "") -> dict:
    rows = list(s.scalars(select(Client).order_by(Client.name)))
    if query:
        q = query.lower()
        rows = [c for c in rows if q in c.name.lower() or q in (c.city or "").lower()
                or any(w in c.name.lower() for w in q.split() if len(w) > 3)]
    out = []
    for c in rows[:25]:
        r = _client_row(c)
        last = s.scalars(select(Invoice).where(Invoice.client_id == c.id, Invoice.kind == "invoice")
                         .order_by(Invoice.id.desc()).limit(1)).first()
        if last:
            r["last_invoice"] = {"id": last.id, "number": last.number or "(draft)",
                                 "lines": [{"description": ln.description, "quantity": _s(ln.quantity),
                                            "unit": ln.unit, "unit_price": _s(ln.unit_price)} for ln in last.lines]}
        out.append(r)
    return {"clients": out, "count": len(rows)}


def t_list_invoices(s: Session, status: str = "", year: int | None = None, client_id: int | None = None,
                    limit: int = 20) -> dict:
    q = select(Invoice).order_by(Invoice.id.desc())
    if status == "open":
        rows = services.open_invoices(s)
    else:
        if status:
            q = q.where(Invoice.status == status)
        if year:
            q = q.where(Invoice.year == int(year))
        rows = list(s.scalars(q))
    if client_id:
        rows = [i for i in rows if i.client_id == int(client_id)]
    return {"invoices": [_invoice_row(i) for i in rows[:min(int(limit or 20), 50)]], "count": len(rows)}


def t_get_invoice(s: Session, invoice_id: int) -> dict:
    i = _get(s, Invoice, invoice_id, "invoice")
    r = _invoice_row(i)
    r["lines"] = [{"description": ln.description, "quantity": _s(ln.quantity), "unit": ln.unit,
                   "unit_price": _s(ln.unit_price), "amount": _s(ln.line_net)} for ln in i.lines]
    r["vat_note"] = i.vat_note or None
    r["payments"] = [{"date": str(p.date), "amount": _s(p.amount)} for p in i.payments]
    return r


def t_list_bank(s: Session, status: str = "open", query: str = "", limit: int = 20) -> dict:
    q = select(Transaction).order_by(Transaction.booking_date.desc(), Transaction.id.desc())
    if status and status != "all":
        q = q.where(Transaction.status == status)
    rows = list(s.scalars(q))
    if query:
        ql = query.lower()
        rows = [t for t in rows if ql in f"{t.counterparty} {t.purpose} {t.amount}".lower()]
    return {"transactions": [_tx_row(t) for t in rows[:min(int(limit or 20), 50)]], "count": len(rows)}


def t_list_expenses(s: Session, year: int | None = None, query: str = "", limit: int = 20) -> dict:
    q = select(Expense).where(Expense.voided.is_(False)).order_by(Expense.date.desc())
    rows = list(s.scalars(q))
    if year:
        rows = [e for e in rows if e.date.year == int(year)]
    if query:
        ql = query.lower()
        rows = [e for e in rows if ql in f"{e.vendor} {e.description} {e.category}".lower()]
    return {"expenses": [{"id": e.id, "date": str(e.date), "vendor": e.vendor, "description": e.description,
                          "gross_eur": _s(e.gross_eur), "category": e.category, "has_receipt": bool(e.receipts)}
                         for e in rows[:min(int(limit or 20), 50)]], "count": len(rows)}


def t_categories(s: Session) -> dict:
    return {"categories": [{"key": c.key, "label": c.label, "hint": c.hint} for c in catalog.CATEGORIES],
            "payment_methods": list(bookkeeping.PAYMENT_METHODS)}


def t_report(s: Session, kind: str, year: int | None = None) -> dict:
    year = int(year or date.today().year)
    st = services.get_settings(s)
    if kind == "euer":
        eu = ledger.euer(s, year)
        return {"year": year, "income_total": _s(eu.income_total), "expense_total": _s(eu.expense_total),
                "profit": _s(eu.profit),
                "lines": {f"Kz {kz} {catalog.EUER_LINES[kz].label}" if kz in catalog.EUER_LINES else kz: _s(v)
                          for kz, v in eu.lines.items() if v}}
    if kind == "tax":
        eu = ledger.euer(s, year)
        ty = bookkeeping.tax_year(s, year)
        est = taxcalc.estimate(year, eu.profit, ty.sonderausgaben, st.church_tax, st.business_type == "gewerbe",
                               st.gewerbesteuer_hebesatz)
        return {"year": year, "profit_so_far": _s(eu.profit), "taxable_income": _s(est.zve),
                "income_tax": _s(est.income_tax), "soli": _s(est.soli), "church": _s(est.church),
                "gewerbesteuer": _s(est.gewerbesteuer), "total": _s(est.total),
                "already_paid": _s(ledger.tax_payments(s, year)),
                "note": "Estimate for the profit booked so far, single person. Not a tax assessment."}
    if kind == "kleinunternehmer":
        ku = ledger.ku_status(s, year)
        return {"year": year, "turnover_previous_year": _s(ku.prior_total), "limit_previous_year": "25000",
                "turnover_this_year": _s(ku.current_total), "limit_this_year": "100000",
                "ok": ku.prior_ok and ku.current_ok, "youth_welfare_share_previous_year_pct": _s(ku.youth_share_prior)}
    if kind == "ustva":
        u = ledger.ustva(s, date(year, 1, 1), date(year, 12, 31))
        return {"year": year, "kleinunternehmer": u.kleinunternehmer,
                "kz": {k: _s(v) for k, v in u.kz.items() if v}}
    raise BusinessError("kind must be one of: euer, tax, kleinunternehmer, ustva.")


# ---------- proposal tools ----------

def _proposal(conv: Conversation, kind: str, title: str, rows, data, notes=()) -> dict:
    p = Proposal(id=uuid.uuid4().hex[:10], kind=kind, title=title, rows=list(rows), data=data, notes=list(notes))
    conv.proposals[p.id] = p
    conv.display.append({"role": "proposal", "id": p.id})
    return {"proposal_id": p.id, "status": "shown to the user as a card, NOT saved yet",
            "next": "Tell the user briefly what you prepared and that they can press Apply."}


def _lines(raw) -> list[dict]:
    if not isinstance(raw, list) or not raw:
        raise BusinessError("'lines' must be a non-empty list of {description, quantity, unit, unit_price}.")
    out = []
    for i, ln in enumerate(raw, 1):
        if not isinstance(ln, dict) or not str(ln.get("description", "")).strip():
            raise BusinessError(f"Line {i} needs a description.")
        qty = _dec(ln.get("quantity", 1), f"lines[{i}].quantity")
        price = _dec(ln.get("unit_price"), f"lines[{i}].unit_price")
        if qty <= 0 or price < 0:
            raise BusinessError(f"Line {i}: quantity must be > 0 and price ≥ 0.")
        out.append({"description": str(ln["description"]).strip(), "quantity": qty,
                    "unit": str(ln.get("unit") or "").strip(), "unit_price": price})
    return out


def p_invoice_draft(s: Session, conv: Conversation, client_id, lines, service_from=None, service_to=None,
                    intro_text: str = "") -> dict:
    c = _get(s, Client, client_id, "client")
    ls = _lines(lines)
    sf, stt = _date(service_from, "service_from", False), _date(service_to, "service_to", False)
    if sf and stt and stt < sf:
        raise BusinessError("service_to is before service_from.")
    st = services.get_settings(s)
    d = vat.decide(seller_kleinunternehmer=services.is_kleinunternehmer(s, date.today().year),
                   seller_vat_id=st.vat_id, client=c)
    _, _, gross = money.totals([money.line_net(l["quantity"], l["unit_price"]) for l in ls], d.rate)
    rows = [("Client", c.name)]
    if sf:
        rows.append(("Service", f"{sf:%d.%m.%Y}" + (f" – {stt:%d.%m.%Y}" if stt and stt != sf else "")))
    for l in ls:
        rows.append((l["description"], f"{money.fmt_qty(l['quantity'])} {l['unit']} × {money.fmt_eur(l['unit_price'])}"
                                       f" = {money.fmt_eur(money.line_net(l['quantity'], l['unit_price']))}"))
    rows.append(("VAT", d.label))
    rows.append(("Total", money.fmt_eur(gross)))
    data = {"client_id": c.id, "lines": [{**l, "quantity": str(l["quantity"]), "unit_price": str(l["unit_price"])}
                                         for l in ls],
            "service_from": sf.isoformat() if sf else None, "service_to": stt.isoformat() if stt else None,
            "intro_text": (intro_text or "").strip()}
    notes = list(d.warnings) + ["Creates a draft only. You check it and issue it yourself."]
    return _proposal(conv, "invoice_draft", "New invoice draft", rows, data, notes)


def p_client(s: Session, conv: Conversation, name, street="", postal_code="", city="", country="DE",
             is_business=True, vat_id="", email="", default_hourly_rate=None, exemption="") -> dict:
    name = str(name or "").strip()
    if not name:
        raise BusinessError("name is required.")
    country = str(country or "DE").strip().upper()
    if len(country) != 2:
        raise BusinessError("country must be a 2-letter code like DE.")
    exemption = {"4_25": "4_25", "§4 nr. 25": "4_25", "4_21": "4_21"}.get(str(exemption or "").strip().lower(),
                                                                          str(exemption or "").strip())
    if exemption not in ("", "4_25", "4_21"):
        raise BusinessError("exemption must be '', '4_25' or '4_21'.")
    same = [c for c in s.scalars(select(Client)) if c.name.strip().lower() == name.lower()]
    if same:
        raise BusinessError(f"A client named '{name}' already exists (id {same[0].id}). Use that id.")
    rate = _dec(default_hourly_rate, "default_hourly_rate") if default_hourly_rate not in (None, "") else None
    data = {"name": name, "street": str(street or "").strip(), "postal_code": str(postal_code or "").strip(),
            "city": str(city or "").strip(), "country": country, "is_business": bool(is_business),
            "vat_id": str(vat_id or "").replace(" ", "").upper(), "email": str(email or "").strip(),
            "default_hourly_rate": str(rate) if rate is not None else None, "exemption": exemption}
    rows = [("Name", name), ("Address", ", ".join(x for x in (data["street"], f"{data['postal_code']} {data['city']}".strip(), country) if x)),
            ("Type", "Business" if data["is_business"] else "Private person")]
    if data["vat_id"]:
        rows.append(("VAT ID", data["vat_id"]))
    if rate is not None:
        rows.append(("Hourly rate", money.fmt_eur(rate)))
    if exemption:
        rows.append(("Exemption", vat.LABELS[f"exempt_{exemption}"]))
    notes = ["Exemptions need evidence (e.g. § 75 SGB VIII recognition). Add it on the client page."] if exemption else []
    return _proposal(conv, "client", "New client", rows, data, notes)


def p_expense(s: Session, conv: Conversation, date_, vendor, gross_eur, category, description="", vat_rate=0,
              payment_method="bank", transaction_id=None, business_share=100) -> dict:
    d = _date(date_, "date")
    gross = _dec(gross_eur, "gross_eur")
    rate = _dec(vat_rate if vat_rate not in (None, "") else 0, "vat_rate")
    if rate not in (Decimal(0), Decimal(7), Decimal(19)):
        raise BusinessError("vat_rate must be 0, 7 or 19.")
    share = _dec(business_share if business_share not in (None, "") else 100, "business_share")
    if payment_method not in bookkeeping.PAYMENT_METHODS:
        raise BusinessError(f"payment_method must be one of {list(bookkeeping.PAYMENT_METHODS)}.")
    data = {"date": d, "vendor": str(vendor or "").strip(), "description": str(description or "").strip(),
            "category": category, "payment_method": payment_method, "gross_eur": gross, "vat_rate": rate,
            "vat_eur": bookkeeping.vat_from_gross(gross, rate), "business_share": share, "currency": "EUR"}
    notes = []
    if transaction_id not in (None, ""):
        tx = _get(s, Transaction, transaction_id, "bank transaction")
        if tx.status != "open":
            raise BusinessError(f"Bank transaction {tx.id} is already assigned ({tx.status}).")
        if tx.amount >= 0:
            raise BusinessError(f"Bank transaction {tx.id} is incoming money, not an expense.")
        if -tx.amount != gross:
            notes.append(f"The bank shows {money.fmt_eur(-tx.amount)}, the expense says {money.fmt_eur(gross)}.")
        data["transaction_id"] = tx.id
        data["source"] = "bank"
    bookkeeping._validate_expense(dict(data))
    cat = catalog.CATEGORY_BY_KEY[category]
    rows = [("Date", f"{d:%d.%m.%Y}"), ("Vendor", data["vendor"]), ("Category", cat.label),
            ("Amount", f"{money.fmt_eur(gross)} (VAT {money.fmt_qty(rate)} %: {money.fmt_eur(data['vat_eur'])})")]
    if data["description"]:
        rows.insert(2, ("What", data["description"]))
    if share != 100:
        rows.append(("Business share", f"{money.fmt_qty(share)} %"))
    if data.get("transaction_id"):
        rows.append(("Bank", f"linked to the payment of {d:%d.%m.%Y}"))
    notes.append("Add the receipt afterwards on the expense page.")
    stored = {k: (v.isoformat() if isinstance(v, date) else str(v) if isinstance(v, Decimal) else v)
              for k, v in data.items()}
    return _proposal(conv, "expense", "New expense", rows, stored, notes)


TX_LABELS = {"private": "Private", "transfer": "Own transfer", "ignored": "Ignore", "tax": "Tax office payment"}


def p_bank_status(s: Session, conv: Conversation, transaction_id, status, tax_kind="", tax_year=None) -> dict:
    tx = _get(s, Transaction, transaction_id, "bank transaction")
    if tx.status != "open":
        raise BusinessError(f"Transaction {tx.id} is already marked as {tx.status}.")
    if status not in TX_LABELS:
        raise BusinessError("status must be private, transfer, ignored or tax. For invoices use propose_bank_match, "
                            "for expenses propose_expense, for other income propose_other_income.")
    if status == "tax" and tax_kind not in ("est", "ust", "other"):
        raise BusinessError("For status 'tax', tax_kind must be est (income tax), ust (VAT) or other.")
    data = {"transaction_id": tx.id, "status": status, "tax_kind": tax_kind if status == "tax" else "",
            "tax_year": int(tax_year) if status == "tax" and tax_year else (tx.booking_date.year if status == "tax" else None)}
    rows = [("Payment", f"{tx.booking_date:%d.%m.%Y} · {tx.counterparty} · {money.fmt_eur(tx.amount)}"),
            ("Mark as", TX_LABELS[status] + (f" ({ {'est': 'income tax', 'ust': 'VAT', 'other': 'other'}[tax_kind]} {data['tax_year']})"
                                            if status == "tax" else ""))]
    return _proposal(conv, "bank_status", "Bank transaction", rows, data)


def p_bank_match(s: Session, conv: Conversation, transaction_id, invoice_ids) -> dict:
    tx = _get(s, Transaction, transaction_id, "bank transaction")
    if tx.status != "open" or tx.amount <= 0:
        raise BusinessError(f"Transaction {tx.id} is not open incoming money.")
    if not isinstance(invoice_ids, list):
        invoice_ids = [invoice_ids]
    invs = [_get(s, Invoice, i, "invoice") for i in invoice_ids]
    for i in invs:
        if i.status != "issued":
            raise BusinessError(f"Invoice {i.number or i.id} is {i.status}, it cannot receive a payment.")
    total = sum((i.outstanding for i in invs), Decimal(0))
    rows = [("Payment", f"{tx.booking_date:%d.%m.%Y} · {tx.counterparty} · {money.fmt_eur(tx.amount)}"),
            ("Invoice(s)", ", ".join(f"{i.number} ({money.fmt_eur(i.outstanding)})" for i in invs))]
    notes = [] if total == tx.amount else [f"Amounts differ: invoices {money.fmt_eur(total)}, payment {money.fmt_eur(tx.amount)}."]
    return _proposal(conv, "bank_match", "Assign payment to invoice", rows,
                     {"transaction_id": tx.id, "invoice_ids": [i.id for i in invs]}, notes)


def p_other_income(s: Session, conv: Conversation, date_, gross_eur, description, client_id=None,
                   transaction_id=None) -> dict:
    d = _date(date_, "date")
    gross = _dec(gross_eur, "gross_eur")
    if gross <= 0:
        raise BusinessError("gross_eur must be positive.")
    c = _get(s, Client, client_id, "client") if client_id not in (None, "") else None
    if c and c.exemption:
        treatment = f"exempt_{c.exemption}"
    elif services.is_kleinunternehmer(s, d.year):
        treatment = "kleinunternehmer"
    else:
        treatment = "domestic"
    data = {"date": d.isoformat(), "gross_eur": str(gross), "description": str(description or "").strip(),
            "client_id": c.id if c else None, "vat_treatment": treatment}
    if transaction_id not in (None, ""):
        tx = _get(s, Transaction, transaction_id, "bank transaction")
        if tx.status != "open" or tx.amount <= 0:
            raise BusinessError(f"Transaction {tx.id} is not open incoming money.")
        data["transaction_id"] = tx.id
    rows = [("Date", f"{d:%d.%m.%Y}"), ("From", c.name if c else "–"), ("What", data["description"] or "–"),
            ("Amount", money.fmt_eur(gross)), ("VAT", vat.LABELS.get(treatment, treatment))]
    return _proposal(conv, "other_income", "Income without invoice", rows, data)


# ---------- apply ----------

def apply(s: Session, p: Proposal) -> str:
    """Execute a proposal with the normal service functions. Returns the URL of the result."""
    if p.status != "pending":
        raise BusinessError("This suggestion was already handled.")
    d = p.data
    if p.kind == "invoice_draft":
        c = _get(s, Client, d["client_id"], "client")
        inv = services.new_draft(s, c)
        header = {"client_id": c.id, "language": inv.language, "issue_date": None,
                  "service_from": _date(d["service_from"], "", False), "service_to": _date(d["service_to"], "", False),
                  "intro_text": d.get("intro_text", ""), "vat_override": ""}
        lines = [{**l, "quantity": Decimal(l["quantity"]), "unit_price": Decimal(l["unit_price"])} for l in d["lines"]]
        services.update_draft(s, inv, header, lines)
        url = f"/invoices/{inv.id}"
    elif p.kind == "client":
        data = dict(d)
        data["default_hourly_rate"] = Decimal(d["default_hourly_rate"]) if d.get("default_hourly_rate") else None
        c = services.save_client(s, None, data)
        url = f"/clients/{c.id}"
    elif p.kind == "expense":
        data = dict(d)
        data["date"] = date.fromisoformat(d["date"])
        for k in ("gross_eur", "vat_rate", "vat_eur", "business_share"):
            data[k] = Decimal(d[k])
        if data.get("transaction_id") and s.get(Transaction, data["transaction_id"]).status != "open":
            raise BusinessError("The bank transaction was assigned in the meantime.")
        e = bookkeeping.save_expense(s, None, data)
        url = f"/expenses/{e.id}"
    elif p.kind == "bank_status":
        from . import bank
        tx = s.get(Transaction, d["transaction_id"])
        bank.set_status(s, tx, d["status"], d.get("tax_kind", ""), d.get("tax_year"), "via assistant")
        url = f"/bank?status={d['status']}"
    elif p.kind == "bank_match":
        from . import bank
        tx = s.get(Transaction, d["transaction_id"])
        bank.apply_match(s, tx, [s.get(Invoice, i) for i in d["invoice_ids"]])
        url = "/bank?status=invoice"
    elif p.kind == "other_income":
        data = dict(d)
        data["date"] = date.fromisoformat(d["date"])
        data["gross_eur"] = Decimal(d["gross_eur"])
        data["vat_eur"] = None
        bookkeeping.save_other_income(s, None, data)
        url = "/income"
    else:
        raise BusinessError("Unknown suggestion.")
    services.audit(s, "assistant", None, "proposal_applied", kind=p.kind, data=d)
    return url


# ---------- tool table ----------

def _schema(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": required}


STR, NUM, INT, BOOL = {"type": "string"}, {"type": "number"}, {"type": "integer"}, {"type": "boolean"}
LINE = {"type": "object", "properties": {"description": STR, "quantity": NUM, "unit": STR, "unit_price": NUM},
        "required": ["description", "quantity", "unit_price"]}

TOOLS = [
    ("overview", "Today's date and a summary of the business (VAT status, open invoices, bank to-dos).",
     _schema({}, []), t_overview, False),
    ("find_clients", "Find clients by name. Returns ids, hourly rate, VAT exemption and the lines of their last "
     "invoice (useful for 'same as last time'). Empty query lists all clients.",
     _schema({"query": STR}, []), t_find_clients, False),
    ("list_invoices", "List invoices. status: draft, issued, paid, cancelled, or 'open' (issued and unpaid).",
     _schema({"status": STR, "year": INT, "client_id": INT, "limit": INT}, []), t_list_invoices, False),
    ("get_invoice", "One invoice with lines and payments.", _schema({"invoice_id": INT}, ["invoice_id"]),
     t_get_invoice, False),
    ("list_bank_transactions", "Bank transactions. status 'open' = not assigned yet (default), or 'all'. "
     "Negative amount = money out.", _schema({"status": STR, "query": STR, "limit": INT}, []), t_list_bank, False),
    ("list_expenses", "Booked expenses.", _schema({"year": INT, "query": STR, "limit": INT}, []), t_list_expenses, False),
    ("expense_categories", "The allowed expense category keys and payment methods.", _schema({}, []),
     t_categories, False),
    ("report", "Numbers for a year. kind: euer (profit), tax (income tax estimate), kleinunternehmer (limits), ustva.",
     _schema({"kind": STR, "year": INT}, ["kind"]), t_report, False),
    ("propose_invoice_draft", "Prepare a new invoice DRAFT for an existing client (client_id from find_clients). "
     "Dates YYYY-MM-DD. The user must press Apply; it can never be issued by you.",
     _schema({"client_id": INT, "lines": {"type": "array", "items": LINE}, "service_from": STR, "service_to": STR,
              "intro_text": STR}, ["client_id", "lines"]), p_invoice_draft, True),
    ("propose_client", "Prepare a new client. country = 2-letter code. exemption: '' , '4_25' (youth welfare "
     "organisation, § 4 Nr. 25) or '4_21' (school).",
     _schema({"name": STR, "street": STR, "postal_code": STR, "city": STR, "country": STR, "is_business": BOOL,
              "vat_id": STR, "email": STR, "default_hourly_rate": NUM, "exemption": STR}, ["name"]), p_client, True),
    ("propose_expense", "Prepare a business expense. category = key from expense_categories. gross_eur = total "
     "paid incl. VAT. vat_rate 0, 7 or 19 (0 if the receipt shows no VAT). transaction_id links a bank payment.",
     _schema({"date": STR, "vendor": STR, "description": STR, "gross_eur": NUM, "vat_rate": NUM, "category": STR,
              "payment_method": STR, "transaction_id": INT, "business_share": NUM},
             ["date", "vendor", "gross_eur", "category"]), p_expense, True),
    ("propose_bank_status", "Mark an open bank transaction as private, transfer (own account), ignored, or tax "
     "(payment to/refund from the Finanzamt; tax_kind est=income tax, ust=VAT).",
     _schema({"transaction_id": INT, "status": STR, "tax_kind": STR, "tax_year": INT},
             ["transaction_id", "status"]), p_bank_status, True),
    ("propose_bank_match", "Assign incoming bank money to one or more open invoices.",
     _schema({"transaction_id": INT, "invoice_ids": {"type": "array", "items": INT}},
             ["transaction_id", "invoice_ids"]), p_bank_match, True),
    ("propose_other_income", "Book income that has no invoice in this app (e.g. Übungsleiterhonorar).",
     _schema({"date": STR, "gross_eur": NUM, "description": STR, "client_id": INT, "transaction_id": INT},
             ["date", "gross_eur", "description"]), p_other_income, True),
]
TOOL_BY_NAME = {t[0]: t for t in TOOLS}


def ollama_tools() -> list[dict]:
    return [{"type": "function", "function": {"name": n, "description": d, "parameters": p}}
            for n, d, p, _, _ in TOOLS]


def run_tool(s: Session, conv: Conversation, name: str, args) -> str:
    if name not in TOOL_BY_NAME:
        return json.dumps({"error": f"Unknown tool {name}."})
    if isinstance(args, str):
        try:
            args = json.loads(args or "{}")
        except json.JSONDecodeError:
            return json.dumps({"error": "Arguments were not valid JSON."})
    args = {k: v for k, v in (args or {}).items() if v is not None}
    _, _, params, fn, is_proposal = TOOL_BY_NAME[name]
    allowed = set(params["properties"])
    unknown = set(args) - allowed
    if unknown:
        return json.dumps({"error": f"Unknown argument(s) {sorted(unknown)}. Allowed: {sorted(allowed)}."})
    if "date" in args:            # 'date' is a Python name; the functions take date_
        args["date_"] = args.pop("date")
    try:
        result = fn(s, conv, **args) if is_proposal else fn(s, **args)
    except BusinessError as e:
        return json.dumps({"error": str(e), "hint": "Fix the arguments and try again, or ask the user."})
    except TypeError as e:
        return json.dumps({"error": f"Missing or wrong arguments: {e}"})
    return json.dumps(result, ensure_ascii=False, default=str)


# ---------- the chat loop ----------

SYSTEM = """You are the bookkeeping assistant inside "Büro", a local invoicing and bookkeeping app of a \
freelancer in Berlin, Germany. Today is {today}. The user is {name}{ku}.

How you work:
- Always reply in the language the user writes in (often Turkish or German). Be short and friendly.
- Use the tools to look things up. Never guess ids, amounts or dates: find them with a tool first.
- To change anything, call a propose_* tool. A proposal is NOT saved: it appears as a card and the user \
presses Apply. After proposing, say in one sentence what you prepared.
- You cannot issue, cancel, delete or send invoices. If asked, tell the user to do it on the invoice page.
- Money is in EUR. Dates are YYYY-MM-DD. "last month" means the previous calendar month.
- For invoice lines use the client's hourly rate or the lines of their last invoice unless the user says otherwise.
- If something is unclear (which client, which amount), ask one short question instead of guessing.
- You are not a tax advisor; for legal questions give a short hint and recommend checking with the Finanzamt \
or a Steuerberater."""


def _post(url: str, payload: dict) -> dict:
    try:
        r = httpx.post(url.rstrip("/") + "/api/chat", json=payload, timeout=TIMEOUT)
    except httpx.ConnectError:
        raise AssistantError("Ollama is not running. Open the Ollama app (or run 'ollama serve') and try again.") from None
    except httpx.TimeoutException:
        raise AssistantError("The model took too long to answer. Try a smaller model in Settings.") from None
    if r.status_code == 404:
        raise AssistantError(f"Model not found. In Terminal run: ollama pull {payload['model']}")
    if r.status_code >= 400:
        raise AssistantError(f"Ollama error {r.status_code}: {r.text[:300]}")
    return r.json()


def status(url: str, model: str) -> dict:
    """Is Ollama reachable and is the model installed?"""
    try:
        r = httpx.get(url.rstrip("/") + "/api/tags", timeout=3)
        names = [m.get("name", "") for m in r.json().get("models", [])]
    except (httpx.HTTPError, ValueError):
        return {"running": False, "installed": False, "models": []}
    wanted = model if ":" in model else model + ":latest"
    return {"running": True, "installed": wanted in names, "models": names}


THINK_RE = re.compile(r"<think>.*?</think>", re.S)


def chat(s: Session, conv: Conversation, user_text: str, url: str, model: str) -> str:
    user_text = (user_text or "").strip()
    if not user_text:
        raise BusinessError("Write a message first.")
    st = services.get_settings(s)
    ku = services.is_kleinunternehmer(s, date.today().year)
    system = SYSTEM.format(today=date.today().isoformat(), name=st.name or "the freelancer",
                           ku=" (Kleinunternehmer, § 19 UStG)" if ku else " (charges VAT)")
    conv.messages.append({"role": "user", "content": user_text})
    conv.display.append({"role": "user", "text": user_text})
    reply = ""
    for _ in range(MAX_ROUNDS):
        res = _post(url, {"model": model, "stream": False, "tools": ollama_tools(),
                          "messages": [{"role": "system", "content": system}, *conv.messages],
                          "options": {"num_ctx": NUM_CTX, "temperature": 0.2}})
        msg = res.get("message") or {}
        calls = msg.get("tool_calls") or []
        content = THINK_RE.sub("", msg.get("content") or "").strip()
        conv.messages.append({"role": "assistant", "content": content, **({"tool_calls": calls} if calls else {})})
        if not calls:
            reply = content
            break
        for call in calls:
            fn = call.get("function") or {}
            name = fn.get("name", "")
            result = run_tool(s, conv, name, fn.get("arguments") or {})
            conv.messages.append({"role": "tool", "tool_name": name, "content": result})
    else:
        reply = "I could not finish this in a few steps. Please ask in a simpler way."
    s.rollback()  # the assistant itself never writes; only apply() does, in its own request
    if not reply:
        pending = [p for p in conv.proposals.values() if p.status == "pending"]
        reply = "I prepared it below. Check and press Apply." if pending else "…"
    conv.display.append({"role": "assistant", "text": reply, "ts": datetime.now().strftime("%H:%M")})
    return reply
