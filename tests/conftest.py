from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from buero import db, services
from buero.models import Client


@pytest.fixture
def s(tmp_path, monkeypatch):
    monkeypatch.setenv("BUERO_DATA_DIR", str(tmp_path))
    engine = db.make_engine(tmp_path / "test.sqlite3")
    with Session(engine, expire_on_commit=False) as session:
        services.save_settings(session, dict(name="Umut Test", street="Teststr. 1", postal_code="10999",
                                             city="Berlin", tax_number="12/345/67890", iban="DE89370400440532013000",
                                             kleinunternehmer=True))
        session.commit()
        yield session


@pytest.fixture
def client(s):
    c = services.save_client(s, None, dict(name="GrenzKultur gGmbH", street="Am Sudhaus 2", postal_code="12053",
                                           city="Berlin", country="DE", is_business=True, vat_id="",
                                           default_hourly_rate=Decimal("29"), exemption="", exemption_evidence=""))
    s.commit()
    return c


def make_invoice(s, client, lines=((Decimal("10"), Decimal("29")),), **header):
    from datetime import date
    inv = services.new_draft(s, client)
    header = {"service_from": date(2026, 9, 1), "service_to": date(2026, 9, 30), **header}
    services.update_draft(s, inv, header, [
        {"description": "Zirkuspädagogik", "quantity": q, "unit": "Std.", "unit_price": p} for q, p in lines])
    return inv
