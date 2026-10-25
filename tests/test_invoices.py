import sqlite3
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, OperationalError

from buero import services
from buero.models import Invoice
from buero.services import BusinessError
from conftest import make_invoice


def test_draft_totals_and_no_number(s, client):
    inv = make_invoice(s, client, lines=[(Decimal("10"), Decimal("29")), (Decimal("0.75"), Decimal("29"))])
    assert inv.number is None and inv.status == "draft"
    assert inv.gross_total == Decimal("311.75") and inv.vat_total == 0


def test_issue_assigns_sequential_numbers_per_year(s, client):
    a = services.issue(s, make_invoice(s, client), today=date(2026, 10, 1))
    b = services.issue(s, make_invoice(s, client), today=date(2026, 10, 2))
    c = services.issue(s, make_invoice(s, client), today=date(2027, 1, 2))
    assert (a.number, b.number, c.number) == ("2026-01", "2026-02", "2027-01")
    assert a.vat_note == "Gemäß § 19 UStG wird keine Umsatzsteuer berechnet."
    assert a.due_date == date(2026, 10, 15)
    assert services.check_numbering(s) == []


def test_deleted_draft_does_not_create_gap(s, client):
    d1 = make_invoice(s, client)
    services.delete_draft(s, d1)
    a = services.issue(s, make_invoice(s, client), today=date(2026, 10, 1))
    assert a.number == "2026-01"


def test_continue_numbering_from_old_system(s, client):
    services.set_next_number(s, 2026, 19)
    inv = services.issue(s, make_invoice(s, client), today=date(2026, 10, 1))
    assert inv.number == "2026-19"
    with pytest.raises(BusinessError):
        services.set_next_number(s, 2026, 30)


def test_issue_requires_mandatory_fields(s, client):
    inv = make_invoice(s, client, service_from=None, service_to=None)
    with pytest.raises(BusinessError, match="Leistungszeitpunkt"):
        services.issue(s, inv)
    assert inv.number is None
    client.street = ""
    inv2 = make_invoice(s, client)
    with pytest.raises(BusinessError, match="address"):
        services.issue(s, inv2)


def test_issued_invoice_cannot_be_edited_or_deleted_via_app(s, client):
    inv = services.issue(s, make_invoice(s, client), today=date(2026, 10, 1))
    with pytest.raises(BusinessError):
        services.update_draft(s, inv, {}, [])
    with pytest.raises(BusinessError):
        services.delete_draft(s, inv)


def test_database_triggers_block_changes_even_outside_the_app(s, client):
    inv = services.issue(s, make_invoice(s, client), today=date(2026, 10, 1))
    s.commit()
    for sql in [f"UPDATE invoices SET gross_total='1' WHERE id={inv.id}",
                f"UPDATE invoices SET status='draft' WHERE id={inv.id}",
                f"DELETE FROM invoices WHERE id={inv.id}",
                f"UPDATE invoice_lines SET quantity='99' WHERE invoice_id={inv.id}",
                f"DELETE FROM invoice_lines WHERE invoice_id={inv.id}",
                "DELETE FROM audit_log",
                "UPDATE audit_log SET action='x'"]:
        with pytest.raises((IntegrityError, OperationalError)):
            s.execute(text(sql))
        s.rollback()


def test_payment(s, client):
    inv = services.issue(s, make_invoice(s, client), today=date(2026, 10, 1))
    assert inv.is_open and inv.is_overdue(date(2026, 10, 16)) and not inv.is_overdue(date(2026, 10, 15))
    services.mark_paid(s, inv, date(2026, 10, 10))
    s.commit()
    assert inv.status == "paid" and inv.paid_amount == Decimal("290.00") and not inv.is_open
    services.unmark_paid(s, inv)
    assert inv.status == "issued"


def test_storno_mirrors_original_and_uses_next_number(s, client):
    orig = services.issue(s, make_invoice(s, client, lines=[(Decimal("2.5"), Decimal("29"))]), today=date(2026, 10, 1))
    storno = services.cancel(s, orig, today=date(2026, 10, 5), reason="Falsche Stundenzahl")
    s.commit()
    assert orig.status == "cancelled"
    assert storno.number == "2026-02" and storno.kind == "storno" and storno.cancels_invoice_id == orig.id
    assert storno.gross_total == -orig.gross_total == Decimal("-72.50")
    assert [ln.line_net for ln in storno.lines] == [Decimal("-72.50")]
    assert storno.vat_note == orig.vat_note and "2026-01" in storno.intro_text
    with pytest.raises(BusinessError):
        services.cancel(s, orig)
    new = services.copy_as_draft(s, orig)
    assert new.is_draft and new.gross_total == Decimal("72.50")


def test_vat_19_invoice_totals(s, client):
    services.get_settings(s).kleinunternehmer = False
    inv = services.issue(s, make_invoice(s, client, lines=[(Decimal("3"), Decimal("33.33"))]), today=date(2026, 10, 1))
    assert (inv.net_total, inv.vat_total, inv.gross_total) == (Decimal("99.99"), Decimal("19.00"), Decimal("118.99"))
    assert inv.vat_treatment == "domestic" and inv.vat_note == ""


def test_snapshot_keeps_old_address(s, client):
    import json
    inv = services.issue(s, make_invoice(s, client), today=date(2026, 10, 1))
    services.save_client(s, client, {"name": client.name, "street": "Neue Str. 5"})
    assert json.loads(inv.client_snapshot)["street"] == "Am Sudhaus 2"


def test_audit_log_written(s, client):
    from buero.models import AuditLog
    inv = services.issue(s, make_invoice(s, client), today=date(2026, 10, 1))
    actions = [a for (a,) in s.execute(text(f"SELECT action FROM audit_log WHERE entity='invoice' AND entity_id={inv.id}"))]
    assert actions == ["draft_created", "draft_updated", "issued"]
