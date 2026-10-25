"""Database setup, including the SQLite triggers that enforce GoBD immutability
even if someone bypasses the app."""
import shutil
from datetime import datetime
from pathlib import Path

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from . import config
from .models import Base, Settings

# Columns that are frozen once an invoice leaves draft status.
FROZEN_COLUMNS = [
    "kind", "number", "year", "seq", "client_id", "cancels_invoice_id", "language", "issue_date",
    "service_from", "service_to", "due_date", "intro_text", "vat_treatment", "vat_rate", "vat_note",
    "seller_snapshot", "client_snapshot", "net_total", "vat_total", "gross_total", "issued_at",
]

TRIGGERS = [
    """CREATE TRIGGER IF NOT EXISTS invoices_no_delete BEFORE DELETE ON invoices
       WHEN OLD.status != 'draft'
       BEGIN SELECT RAISE(ABORT, 'GoBD: issued invoices cannot be deleted'); END""",
    f"""CREATE TRIGGER IF NOT EXISTS invoices_frozen BEFORE UPDATE ON invoices
       WHEN OLD.status != 'draft' AND (NEW.status = 'draft' OR {' OR '.join(f'NEW.{c} IS NOT OLD.{c}' for c in FROZEN_COLUMNS)})
       BEGIN SELECT RAISE(ABORT, 'GoBD: issued invoices cannot be changed'); END""",
    """CREATE TRIGGER IF NOT EXISTS invoices_pdf_once BEFORE UPDATE ON invoices
       WHEN OLD.status != 'draft' AND OLD.pdf_sha256 != '' AND NEW.pdf_sha256 IS NOT OLD.pdf_sha256
       BEGIN SELECT RAISE(ABORT, 'GoBD: the archived PDF of an issued invoice cannot be replaced'); END""",
    """CREATE TRIGGER IF NOT EXISTS lines_insert_draft_only BEFORE INSERT ON invoice_lines
       WHEN (SELECT status FROM invoices WHERE id = NEW.invoice_id) != 'draft'
       BEGIN SELECT RAISE(ABORT, 'GoBD: lines of issued invoices cannot be changed'); END""",
    """CREATE TRIGGER IF NOT EXISTS lines_update_draft_only BEFORE UPDATE ON invoice_lines
       WHEN (SELECT status FROM invoices WHERE id = OLD.invoice_id) != 'draft'
       BEGIN SELECT RAISE(ABORT, 'GoBD: lines of issued invoices cannot be changed'); END""",
    """CREATE TRIGGER IF NOT EXISTS lines_delete_draft_only BEFORE DELETE ON invoice_lines
       WHEN (SELECT status FROM invoices WHERE id = OLD.invoice_id) != 'draft'
       BEGIN SELECT RAISE(ABORT, 'GoBD: lines of issued invoices cannot be changed'); END""",
    """CREATE TRIGGER IF NOT EXISTS receipts_no_update BEFORE UPDATE ON receipts
       BEGIN SELECT RAISE(ABORT, 'GoBD: stored receipts cannot be changed'); END""",
    """CREATE TRIGGER IF NOT EXISTS receipts_no_delete BEFORE DELETE ON receipts
       BEGIN SELECT RAISE(ABORT, 'GoBD: stored receipts cannot be deleted'); END""",
    """CREATE TRIGGER IF NOT EXISTS expenses_no_delete BEFORE DELETE ON expenses
       BEGIN SELECT RAISE(ABORT, 'GoBD: expenses cannot be deleted, void them instead'); END""",
    """CREATE TRIGGER IF NOT EXISTS reminders_frozen BEFORE UPDATE ON reminders
       WHEN OLD.pdf_sha256 != ''
       BEGIN SELECT RAISE(ABORT, 'GoBD: issued reminders cannot be changed'); END""",
    """CREATE TRIGGER IF NOT EXISTS reminders_no_delete BEFORE DELETE ON reminders
       BEGIN SELECT RAISE(ABORT, 'GoBD: issued reminders cannot be deleted'); END""",
    """CREATE TRIGGER IF NOT EXISTS transactions_no_delete BEFORE DELETE ON transactions
       BEGIN SELECT RAISE(ABORT, 'Imported bank transactions cannot be deleted'); END""",
    """CREATE TRIGGER IF NOT EXISTS transactions_core_frozen BEFORE UPDATE ON transactions
       WHEN NEW.booking_date IS NOT OLD.booking_date OR NEW.amount IS NOT OLD.amount
         OR NEW.purpose IS NOT OLD.purpose OR NEW.counterparty IS NOT OLD.counterparty
       BEGIN SELECT RAISE(ABORT, 'Imported bank data cannot be changed'); END""",
    """CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit_log
       BEGIN SELECT RAISE(ABORT, 'Audit log is append-only'); END""",
    """CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit_log
       BEGIN SELECT RAISE(ABORT, 'Audit log is append-only'); END""",
]


def make_engine(path: Path | None = None):
    url = f"sqlite:///{path}" if path else "sqlite://"
    engine = create_engine(url, future=True)

    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_conn, _):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys = ON")
        cur.close()

    Base.metadata.create_all(engine)
    _add_missing_columns(engine)
    with engine.begin() as conn:
        for ddl in TRIGGERS:
            conn.execute(text(ddl))
    with Session(engine) as s:
        if s.get(Settings, 1) is None:
            s.add(Settings(id=1))
            s.commit()
    return engine


def _add_missing_columns(engine) -> None:
    """Minimal forward migration: new model columns are added to existing tables."""
    from sqlalchemy.schema import CreateColumn
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            existing = {row[1] for row in conn.execute(text(f"PRAGMA table_info({table.name})"))}
            for col in table.columns:
                if col.name in existing:
                    continue
                ddl = str(CreateColumn(col).compile(engine)).replace(" NOT NULL", "")
                default = col.default.arg if col.default is not None and not callable(col.default.arg) else None
                if default is not None:
                    value = default if isinstance(default, (int, float)) else f"'{default}'"
                    if isinstance(default, bool):
                        value = int(default)
                    ddl += f" DEFAULT {value}"
                conn.execute(text(f"ALTER TABLE {table.name} ADD COLUMN {ddl}"))


def backup_on_start(path: Path) -> Path | None:
    """Dated copy of the database each time the app starts. Old backups are never deleted."""
    if not path.exists():
        return None
    target = config.backups_dir() / f"buero_{datetime.now():%Y-%m-%d_%H%M%S}.sqlite3"
    shutil.copy2(path, target)
    return target


_engine = None
SessionLocal = sessionmaker(expire_on_commit=False)


def init(path: Path | None = None):
    global _engine
    _engine = make_engine(path)
    SessionLocal.configure(bind=_engine)
    return _engine
