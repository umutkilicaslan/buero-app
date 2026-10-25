"""EÜR, UStVA, ZM, Kleinunternehmer check, depreciation, tax estimate."""
from datetime import date
from decimal import Decimal as D

import pytest

from buero import bookkeeping, ledger, services, taxcalc
from buero.services import BusinessError
from conftest import make_invoice


def expense(s, **kw):
    data = dict(date=date(2026, 3, 10), vendor="Shop", category="equipment", gross_eur=D("119.00"),
                vat_rate=D("19"))
    data.update(kw)
    return bookkeeping.save_expense(s, None, data)


def client(s, **kw):
    data = dict(name="Kunde", street="Weg 1", postal_code="10115", city="Berlin", country="DE", is_business=True,
                vat_id="", exemption="", exemption_evidence="")
    data.update(kw)
    return services.save_client(s, None, data)


# ---------- Kleinunternehmer year ----------

def test_euer_kleinunternehmer(s):
    services.set_vat_year(s, 2026, True)
    c = client(s)
    inv = services.issue(s, make_invoice(s, c, lines=[(D("10"), D("29"))]), today=date(2026, 3, 1))
    services.mark_paid(s, inv, date(2026, 3, 20))
    expense(s)  # 119 € incl. VAT, KU → full 119 € cost
    expense(s, category="telecom", gross_eur=D("40"), business_share=D("20"))  # 8 €
    expense(s, category="hospitality", gross_eur=D("100"))  # 70 € deductible, 30 € not
    r = ledger.euer(s, 2026)
    assert r.lines["111"] == D("290.00")
    assert r.lines["229"] == D("119.00")
    assert r.lines["280"] == D("8.00")
    assert r.lines["175"] == D("70.00") and r.lines["165"] == D("30.00")
    assert "185" not in r.lines or r.lines["185"] == 0
    assert r.expense_total == D("197.00") and r.profit == D("93.00")


def test_income_counts_in_year_paid(s):
    services.set_vat_year(s, 2025, True)
    services.set_vat_year(s, 2026, True)
    inv = services.issue(s, make_invoice(s, client(s)), today=date(2025, 12, 15))
    services.mark_paid(s, inv, date(2026, 1, 5))
    assert ledger.euer(s, 2025).income_total == 0
    assert ledger.euer(s, 2026).lines["111"] == D("290.00")


def test_exempt_income_in_ku_year_goes_to_119_and_not_into_gesamtumsatz(s):
    services.set_vat_year(s, 2026, True)
    youth = client(s, name="GrenzKultur gGmbH", exemption="4_25", exemption_evidence="Bescheid § 75 SGB VIII")
    other = client(s, name="Firma")
    a = services.issue(s, make_invoice(s, youth, lines=[(D("100"), D("29"))]), today=date(2026, 2, 1))
    b = services.issue(s, make_invoice(s, other, lines=[(D("1"), D("500"))]), today=date(2026, 2, 1))
    services.mark_paid(s, a, date(2026, 2, 10))
    services.mark_paid(s, b, date(2026, 2, 10))
    r = ledger.euer(s, 2026)
    assert r.lines["111"] == D("3400.00") and r.lines["119"] == D("2900.00")
    assert ledger.gesamtumsatz(s, 2026) == D("500.00")
    assert ledger.youth_share(s, 2026) == D("85.3")


def test_partial_payments(s):
    inv = services.issue(s, make_invoice(s, client(s)), today=date(2026, 3, 1))
    services.mark_paid(s, inv, date(2026, 3, 5), D("100"))
    assert inv.status == "issued" and inv.outstanding == D("190.00")
    services.mark_paid(s, inv, date(2026, 4, 5))
    assert inv.status == "paid" and inv.paid_amount == D("290.00") and inv.paid_date == date(2026, 4, 5)


# ---------- regular VAT year ----------

def test_euer_and_ustva_regular(s):
    services.get_settings(s).kleinunternehmer = False
    services.set_vat_year(s, 2026, False)
    services.get_settings(s).vat_id = "DE123456789"
    de = client(s)
    at = client(s, name="Wien GmbH", country="AT", vat_id="ATU12345678")
    us = client(s, name="NY Inc", country="US")
    i1 = services.issue(s, make_invoice(s, de, lines=[(D("10"), D("100"))]), today=date(2026, 4, 2))  # 1000 + 190
    i2 = services.issue(s, make_invoice(s, at, lines=[(D("1"), D("500"))]), today=date(2026, 4, 2))
    i3 = services.issue(s, make_invoice(s, us, lines=[(D("1"), D("300"))]), today=date(2026, 4, 2))
    for i in (i1, i2, i3):
        services.mark_paid(s, i, date(2026, 5, 1))
    expense(s, date=date(2026, 5, 3))  # 100 net + 19 VAT
    expense(s, date=date(2026, 5, 4), vendor="Google Ireland", category="advertising", gross_eur=D("50"),
            vat_rate=D("0"), reverse_charge="eu")
    r = ledger.euer(s, 2026)
    assert r.lines["112"] == D("1000.00") and r.lines["140"] == D("190.00")
    assert r.lines["103"] == D("800.00")
    assert r.lines["229"] == D("100.00") and r.lines["185"] == D("19.00") and r.lines["224"] == D("50.00")
    u = ledger.ustva(s, date(2026, 4, 1), date(2026, 6, 30))
    assert u.kz["81"] == D("1000") and u.kz["21"] == D("500") and u.kz["45"] == D("300")
    assert u.kz["46"] == D("50.00") and u.kz["47"] == D("9.50") and u.kz["67"] == D("9.50")
    assert u.kz["66"] == D("19.00")
    assert u.kz["83"] == D("190.00") + D("9.50") - D("19.00") - D("9.50")
    assert ledger.zm(s, date(2026, 4, 1), date(2026, 6, 30)) == []  # ZM follows the service date (September)
    zm = ledger.zm(s, date(2026, 7, 1), date(2026, 9, 30))
    assert zm == [{"country": "AT", "vat_id": "U12345678", "name": "Wien GmbH", "amount": D("500.00"),
                   "invoices": [i2.number], "amount_eur": D("500")}]


def test_ustva_kleinunternehmer_only_13b(s):
    services.set_vat_year(s, 2026, True)
    inv = services.issue(s, make_invoice(s, client(s)), today=date(2026, 1, 10))
    services.mark_paid(s, inv, date(2026, 1, 20))
    expense(s, date=date(2026, 1, 15), vendor="Adobe Ireland", category="software", gross_eur=D("60"),
            vat_rate=D("0"), reverse_charge="eu")
    u = ledger.ustva(s, date(2026, 1, 1), date(2026, 3, 31))
    assert "81" not in u.kz and u.kz["47"] == D("11.40") and "67" not in u.kz and u.kz["83"] == D("11.40")


def test_ust_payments_from_bank_in_euer(s):
    from buero.models import BankImport, Transaction
    services.set_vat_year(s, 2026, False)
    imp = BankImport(filename="x", sha256="x", profile="", rows_total=2, rows_new=2)
    s.add(imp)
    s.flush()
    s.add_all([Transaction(import_id=imp.id, booking_date=date(2026, 2, 10), amount=D("-150"), dedupe_key="a",
                           status="tax", tax_kind="ust", tax_year=2026),
               Transaction(import_id=imp.id, booking_date=date(2026, 3, 10), amount=D("20"), dedupe_key="b",
                           status="tax", tax_kind="ust", tax_year=2025),
               Transaction(import_id=imp.id, booking_date=date(2026, 3, 10), amount=D("-400"), dedupe_key="c",
                           status="tax", tax_kind="est", tax_year=2026)])
    s.flush()
    r = ledger.euer(s, 2026)
    assert r.lines["186"] == D("150") and r.lines["141"] == D("20")
    assert ledger.tax_payments(s, 2026) == D("400")


# ---------- assets ----------

def test_gwg_and_afa(s):
    services.set_vat_year(s, 2026, True)
    gwg = expense(s, category="asset", gross_eur=D("952.00"))  # 800 net → GWG
    assert ledger.is_gwg(gwg)
    cam = expense(s, date=date(2026, 9, 15), category="asset", gross_eur=D("1428.00"), useful_life_years=7)
    sched = ledger.depreciation_schedule(cam, True)  # KU: cost = gross 1428
    assert sched[2026] == D("68.00")  # 1428/7 = 204 × 4/12
    assert sum(sched.values()) == D("1428.00") and max(sched) == 2033
    laptop = expense(s, date=date(2026, 10, 2), category="asset", gross_eur=D("1785.00"), useful_life_years=1)
    r = ledger.euer(s, 2026)
    assert r.lines["132"] == D("952.00")
    assert r.lines["130"] == D("68.00") + D("1785.00")
    assert ledger.euer(s, 2027).lines["130"] == D("204.00")


def test_asset_over_800_needs_life(s):
    with pytest.raises(BusinessError, match="useful life"):
        expense(s, category="asset", gross_eur=D("2000"))


def test_homeoffice_capped(s):
    bookkeeping.save_tax_year(s, 2026, 250, D("0"), D("0"))
    assert ledger.euer(s, 2026).lines["163"] == D("1260")


# ---------- expenses ----------

def test_expense_validation_and_void(s):
    with pytest.raises(BusinessError):
        expense(s, gross_eur=D("0"))
    with pytest.raises(BusinessError):
        expense(s, business_share=D("120"))
    with pytest.raises(BusinessError):
        expense(s, reverse_charge="eu", vat_eur=D("5"))
    with pytest.raises(BusinessError):
        expense(s, currency="USD")
    e = expense(s, currency="USD", amount_original=D("130"))
    assert e.vat_eur == D("19.00")
    bookkeeping.void_expense(s, e, "doppelt")
    assert ledger.euer(s, 2026).expense_total == 0
    with pytest.raises(BusinessError):
        bookkeeping.save_expense(s, e, {"date": e.date, "vendor": "x", "category": "other", "gross_eur": D("1")})


def test_receipts_are_immutable(s):
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError, OperationalError
    e = expense(s)
    r = bookkeeping.store_receipt(s, e, "Rechnung März.pdf", b"%PDF-1.4 test")
    s.commit()
    assert bookkeeping.verify_receipt(r)
    with pytest.raises(BusinessError):
        bookkeeping.store_receipt(s, e, "virus.exe", b"x")
    for sql in ["DELETE FROM receipts", "UPDATE receipts SET sha256='x'", "DELETE FROM expenses"]:
        with pytest.raises((IntegrityError, OperationalError)):
            s.execute(text(sql))
        s.rollback()


# ---------- tax ----------

@pytest.mark.parametrize("year,zve,tax", [
    (2026, D("12348"), D("0")), (2026, D("17799"), D("1034")), (2026, D("69878"), D("18213")),
    (2026, D("69879"), D("18213")), (2026, D("300000"), D("115529")),
    (2025, D("12096"), D("0")), (2025, D("40000"), D("7320")), (2024, D("11784"), D("0")),
])
def test_income_tax_tariff(year, zve, tax):
    assert taxcalc.income_tax(zve, year) == tax


def test_tariff_continuity():
    for year in (2024, 2025, 2026):
        prev = taxcalc.income_tax(D(0), year)
        for x in range(0, 300000, 250):
            t = taxcalc.income_tax(D(x), year)
            assert t >= prev and t - prev <= 250 * D("0.45") + 1
            prev = t


def test_estimate_2025_hand_checked():
    # Example profit and health insurance; no Soli below the 2025 threshold
    e = taxcalc.estimate(2025, D("40000"), D("1500"), church=False, gewerbe=False)
    assert e.income_tax == D("6845") and e.soli == 0 and e.total == D("6845")


def test_soli_and_church():
    e = taxcalc.estimate(2026, D("120000"), D("0"), church=True, gewerbe=False)
    assert e.soli > 0 and e.church == (e.income_tax * D("0.09")).quantize(D("0.01"))
