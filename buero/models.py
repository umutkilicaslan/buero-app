from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, String, Text, TypeDecorator, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Money(TypeDecorator):
    """Decimal stored as TEXT so SQLite never turns it into a float."""

    impl = String
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return None if value is None else str(Decimal(value))

    def process_result_value(self, value, dialect):
        return None if value is None else Decimal(value)


class Base(DeclarativeBase):
    pass


class Settings(Base):
    """My own business data (single row, id=1)."""

    __tablename__ = "settings"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String, default="")
    business_name: Mapped[str] = mapped_column(String, default="")
    street: Mapped[str] = mapped_column(String, default="")
    postal_code: Mapped[str] = mapped_column(String, default="")
    city: Mapped[str] = mapped_column(String, default="")
    email: Mapped[str] = mapped_column(String, default="")
    phone: Mapped[str] = mapped_column(String, default="")
    tax_number: Mapped[str] = mapped_column(String, default="")  # Steuernummer
    vat_id: Mapped[str] = mapped_column(String, default="")  # USt-IdNr, empty = none
    kleinunternehmer: Mapped[bool] = mapped_column(Boolean, default=True)
    business_type: Mapped[str] = mapped_column(String, default="freiberufler")
    profession: Mapped[str] = mapped_column(String, default="")
    bank_name: Mapped[str] = mapped_column(String, default="")
    iban: Mapped[str] = mapped_column(String, default="")
    bic: Mapped[str] = mapped_column(String, default="")
    invoice_language: Mapped[str] = mapped_column(String, default="de")
    number_digits: Mapped[int] = mapped_column(Integer, default=2)
    payment_terms_days: Mapped[int] = mapped_column(Integer, default=14)
    # VAT filing (only relevant when not Kleinunternehmer, or for § 13b purchases)
    ist_versteuerung: Mapped[bool] = mapped_column(Boolean, default=True)  # § 20 UStG, usual for Freiberufler
    ustva_period: Mapped[str] = mapped_column(String, default="quarterly")  # monthly | quarterly
    dauerfristverlaengerung: Mapped[bool] = mapped_column(Boolean, default=False)
    # Income tax estimate
    church_tax: Mapped[bool] = mapped_column(Boolean, default=False)
    gewerbesteuer_hebesatz: Mapped[int] = mapped_column(Integer, default=410)  # Berlin
    # E-invoices and backups
    einvoice: Mapped[bool] = mapped_column(Boolean, default=True)  # embed Factur-X XML in invoice PDFs
    backup_dir: Mapped[str] = mapped_column(String, default="")
    reminder_fee: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    # Local AI assistant (Ollama). Off until you switch it on.
    ai_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    ai_url: Mapped[str] = mapped_column(String, default="http://127.0.0.1:11434")
    ai_model: Mapped[str] = mapped_column(String, default="qwen3:8b")

    @property
    def complete(self) -> bool:
        return all([self.name, self.street, self.postal_code, self.city, self.tax_number or self.vat_id, self.iban])


class Client(Base):
    __tablename__ = "clients"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String)
    contact_person: Mapped[str] = mapped_column(String, default="")
    street: Mapped[str] = mapped_column(String, default="")
    postal_code: Mapped[str] = mapped_column(String, default="")
    city: Mapped[str] = mapped_column(String, default="")
    country: Mapped[str] = mapped_column(String(2), default="DE")  # ISO 3166-1 alpha-2
    is_business: Mapped[bool] = mapped_column(Boolean, default=True)
    vat_id: Mapped[str] = mapped_column(String, default="")
    email: Mapped[str] = mapped_column(String, default="")
    default_hourly_rate: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    # Domestic VAT exemption for this client's services: "", "4_25" or "4_21"
    exemption: Mapped[str] = mapped_column(String, default="")
    exemption_evidence: Mapped[str] = mapped_column(Text, default="")
    leitweg_id: Mapped[str] = mapped_column(String, default="")  # public-sector buyer reference (XRechnung)
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    invoices: Mapped[list["Invoice"]] = relationship(back_populates="client")


class NumberSequence(Base):
    """Next free invoice number per year. Numbers are only taken when an invoice is issued."""

    __tablename__ = "number_sequences"
    year: Mapped[int] = mapped_column(Integer, primary_key=True)
    next_number: Mapped[int] = mapped_column(Integer, default=1)


class Invoice(Base):
    __tablename__ = "invoices"
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String, default="invoice")  # invoice | storno
    status: Mapped[str] = mapped_column(String, default="draft")  # draft | issued | paid | cancelled
    number: Mapped[str | None] = mapped_column(String, unique=True, nullable=True)
    year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    seq: Mapped[int | None] = mapped_column(Integer, nullable=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"))
    cancels_invoice_id: Mapped[int | None] = mapped_column(ForeignKey("invoices.id"), nullable=True)
    language: Mapped[str] = mapped_column(String, default="de")
    issue_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    service_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    service_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    intro_text: Mapped[str] = mapped_column(Text, default="")
    vat_override: Mapped[str] = mapped_column(String, default="")  # "" = automatic
    # Frozen when issued:
    vat_treatment: Mapped[str] = mapped_column(String, default="")
    vat_rate: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    vat_note: Mapped[str] = mapped_column(Text, default="")
    seller_snapshot: Mapped[str] = mapped_column(Text, default="")  # JSON
    client_snapshot: Mapped[str] = mapped_column(Text, default="")  # JSON
    net_total: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    vat_total: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    gross_total: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    pdf_path: Mapped[str] = mapped_column(String, default="")
    pdf_sha256: Mapped[str] = mapped_column(String, default="")
    # Payment (may change after issue):
    paid_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    paid_amount: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    issued_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    client: Mapped[Client] = relationship(back_populates="invoices")
    lines: Mapped[list["InvoiceLine"]] = relationship(
        back_populates="invoice", cascade="all, delete-orphan", order_by="InvoiceLine.position"
    )
    cancels: Mapped["Invoice | None"] = relationship(remote_side=[id], foreign_keys=[cancels_invoice_id])
    payments: Mapped[list["Payment"]] = relationship(back_populates="invoice", order_by="Payment.date")
    reminders: Mapped[list["Reminder"]] = relationship(back_populates="invoice", order_by="Reminder.level")

    @property
    def outstanding(self) -> Decimal:
        return self.gross_total - (self.paid_amount or Decimal("0"))

    @property
    def is_draft(self) -> bool:
        return self.status == "draft"

    @property
    def is_open(self) -> bool:
        return self.kind == "invoice" and self.status == "issued"

    def is_overdue(self, today: date) -> bool:
        return self.is_open and self.due_date is not None and self.due_date < today


class InvoiceLine(Base):
    __tablename__ = "invoice_lines"
    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoices.id"))
    position: Mapped[int] = mapped_column(Integer)
    description: Mapped[str] = mapped_column(Text)
    quantity: Mapped[Decimal] = mapped_column(Money)
    unit: Mapped[str] = mapped_column(String, default="Std.")
    unit_price: Mapped[Decimal] = mapped_column(Money)
    line_net: Mapped[Decimal] = mapped_column(Money)

    invoice: Mapped[Invoice] = relationship(back_populates="lines")


class AuditLog(Base):
    """Append-only. SQLite triggers forbid UPDATE and DELETE."""

    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    entity: Mapped[str] = mapped_column(String)
    entity_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    action: Mapped[str] = mapped_column(String)
    details: Mapped[str] = mapped_column(Text, default="")


class Payment(Base):
    """Money received for an invoice. Several payments per invoice and one bank transfer for
    several invoices are both possible."""

    __tablename__ = "payments"
    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoices.id"))
    date: Mapped[date] = mapped_column(Date)
    amount: Mapped[Decimal] = mapped_column(Money)
    transaction_id: Mapped[int | None] = mapped_column(ForeignKey("transactions.id"), nullable=True)

    invoice: Mapped[Invoice] = relationship(back_populates="payments")


class VatYear(Base):
    """VAT status per calendar year, so old years keep their status when settings change."""

    __tablename__ = "vat_years"
    year: Mapped[int] = mapped_column(Integer, primary_key=True)
    kleinunternehmer: Mapped[bool] = mapped_column(Boolean)


class Expense(Base):
    __tablename__ = "expenses"
    id: Mapped[int] = mapped_column(primary_key=True)
    date: Mapped[date] = mapped_column(Date)  # date paid (EÜR is cash based)
    vendor: Mapped[str] = mapped_column(String)
    description: Mapped[str] = mapped_column(Text, default="")
    vendor_invoice_number: Mapped[str] = mapped_column(String, default="")
    category: Mapped[str] = mapped_column(String)
    payment_method: Mapped[str] = mapped_column(String, default="bank")
    currency: Mapped[str] = mapped_column(String(3), default="EUR")
    amount_original: Mapped[Decimal | None] = mapped_column(Money, nullable=True)  # in foreign currency
    gross_eur: Mapped[Decimal] = mapped_column(Money)
    vat_rate: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    vat_eur: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    business_share: Mapped[Decimal] = mapped_column(Money, default=Decimal("100"))  # percent
    reverse_charge: Mapped[str] = mapped_column(String, default="")  # "" | eu | non_eu (§ 13b UStG)
    useful_life_years: Mapped[int | None] = mapped_column(Integer, nullable=True)  # assets > 800 € net
    transaction_id: Mapped[int | None] = mapped_column(ForeignKey("transactions.id"), nullable=True)
    source: Mapped[str] = mapped_column(String, default="manual")  # manual | bank | einvoice
    voided: Mapped[bool] = mapped_column(Boolean, default=False)
    void_reason: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    receipts: Mapped[list["Receipt"]] = relationship(back_populates="expense", order_by="Receipt.id")

    @property
    def net_eur(self) -> Decimal:
        return self.gross_eur - self.vat_eur


class Receipt(Base):
    """An uploaded file, stored byte-for-byte as received (GoBD). Never changed or deleted."""

    __tablename__ = "receipts"
    id: Mapped[int] = mapped_column(primary_key=True)
    expense_id: Mapped[int] = mapped_column(ForeignKey("expenses.id"))
    original_name: Mapped[str] = mapped_column(String)
    path: Mapped[str] = mapped_column(String)
    sha256: Mapped[str] = mapped_column(String)
    size: Mapped[int] = mapped_column(Integer)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    expense: Mapped[Expense] = relationship(back_populates="receipts")


class OtherIncome(Base):
    """Income without an invoice from this app, e.g. payments for invoices written in Accountable."""

    __tablename__ = "other_income"
    id: Mapped[int] = mapped_column(primary_key=True)
    date: Mapped[date] = mapped_column(Date)
    client_id: Mapped[int | None] = mapped_column(ForeignKey("clients.id"), nullable=True)
    description: Mapped[str] = mapped_column(Text, default="")
    gross_eur: Mapped[Decimal] = mapped_column(Money)
    vat_treatment: Mapped[str] = mapped_column(String)
    vat_eur: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    transaction_id: Mapped[int | None] = mapped_column(ForeignKey("transactions.id"), nullable=True)
    voided: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    client: Mapped[Client | None] = relationship()

    @property
    def net_eur(self) -> Decimal:
        return self.gross_eur - self.vat_eur


class BankImport(Base):
    __tablename__ = "bank_imports"
    id: Mapped[int] = mapped_column(primary_key=True)
    filename: Mapped[str] = mapped_column(String)
    sha256: Mapped[str] = mapped_column(String)
    profile: Mapped[str] = mapped_column(String)
    rows_total: Mapped[int] = mapped_column(Integer)
    rows_new: Mapped[int] = mapped_column(Integer)
    imported_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Transaction(Base):
    __tablename__ = "transactions"
    id: Mapped[int] = mapped_column(primary_key=True)
    import_id: Mapped[int] = mapped_column(ForeignKey("bank_imports.id"))
    booking_date: Mapped[date] = mapped_column(Date)
    amount: Mapped[Decimal] = mapped_column(Money)
    currency: Mapped[str] = mapped_column(String(3), default="EUR")
    counterparty: Mapped[str] = mapped_column(String, default="")
    iban: Mapped[str] = mapped_column(String, default="")
    purpose: Mapped[str] = mapped_column(Text, default="")
    reference: Mapped[str] = mapped_column(String, default="")
    dedupe_key: Mapped[str] = mapped_column(String, unique=True)
    # open | invoice | income | expense | private | tax | transfer | ignored
    status: Mapped[str] = mapped_column(String, default="open")
    tax_kind: Mapped[str] = mapped_column(String, default="")  # est | ust | other
    tax_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")

    expenses: Mapped[list[Expense]] = relationship(primaryjoin="Transaction.id == Expense.transaction_id",
                                                   viewonly=True)
    payments: Mapped[list[Payment]] = relationship(primaryjoin="Transaction.id == Payment.transaction_id",
                                                   viewonly=True)


class Reminder(Base):
    """Zahlungserinnerung / Mahnung. Archived as PDF like invoices."""

    __tablename__ = "reminders"
    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoices.id"))
    level: Mapped[int] = mapped_column(Integer)
    date: Mapped[date] = mapped_column(Date)
    new_due_date: Mapped[date] = mapped_column(Date)
    fee: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    pdf_path: Mapped[str] = mapped_column(String, default="")
    pdf_sha256: Mapped[str] = mapped_column(String, default="")

    invoice: Mapped[Invoice] = relationship(back_populates="reminders")


class TaxYear(Base):
    """Inputs for the income tax estimate that are not bookkeeping data."""

    __tablename__ = "tax_years"
    year: Mapped[int] = mapped_column(Integer, primary_key=True)
    homeoffice_days: Mapped[int] = mapped_column(Integer, default=0)
    sonderausgaben: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))  # e.g. health insurance
    prepayments_other: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))  # not visible in bank
