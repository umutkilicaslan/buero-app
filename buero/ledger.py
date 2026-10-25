"""Turns invoices, payments, expenses and bank data into EÜR, UStVA, ZM and status figures.

Principles
- EÜR is cash based (§ 11 EStG): income on the payment date, expenses on the date paid.
- Kleinunternehmer years: no Vorsteuer, the gross expense is the cost.
- Regular years: net amounts in the expense lines, Vorsteuer in Kz 185, VAT received in Kz 140.
- § 13b purchases (reverse charge): the VAT is owed and, in regular years, deducted at the same time,
  so it does not change profit. It is shown in the UStVA only.
"""
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_DOWN, Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import catalog, money, services
from .models import Expense, Invoice, OtherIncome, Payment, TaxYear, Transaction

D = Decimal
ZERO = D("0.00")
FOREIGN = ("reverse_charge", "non_eu")
EXEMPT = ("exempt_4_25", "exempt_4_21")


# ---------- income ----------

@dataclass
class IncomeItem:
    date: date
    gross: Decimal
    vat: Decimal
    treatment: str
    label: str
    client: str
    link: str

    @property
    def net(self) -> Decimal:
        return self.gross - self.vat


def income_items(s: Session, start: date, end: date) -> list[IncomeItem]:
    items = []
    q = select(Payment).join(Invoice).where(Payment.date >= start, Payment.date <= end)
    for p in s.scalars(q):
        inv = p.invoice
        vat = money.round_cents(p.amount * inv.vat_total / inv.gross_total) if inv.gross_total else ZERO
        items.append(IncomeItem(p.date, p.amount, vat, inv.vat_treatment, f"Invoice {inv.number}",
                                inv.client.name, f"/invoices/{inv.id}"))
    q = select(OtherIncome).where(OtherIncome.date >= start, OtherIncome.date <= end, OtherIncome.voided.is_(False))
    for o in s.scalars(q):
        items.append(IncomeItem(o.date, o.gross_eur, o.vat_eur, o.vat_treatment, o.description or "Other income",
                                o.client.name if o.client else "", f"/income/{o.id}"))
    return sorted(items, key=lambda i: i.date)


# ---------- expenses ----------

@dataclass
class ExpenseEffect:
    """What one expense contributes to one year."""
    kz: str
    amount: Decimal
    vorsteuer: Decimal = ZERO
    nondeductible: Decimal = ZERO


def expense_base(e: Expense, kleinunternehmer: bool) -> tuple[Decimal, Decimal]:
    """(cost, vorsteuer), both already reduced to the business share."""
    share = e.business_share / 100
    if e.reverse_charge or kleinunternehmer:
        # § 13b invoices carry no VAT; a Kleinunternehmer cannot deduct VAT, so gross is the cost.
        return money.round_cents(e.gross_eur * share), ZERO
    return money.round_cents(e.net_eur * share), money.round_cents(e.vat_eur * share)


ASSET_CATEGORIES = ("asset", "asset_software")


def is_gwg(e: Expense) -> bool:
    """§ 6 Abs. 2 EStG: the 800 € limit is always checked on the net price."""
    return e.category in ASSET_CATEGORIES and e.net_eur <= catalog.GWG_LIMIT


def depreciation_schedule(e: Expense, kleinunternehmer: bool) -> dict[int, Decimal]:
    """Linear AfA, pro rata by month in the first year (§ 7 Abs. 1 EStG). 1-year life = full in year."""
    cost, _ = expense_base(e, kleinunternehmer)
    life = e.useful_life_years or 1
    if life <= 1:
        return {e.date.year: cost}
    yearly = cost / life
    months_first = 13 - e.date.month
    schedule, remaining, year = {}, cost, e.date.year
    first = money.round_cents(yearly * months_first / 12)
    schedule[year] = first
    remaining -= first
    while remaining > 0:
        year += 1
        amount = min(money.round_cents(yearly), remaining)
        schedule[year] = amount
        remaining -= amount
    return schedule


def expense_effects(s: Session, e: Expense, year: int) -> list[ExpenseEffect]:
    if e.voided:
        return []
    ku = services.is_kleinunternehmer(s, e.date.year)
    cost, vst = expense_base(e, ku)
    vst_effect = vst if e.date.year == year else ZERO
    if e.category in ASSET_CATEGORIES:
        if is_gwg(e):
            return [ExpenseEffect("132", cost, vst)] if e.date.year == year else []
        amount = depreciation_schedule(e, ku).get(year, ZERO)
        kz = "131" if e.category == "asset_software" else "130"
        effects = [ExpenseEffect(kz, amount)] if amount else []
        if vst_effect:
            effects.append(ExpenseEffect("185", ZERO, vst_effect))
        return effects
    if e.date.year != year:
        return []
    cat = catalog.CATEGORY_BY_KEY.get(e.category, catalog.CATEGORY_BY_KEY["other"])
    if e.category == "hospitality":
        deductible = money.round_cents(cost * D("0.7"))
        return [ExpenseEffect("175", deductible, vst, cost - deductible)]
    return [ExpenseEffect(cat.kz, cost, vst)]


def expenses_in(s: Session, start: date, end: date) -> list[Expense]:
    return list(s.scalars(select(Expense).where(Expense.date >= start, Expense.date <= end,
                                                Expense.voided.is_(False)).order_by(Expense.date)))


def assets_active_in(s: Session, year: int) -> list[Expense]:
    q = select(Expense).where(Expense.category.in_(ASSET_CATEGORIES), Expense.voided.is_(False),
                              Expense.date <= date(year, 12, 31))
    return [e for e in s.scalars(q) if not is_gwg(e)]


# ---------- EÜR ----------

@dataclass
class Euer:
    year: int
    kleinunternehmer: bool
    lines: dict[str, Decimal] = field(default_factory=lambda: defaultdict(lambda: ZERO))
    income_total: Decimal = ZERO
    expense_total: Decimal = ZERO
    nondeductible: Decimal = ZERO
    homeoffice_days: int = 0

    @property
    def profit(self) -> Decimal:
        return self.income_total - self.expense_total

    def rows(self, section: str):
        for kz, line in catalog.EUER_LINES.items():
            if line.section == section and self.lines.get(kz):
                yield line, self.lines[kz]


def euer(s: Session, year: int, until: date | None = None) -> Euer:
    start, end = date(year, 1, 1), min(date(year, 12, 31), until or date(year, 12, 31))
    ku = services.is_kleinunternehmer(s, year)
    r = Euer(year, ku)
    L = r.lines
    for it in income_items(s, start, end):
        if ku:
            L["111"] += it.gross
            if it.treatment in EXEMPT + FOREIGN:
                L["119"] += it.gross
        elif it.treatment == "domestic":
            L["112"] += it.net
            L["140"] += it.vat
        elif it.treatment == "kleinunternehmer":  # invoice from a Kleinunternehmer year, paid later
            L["111"] += it.gross
        else:
            L["103"] += it.gross
    # VAT refunds and payments to the Finanzamt
    for tx in s.scalars(select(Transaction).where(Transaction.status == "tax", Transaction.tax_kind == "ust",
                                                  Transaction.booking_date >= start, Transaction.booking_date <= end)):
        if tx.amount > 0:
            L["141"] += tx.amount
        else:
            L["186"] += -tx.amount

    candidates = {e.id: e for e in expenses_in(s, start, end)}
    candidates.update({e.id: e for e in assets_active_in(s, year) if e.date <= end})
    for e in candidates.values():
        for eff in expense_effects(s, e, year):
            L[eff.kz] += eff.amount
            if eff.vorsteuer:
                L["185"] += eff.vorsteuer
            if eff.nondeductible:
                L["165"] += eff.nondeductible
    ty = s.get(TaxYear, year)
    if ty and ty.homeoffice_days:
        r.homeoffice_days = min(ty.homeoffice_days, catalog.HOMEOFFICE_MAX_DAYS)
        L["163"] += catalog.HOMEOFFICE_PER_DAY * r.homeoffice_days
    r.income_total = sum((v for k, v in L.items() if catalog.EUER_LINES[k].section == "income"), ZERO)
    r.expense_total = sum((v for k, v in L.items() if catalog.EUER_LINES[k].section == "expense"), ZERO)
    r.nondeductible = L.get("165", ZERO)
    return r


# ---------- Kleinunternehmer check (§ 19 UStG) ----------

KU_PRIOR_LIMIT = D("25000")
KU_CURRENT_LIMIT = D("100000")


@dataclass
class KuStatus:
    year: int
    prior_total: Decimal
    current_total: Decimal
    youth_share_prior: Decimal  # share of prior-year income from § 4 Nr. 25 clients (in %)

    @property
    def prior_ok(self) -> bool:
        return self.prior_total <= KU_PRIOR_LIMIT

    @property
    def current_ok(self) -> bool:
        return self.current_total <= KU_CURRENT_LIMIT

    @property
    def current_pct(self) -> Decimal:
        return (self.current_total / KU_CURRENT_LIMIT * 100).quantize(D("1"))

    @property
    def next_year_pct(self) -> Decimal:
        return (self.current_total / KU_PRIOR_LIMIT * 100).quantize(D("1"))


def gesamtumsatz(s: Session, year: int) -> Decimal:
    """Received amounts (incl. any VAT) of domestic taxable turnover; exempt § 4 Nr. 8–29 and
    foreign (not taxable in Germany) turnover do not count (§ 19 Abs. 2 UStG)."""
    total = ZERO
    for it in income_items(s, date(year, 1, 1), date(year, 12, 31)):
        if it.treatment not in EXEMPT + FOREIGN:
            total += it.gross
    return total


def youth_share(s: Session, year: int) -> Decimal:
    items = income_items(s, date(year, 1, 1), date(year, 12, 31))
    total = sum((i.gross for i in items), ZERO)
    if not total:
        return ZERO
    youth = sum((i.gross for i in items if i.treatment == "exempt_4_25"), ZERO)
    return (youth / total * 100).quantize(D("0.1"))


def ku_status(s: Session, year: int) -> KuStatus:
    return KuStatus(year, gesamtumsatz(s, year - 1), gesamtumsatz(s, year), youth_share(s, year - 1))


# ---------- UStVA ----------

def whole_euros(x: Decimal) -> Decimal:
    return x.to_integral_value(ROUND_DOWN)


@dataclass
class Ustva:
    start: date
    end: date
    kleinunternehmer: bool
    ist: bool
    kz: dict[str, Decimal]
    details: list[tuple[str, str, Decimal]]

    @property
    def empty(self) -> bool:
        return not any(self.kz.values())


def ustva(s: Session, start: date, end: date) -> Ustva:
    st = services.get_settings(s)
    ku = services.is_kleinunternehmer(s, start.year)
    kz = defaultdict(lambda: ZERO)
    details = []
    if not ku:
        if st.ist_versteuerung:
            for it in income_items(s, start, end):
                _add_sale(kz, it.treatment, it.net, it.vat)
                details.append((it.date.isoformat(), it.label, it.net))
        else:
            q = select(Invoice).where(Invoice.issue_date >= start, Invoice.issue_date <= end,
                                      Invoice.number.is_not(None))
            for inv in s.scalars(q):
                _add_sale(kz, inv.vat_treatment, inv.net_total, inv.vat_total)
                details.append((inv.issue_date.isoformat(), f"Invoice {inv.number}", inv.net_total))
            for o in s.scalars(select(OtherIncome).where(OtherIncome.date >= start, OtherIncome.date <= end,
                                                         OtherIncome.voided.is_(False))):
                _add_sale(kz, o.vat_treatment, o.net_eur, o.vat_eur)
    # Reverse-charge purchases are reported in UStVA under the invoice date (Soll), also by Kleinunternehmer
    for e in expenses_in(s, start, end):
        share = e.business_share / 100
        if e.reverse_charge:
            base = e.gross_eur
            tax = money.round_cents(base * D("0.19"))
            if e.reverse_charge == "eu":
                kz["46"] += base
                kz["47"] += tax
            else:
                kz["84"] += base
                kz["85"] += tax
            if not ku:
                kz["67"] += money.round_cents(tax * share)
            details.append((e.date.isoformat(), f"§ 13b purchase: {e.vendor}", base))
        elif not ku and e.vat_eur:
            kz["66"] += money.round_cents(e.vat_eur * share)
    for k in ("81", "21", "45", "48", "46", "84"):
        if k in kz:
            kz[k] = whole_euros(kz[k])
    kz["81_tax"] = money.round_cents(kz.get("81", ZERO) * D("0.19"))
    kz["83"] = kz["81_tax"] + kz.get("47", ZERO) + kz.get("85", ZERO) - kz.get("66", ZERO) - kz.get("67", ZERO)
    return Ustva(start, end, ku, st.ist_versteuerung, dict(kz), details)


def _add_sale(kz, treatment, net, vat):
    if treatment == "domestic":
        kz["81"] += net
    elif treatment == "reverse_charge":
        kz["21"] += net
    elif treatment == "non_eu":
        kz["45"] += net
    elif treatment in EXEMPT:
        kz["48"] += net


# ---------- Zusammenfassende Meldung ----------

def zm(s: Session, start: date, end: date) -> list[dict]:
    """EU reverse-charge services by client VAT ID, in the period the service was performed."""
    rows: dict[str, dict] = {}
    q = select(Invoice).where(Invoice.vat_treatment == "reverse_charge", Invoice.number.is_not(None))
    for inv in s.scalars(q):
        when = inv.service_to or inv.service_from or inv.issue_date
        if not (start <= when <= end):
            continue
        import json
        c = json.loads(inv.client_snapshot)
        vat_id = (c.get("vat_id") or "").replace(" ", "")
        key = vat_id or c.get("name", "?")
        row = rows.setdefault(key, {"country": vat_id[:2], "vat_id": vat_id[2:], "name": c.get("name"),
                                    "amount": ZERO, "invoices": []})
        row["amount"] += inv.net_total
        row["invoices"].append(inv.number)
    for r in rows.values():
        r["amount_eur"] = whole_euros(r["amount"])
    return sorted(rows.values(), key=lambda r: r["vat_id"])


# ---------- periods ----------

def periods(year: int, kind: str) -> list[tuple[str, date, date]]:
    import calendar
    if kind == "monthly":
        return [(f"{year}-{m:02d}", date(year, m, 1), date(year, m, calendar.monthrange(year, m)[1]))
                for m in range(1, 13)]
    return [(f"Q{q} {year}", date(year, 3 * q - 2, 1),
             date(year, 3 * q, calendar.monthrange(year, 3 * q)[1])) for q in range(1, 5)]


# ---------- prepayments ----------

def tax_payments(s: Session, year: int, kind: str = "est") -> Decimal:
    """Payments to the Finanzamt assigned to an income tax year (negative bank amounts)."""
    q = select(Transaction).where(Transaction.status == "tax", Transaction.tax_kind == kind,
                                  Transaction.tax_year == year)
    paid = -sum((t.amount for t in s.scalars(q)), ZERO)
    ty = s.get(TaxYear, year)
    if kind == "est" and ty:
        paid += ty.prepayments_other
    return paid
