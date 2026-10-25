"""End-to-end through the web app: every page, real PDFs, bank import, reports."""
import re
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from buero import db

DATA = Path(__file__).parent / "data"
REAL = Path("/mnt/project-files/vergi/belge-veritabani/kontist-transactions_2024-01-01_2026-10-19.csv")


@pytest.fixture
def web(tmp_path, monkeypatch):
    monkeypatch.setenv("BUERO_DATA_DIR", str(tmp_path))
    db.init(tmp_path / "web.sqlite3")
    from buero.web import app
    return TestClient(app), tmp_path


def setup_business(c):
    r = c.post("/settings", data=dict(name="Umut K", street="Teststr. 1", postal_code="10999", city="Berlin",
                                     tax_number="12/345/67890", iban="DE89 3704 0044 0532 0130 00", kleinunternehmer="on",
                                     invoice_language="de", number_digits="2", payment_terms_days="14", einvoice="on",
                                     email="u@example.com", phone="030 123"))
    assert "Settings saved" in r.text
    r = c.post("/clients/save", data=dict(name="Katakids gUG", street="Weg 3", postal_code="10115", city="Berlin",
                                         country="de", is_business="on", default_hourly_rate="29,00", email="k@example.com",
                                         leitweg_id="991-1-2"))
    assert "Client saved" in r.text


def test_invalid_iban_rejected(web):
    c, _ = web
    r = c.post("/settings", data=dict(name="x", iban="DE00123456780000000000"))
    assert "IBAN is not valid" in r.text


def test_full_invoice_flow(web):
    c, tmp = web
    setup_business(c)
    r = c.post("/invoices/new", data={"client_id": "1"})
    assert "Draft invoice" in r.text
    r = c.post("/invoices/1/save", data={"client_id": "1", "language": "de", "service_from": "2026-09-01",
                                         "service_to": "2026-09-30", "description": ["Training", ""],
                                         "quantity": ["4,5", ""], "unit": ["Std.", "Std."],
                                         "unit_price": ["29", ""], "action": "issue"})
    assert "issued and archived" in r.text and "ZUGFeRD" in r.text, r.text[:3000]
    pdfs = list((tmp / "invoices" / str(date.today().year)).glob("*.pdf"))
    assert len(pdfs) == 1 and pdfs[0].read_bytes().startswith(b"%PDF")
    assert c.get("/invoices/1/pdf").headers["content-type"] == "application/pdf"
    x = c.get("/invoices/1/xrechnung")
    assert x.headers["content-type"].startswith("application/xml") and b"991-1-2" in x.content
    r = c.post("/invoices/1/save", data={"client_id": "1", "description": ["x"], "quantity": ["1"],
                                         "unit": [""], "unit_price": ["1"]})
    assert "can no longer be changed" in r.text
    r = c.post("/invoices/1/paid", data={"paid_date": date.today().isoformat(), "paid_amount": "50"})
    assert "Payment recorded" in r.text and "Part-paid" in r.text
    r = c.post("/invoices/1/storno", data={"reason": "Test", "copy": "on"})
    assert "Cancelled by" in r.text
    # tampering with the archived PDF is detected
    pdfs[0].write_bytes(b"%PDF fake")
    assert "checksum differs" in c.get("/invoices/1/pdf").text


def test_expense_flow(web):
    c, _ = web
    setup_business(c)
    r = c.post("/expenses/save", data={"date": "2026-03-01", "vendor": "Thomann", "category": "equipment",
                                       "gross_eur": "59,90", "vat_rate": "19", "business_share": "100", "currency": "EUR"},
               files={"receipts": ("beleg.pdf", b"%PDF-1.4 x", "application/pdf")})
    assert "Expense saved" in r.text and "beleg.pdf" in r.text
    assert c.get("/receipts/1").content == b"%PDF-1.4 x"
    r = c.post("/expenses/save", data={"date": "2026-03-01", "vendor": "", "category": "equipment", "gross_eur": "1"})
    assert "Vendor is required" in r.text
    r = c.post("/expenses/1/void", data={"reason": "doppelt"})
    assert "voided" in r.text


def test_einvoice_upload_prefills_expense(web):
    c, _ = web
    setup_business(c)
    c.post("/invoices/new", data={"client_id": "1"})
    c.post("/invoices/1/save", data={"client_id": "1", "language": "de", "service_from": "2026-09-01",
                                     "description": ["Training"], "quantity": ["2"], "unit": ["Std."],
                                     "unit_price": ["29"], "action": "issue"})
    pdf_bytes = c.get("/invoices/1/pdf").content
    r = c.post("/expenses/einvoice", files={"file": ("rechnung.pdf", pdf_bytes, "application/pdf")})
    assert "Read from the e-invoice" in r.text and 'value="58"' in r.text and "Umut K" in r.text


def test_bank_and_reports(web):
    c, _ = web
    setup_business(c)
    r = c.post("/bank/import", files={"file": ("k.csv", (DATA / "kontist_sample.csv").read_bytes(), "text/csv")})
    assert "6 new transactions" in r.text
    r = c.post("/bank/import", files={"file": ("k.csv", (DATA / "kontist_sample.csv").read_bytes(), "text/csv")})
    assert "0 new transactions" in r.text
    page = c.get("/bank").text
    tid = re.search(r'/bank/(\d+)/status', page).group(1)
    r = c.post(f"/bank/{tid}/status", data={"status": "private"})
    assert "Marked as private" in r.text
    r = c.post(f"/bank/{tid}/undo")
    assert "open again" in r.text
    r = c.post("/bank/ignore-before", data={"before": "2099-01-01", "note": "Already booked in Accountable"})
    assert "older transactions marked as ignored" in r.text and "All done" in c.get("/bank").text
    r = c.post(f"/bank/{tid}/undo")
    assert "open again" in r.text
    r = c.post("/bank/import", files={"file": ("bad.csv", b"foo;bar\n1;2\n", "text/csv")})
    assert "Map the columns" in r.text
    for tab in ("euer", "tax", "ku", "ustva", "zm", "assets", "export"):
        r = c.get(f"/reports?year=2026&tab={tab}")
        assert r.status_code == 200, tab
    assert c.get("/reports/euer.pdf?year=2026").content.startswith(b"%PDF")
    assert c.get("/export/archive.zip?year=2026").content[:2] == b"PK"
    for name in ("invoices", "payments", "expenses", "income", "transactions", "euer", "audit"):
        assert c.get(f"/export/{name}.csv?year=2026").status_code == 200
    r = c.post("/reports/tax-year", data={"year": "2026", "homeoffice_days": "100", "sonderausgaben": "2.400,00",
                                          "prepayments_other": "0"})
    assert "Saved" in r.text and "600,00 €" in c.get("/reports?year=2026&tab=euer").text


def test_all_pages_render(web):
    c, _ = web
    setup_business(c)
    for url in ["/", "/invoices", "/invoices?status=open", "/invoices?new=1", "/expenses", "/expenses/new",
                "/expenses?missing=1", "/income", "/income/new", "/bank", "/bank?status=all", "/reports",
                "/clients", "/clients/new", "/clients/1", "/settings", "/audit"]:
        r = c.get(url)
        assert r.status_code == 200, url
        assert "Traceback" not in r.text


@pytest.mark.skipif(not REAL.exists(), reason="real bank export not available")
def test_real_export_through_web(web):
    c, _ = web
    setup_business(c)
    r = c.post("/bank/import", files={"file": ("kontist.csv", REAL.read_bytes(), "text/csv")})
    assert "new transactions" in r.text
    assert c.get("/bank?status=all").status_code == 200
    assert c.get("/").status_code == 200
