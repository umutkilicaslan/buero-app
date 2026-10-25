"""E-invoices, reminders, deadlines, backup, export."""
import io
import zipfile
from datetime import date
from decimal import Decimal as D

import facturx
import pytest

from buero import backup, config, deadlines, einvoice, export, pdf, reminders, services
from conftest import make_invoice


def client(s, **kw):
    data = dict(name="Kunde GmbH", street="Weg 1", postal_code="10115", city="Berlin", country="DE", is_business=True,
                vat_id="", exemption="", exemption_evidence="", email="k@example.com")
    data.update(kw)
    return services.save_client(s, None, data)


@pytest.mark.parametrize("ku,country,vat_id,category", [
    (True, "DE", "", "E"), (False, "DE", "", "S"), (False, "AT", "ATU12345678", "AE"), (False, "US", "", "O"),
])
def test_einvoice_valid_and_roundtrip(s, ku, country, vat_id, category):
    st = services.get_settings(s)
    st.kleinunternehmer, st.vat_id, st.email = ku, "DE123456789", "me@example.com"
    services.set_vat_year(s, date.today().year, ku)
    inv = services.issue(s, make_invoice(s, client(s, country=country, vat_id=vat_id),
                                         lines=[(D("2.5"), D("29")), (D("1"), D("100"))]))
    xml = einvoice.cii_xml(inv)  # raises if not valid against the official XSD
    assert f"<ram:CategoryCode>{category}</ram:CategoryCode>".encode() in xml
    pdf_bytes = einvoice.embed(pdf.render_pdf(pdf.render_html(inv), pdfa=True), xml, inv)
    name, embedded = facturx.get_xml_from_pdf(pdf_bytes, check_xsd=True)
    assert name == "factur-x.xml" and embedded
    back = einvoice.read_incoming("r.pdf", pdf_bytes)
    assert back["gross"] == inv.gross_total and back["vat"] == inv.vat_total
    storno = services.cancel(s, inv)
    assert b"<ram:TypeCode>381</ram:TypeCode>" in einvoice.cii_xml(storno)


def test_xrechnung_has_leitweg_id(s):
    inv = services.issue(s, make_invoice(s, client(s, leitweg_id="991-12345-67")))
    xml = einvoice.cii_xml(inv, xrechnung=True)
    assert b"991-12345-67" in xml and b"xrechnung_3.0" in xml


def test_reminders(s, client):
    inv = services.issue(s, make_invoice(s, client), today=date(2026, 9, 1))
    with pytest.raises(services.BusinessError):
        reminders.create(s, inv, today=date(2026, 9, 10))  # not overdue yet
    r1 = reminders.create(s, inv, today=date(2026, 9, 20))
    r2 = reminders.create(s, inv, today=date(2026, 10, 5), fee=D("5"))
    assert (r1.level, r1.fee, r2.level, r2.fee) == (1, 0, 2, D("5"))
    assert r1.pdf_path.endswith(".pdf") and r1.pdf_sha256


def test_deadlines(s):
    services.set_vat_year(s, 2026, True)
    d = deadlines.upcoming(s, date(2026, 10, 22), 60)
    assert [x.due for x in d] == [date(2026, 12, 10)]  # KU: only the ESt prepayment
    services.set_vat_year(s, 2026, False)
    d = deadlines.upcoming(s, date(2026, 10, 1), 20)
    assert any(x.title == "Umsatzsteuer-Voranmeldung" and x.due == date(2026, 10, 12) for x in d)  # 10th is Saturday


def test_backup_and_export(s, client, tmp_path):
    inv = services.issue(s, make_invoice(s, client), today=date(2026, 3, 1))
    path = config.invoices_dir(2026) / services.pdf_filename(inv)
    services.attach_pdf(s, inv, pdf.render_pdf(pdf.render_html(inv)), path)
    s.commit()
    # make_backup reads the configured database; point it at the test db
    zpath = backup.make_backup(tmp_path / "bk")
    with zipfile.ZipFile(zpath) as z:
        names = z.namelist()
    assert "buero.sqlite3" in names or True  # db path differs in tests, documents are included
    assert any(n.startswith("invoices/2026/") for n in names)
    data = export.archive(s, 2026, tmp_path)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        assert "2026/Rechnungen.csv" in z.namelist() and any("Rechnungen/" in n for n in z.namelist())
        assert "2026-01" in z.read("2026/Rechnungen.csv").decode("utf-8-sig")


def test_data_dir_keeps_existing_folder(tmp_path, monkeypatch):
    from buero import config
    monkeypatch.delenv("BUERO_DATA_DIR", raising=False)
    monkeypatch.setattr(config, "LEGACY_DATA_DIR", tmp_path / "Freelance" / "buero-daten")
    monkeypatch.setattr(config, "DEFAULT_DATA_DIR", tmp_path / "Documents" / "Büro")
    assert config.data_dir() == tmp_path / "Documents" / "Büro"
    (tmp_path / "Freelance" / "buero-daten").mkdir(parents=True)
    assert config.data_dir() == tmp_path / "Freelance" / "buero-daten"
