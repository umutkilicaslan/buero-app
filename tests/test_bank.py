from datetime import date
from decimal import Decimal as D
from pathlib import Path

import pytest

from buero import bank, ledger, services
from buero.models import Transaction
from conftest import make_invoice

DATA = Path(__file__).parent / "data"
REAL = Path("/mnt/project-files/vergi/belge-veritabani/kontist-transactions_2024-01-01_2026-10-19.csv")


def test_parse_kontist_and_sparkasse():
    rows, header, mapping = bank.parse((DATA / "kontist_sample.csv").read_bytes())
    assert len(rows) == 6 and rows[0].amount == D("696.00") and rows[0].counterparty.startswith("GrenzKultur")
    rows, _, _ = bank.parse((DATA / "sparkasse_sample.csv").read_bytes().decode().encode("cp1252"))
    assert rows[0].booking_date == date(2026, 3, 3) and rows[0].amount == D("1234.50")
    assert rows[1].counterparty == "DB Fernverkehr"


def test_import_is_idempotent_and_keeps_monthly_fees(s):
    content = (DATA / "kontist_sample.csv").read_bytes()
    imp = bank.import_csv(s, "a.csv", content)
    assert imp.rows_new == 6  # both 0,18 € fees with the same reference are kept
    imp2 = bank.import_csv(s, "a.csv", content)
    assert imp2.rows_new == 0


def test_tax_office_is_recognised(s):
    bank.import_csv(s, "a.csv", (DATA / "kontist_sample.csv").read_bytes())
    fa = s.query(Transaction).filter(Transaction.counterparty.like("Finanzamt%")).one()
    assert fa.status == "open" and fa.tax_kind == "est" and fa.tax_year == 2026


def test_match_two_invoices_by_number(s, client):
    services.set_next_number(s, 2026, 4)
    a = services.issue(s, make_invoice(s, client, lines=[(D("12"), D("29"))]), today=date(2026, 2, 28))
    b = services.issue(s, make_invoice(s, client, lines=[(D("12"), D("29"))]), today=date(2026, 2, 28))
    assert (a.number, b.number) == ("2026-04", "2026-05")
    bank.import_csv(s, "a.csv", (DATA / "kontist_sample.csv").read_bytes())
    tx = next(t for t in s.query(Transaction) if t.amount == D("696"))
    m = bank.suggest_invoices(s, tx)
    assert m[0].score == 100 and {i.number for i in m[0].invoices} == {"2026-04", "2026-05"}
    bank.apply_match(s, tx, m[0].invoices)
    assert a.status == b.status == "paid" and a.paid_date == date(2026, 3, 3) and tx.status == "invoice"
    assert ledger.euer(s, 2026).income_total == D("696.00")
    services.unmark_paid(s, a)
    assert tx.status == "open" or tx.status == "invoice"  # b still linked


def test_match_by_amount_and_name(s):
    c = services.save_client(s, None, dict(name="Katakids gUG", street="x", postal_code="1", city="B", country="DE",
                                           is_business=True, vat_id="", exemption="", exemption_evidence=""))
    inv = services.issue(s, make_invoice(s, c, lines=[(D("1"), D("425"))]), today=date(2026, 3, 1))
    bank.import_csv(s, "a.csv", (DATA / "kontist_sample.csv").read_bytes())
    tx = next(t for t in s.query(Transaction) if t.amount == D("425"))
    m = bank.suggest_invoices(s, tx)
    assert m and m[0].invoices == [inv] and m[0].score == 80


def test_overpayment_is_refused(s, client):
    inv = services.issue(s, make_invoice(s, client, lines=[(D("1"), D("100"))]), today=date(2026, 3, 1))
    bank.import_csv(s, "a.csv", (DATA / "kontist_sample.csv").read_bytes())
    tx = next(t for t in s.query(Transaction) if t.amount == D("425"))
    with pytest.raises(services.BusinessError):
        bank.apply_match(s, tx, [inv])


def test_expense_suggestion_reuses_category(s):
    from buero import bookkeeping
    bank.import_csv(s, "a.csv", (DATA / "kontist_sample.csv").read_bytes())
    tx = s.query(Transaction).filter(Transaction.counterparty == "Thomann").one()
    sug = bank.suggest_expense(s, tx)
    assert sug["gross_eur"] == D("59.90") and sug["vendor"] == "Thomann"
    e = bookkeeping.save_expense(s, None, {**sug, "category": "equipment", "vat_rate": D("19"), "transaction_id": tx.id})
    assert tx.status == "expense" and bank.missing_receipts(s) == [e]


def test_set_status_rules(s):
    bank.import_csv(s, "a.csv", (DATA / "kontist_sample.csv").read_bytes())
    tx = s.query(Transaction).filter(Transaction.counterparty.like("Finanzamt%")).one()
    with pytest.raises(services.BusinessError):
        bank.set_status(s, tx, "tax", "", 2026)
    bank.set_status(s, tx, "tax", "est", 2026)
    assert ledger.tax_payments(s, 2026) == D("145.00")


def test_invoice_numbers_in_text():
    assert bank.invoice_numbers_in("Rnr-2025-14 RNR-2025-35") == {(2025, 14), (2025, 35)}
    assert bank.invoice_numbers_in("Rechnung 17-2023") == {(2023, 17)}
    assert bank.invoice_numbers_in("IBAN DE1234 Betrag 2026") == set()


@pytest.mark.skipif(not REAL.exists(), reason="real bank export not available")
def test_real_kontist_export(s):
    """A real Kontist export (kept outside the repo) imports completely, and a second import adds nothing."""
    first = bank.import_csv(s, "kontist.csv", REAL.read_bytes())
    assert first.rows_total > 0 and first.rows_new == first.rows_total
    assert bank.import_csv(s, "kontist.csv", REAL.read_bytes()).rows_new == 0


def test_ignore_open_before(s):
    from datetime import date
    from pathlib import Path
    from buero import bank
    from buero.models import AuditLog, Transaction
    from sqlalchemy import select
    bank.import_csv(s, "k.csv", (Path(__file__).parent / "data" / "kontist_sample.csv").read_bytes())
    s.flush()
    open_before = [t for t in s.scalars(select(Transaction)) if t.status == "open"]
    cutoff = max(t.booking_date for t in open_before)
    expected = sum(1 for t in open_before if t.booking_date < cutoff)
    import pytest
    from buero.services import BusinessError
    with pytest.raises(BusinessError):
        bank.ignore_open_before(s, cutoff, " ")
    n = bank.ignore_open_before(s, cutoff, "Already booked in Accountable")
    assert n == expected and n > 0
    assert all(t.status == "open" for t in s.scalars(select(Transaction)) if t.booking_date >= cutoff and t in open_before)
    assert s.scalar(select(AuditLog).where(AuditLog.action == "bulk_ignored")) is not None
