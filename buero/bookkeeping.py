"""Expenses, receipts and other income."""
import hashlib
import re
from datetime import date
from decimal import Decimal
from pathlib import Path

from sqlalchemy.orm import Session

from . import catalog, config, money
from .models import Expense, OtherIncome, Receipt, TaxYear, Transaction
from .services import BusinessError, audit

EXPENSE_FIELDS = ["date", "vendor", "description", "vendor_invoice_number", "category", "payment_method",
                  "currency", "amount_original", "gross_eur", "vat_rate", "vat_eur", "business_share",
                  "reverse_charge", "useful_life_years", "transaction_id", "source"]
PAYMENT_METHODS = {"bank": "Bank transfer / card (business account)", "cash": "Cash",
                   "private": "Paid privately", "paypal": "PayPal", "card": "Credit card"}
ALLOWED_RECEIPT_TYPES = {".pdf", ".jpg", ".jpeg", ".png", ".heic", ".webp", ".xml", ".gif", ".tif", ".tiff"}
MAX_RECEIPT_BYTES = 25 * 1024 * 1024


def vat_from_gross(gross: Decimal, rate: Decimal) -> Decimal:
    return money.round_cents(gross * rate / (100 + rate)) if rate else Decimal("0.00")


def _validate_expense(data: dict) -> None:
    if not data.get("vendor", "").strip():
        raise BusinessError("Vendor is required.")
    if data.get("category") not in catalog.CATEGORY_BY_KEY:
        raise BusinessError("Choose a category.")
    if data.get("date") is None:
        raise BusinessError("Date is required.")
    if data["date"] > date.today():
        raise BusinessError("Date lies in the future.")
    gross = data.get("gross_eur")
    if gross is None or gross <= 0:
        raise BusinessError("Amount in EUR must be greater than zero.")
    share = data.get("business_share", Decimal("100"))
    if not (0 < share <= 100):
        raise BusinessError("Business share must be between 1 and 100 %.")
    if data.get("reverse_charge") and data.get("vat_eur"):
        raise BusinessError("A reverse-charge invoice (§ 13b) contains no VAT. Set VAT to 0.")
    if data.get("vat_eur", Decimal(0)) >= gross:
        raise BusinessError("VAT cannot be as high as the total.")
    if data.get("currency", "EUR") != "EUR" and not data.get("amount_original"):
        raise BusinessError("Enter the original amount for a foreign-currency expense.")
    if data.get("category") in ("asset", "asset_software"):
        net = gross - data.get("vat_eur", Decimal(0))
        if net > catalog.GWG_LIMIT and not data.get("useful_life_years"):
            raise BusinessError("Assets over 800 € net need a useful life in years (AfA table; computers/software: 1).")


def save_expense(s: Session, expense: Expense | None, data: dict) -> Expense:
    data.setdefault("business_share", Decimal("100"))
    data.setdefault("vat_rate", Decimal("0"))
    if data.get("vat_eur") is None:
        data["vat_eur"] = Decimal("0") if data.get("reverse_charge") else vat_from_gross(data["gross_eur"], data["vat_rate"])
    _validate_expense(data)
    if expense is not None and expense.voided:
        raise BusinessError("A voided expense cannot be changed.")
    is_new = expense is None
    expense = expense or Expense()
    changes = {}
    for f in EXPENSE_FIELDS:
        if f in data and getattr(expense, f, None) != data[f]:
            changes[f] = [getattr(expense, f, None), data[f]]
            setattr(expense, f, data[f])
    s.add(expense)
    s.flush()
    if expense.transaction_id:
        tx = s.get(Transaction, expense.transaction_id)
        tx.status = "expense"
    audit(s, "expense", expense.id, "created" if is_new else "updated", changes=changes)
    return expense


def void_expense(s: Session, expense: Expense, reason: str) -> None:
    if not reason.strip():
        raise BusinessError("Please give a reason for voiding.")
    expense.voided = True
    expense.void_reason = reason
    if expense.transaction_id:
        tx = s.get(Transaction, expense.transaction_id)
        if tx and all(e.voided for e in tx.expenses):
            tx.status = "open"
    audit(s, "expense", expense.id, "voided", reason=reason)


def store_receipt(s: Session, expense: Expense, filename: str, content: bytes) -> Receipt:
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_RECEIPT_TYPES:
        raise BusinessError(f"File type {ext or '?'} not allowed. Use PDF, image or XML.")
    if not content:
        raise BusinessError("The file is empty.")
    if len(content) > MAX_RECEIPT_BYTES:
        raise BusinessError("File is larger than 25 MB.")
    digest = hashlib.sha256(content).hexdigest()
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(filename).stem)[:60] or "beleg"
    folder = config.receipts_dir(expense.date.year)
    target = folder / f"{expense.date:%Y-%m-%d}_{expense.id:05d}_{digest[:8]}_{safe}{ext}"
    if not target.exists():
        target.write_bytes(content)  # stored unchanged
    r = Receipt(expense=expense, original_name=filename, path=str(target), sha256=digest, size=len(content))
    s.add(r)
    s.flush()
    audit(s, "receipt", r.id, "stored", expense_id=expense.id, file=target.name, sha256=digest)
    return r


def verify_receipt(r: Receipt) -> bool:
    p = Path(r.path)
    return p.exists() and hashlib.sha256(p.read_bytes()).hexdigest() == r.sha256


def save_other_income(s: Session, item: OtherIncome | None, data: dict) -> OtherIncome:
    if data.get("date") is None or data.get("gross_eur") is None:
        raise BusinessError("Date and amount are required.")
    if data["gross_eur"] == 0:
        raise BusinessError("Amount cannot be zero.")
    if data.get("vat_treatment") not in ("kleinunternehmer", "domestic", "reverse_charge", "non_eu",
                                         "exempt_4_25", "exempt_4_21"):
        raise BusinessError("Choose the VAT treatment.")
    if data["vat_treatment"] == "domestic" and data.get("vat_eur") is None:
        data["vat_eur"] = vat_from_gross(data["gross_eur"], Decimal("19"))
    if data["vat_treatment"] != "domestic":
        data["vat_eur"] = Decimal("0")
    is_new = item is None
    item = item or OtherIncome()
    changes = {}
    for f in ("date", "client_id", "description", "gross_eur", "vat_treatment", "vat_eur", "transaction_id"):
        if f in data and getattr(item, f, None) != data[f]:
            changes[f] = [getattr(item, f, None), data[f]]
            setattr(item, f, data[f])
    s.add(item)
    s.flush()
    if item.transaction_id:
        s.get(Transaction, item.transaction_id).status = "income"
    audit(s, "other_income", item.id, "created" if is_new else "updated", changes=changes)
    return item


def void_other_income(s: Session, item: OtherIncome) -> None:
    item.voided = True
    if item.transaction_id:
        s.get(Transaction, item.transaction_id).status = "open"
    audit(s, "other_income", item.id, "voided")


def tax_year(s: Session, year: int) -> TaxYear:
    ty = s.get(TaxYear, year)
    if ty is None:
        ty = TaxYear(year=year, homeoffice_days=0, sonderausgaben=Decimal("0"), prepayments_other=Decimal("0"))
        s.add(ty)
        s.flush()
    return ty


def save_tax_year(s: Session, year: int, homeoffice_days: int, sonderausgaben: Decimal,
                  prepayments_other: Decimal) -> TaxYear:
    if not 0 <= homeoffice_days <= 366:
        raise BusinessError("Home office days must be between 0 and 366.")
    ty = tax_year(s, year)
    before = [ty.homeoffice_days, ty.sonderausgaben, ty.prepayments_other]
    ty.homeoffice_days, ty.sonderausgaben, ty.prepayments_other = homeoffice_days, sonderausgaben, prepayments_other
    audit(s, "tax_year", year, "updated", before=before,
          after=[homeoffice_days, sonderausgaben, prepayments_other])
    return ty
