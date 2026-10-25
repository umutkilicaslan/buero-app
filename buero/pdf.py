"""Invoice HTML/PDF rendering (WeasyPrint)."""
import json
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import money
from .models import Invoice

TEMPLATES = Path(__file__).parent / "templates"

LABELS = {
    "de": {
        "invoice": "Rechnung", "storno": "Stornorechnung", "number": "Rechnungsnummer", "date": "Rechnungsdatum",
        "service": "Leistungszeitraum", "service_date": "Leistungsdatum", "pos": "Pos.", "desc": "Beschreibung",
        "qty": "Menge", "unit": "Einheit", "price": "Einzelpreis", "amount": "Betrag", "net": "Nettobetrag",
        "vat": "Umsatzsteuer", "gross": "Gesamtbetrag", "total": "Rechnungsbetrag", "tax_number": "Steuernummer",
        "vat_id": "USt-IdNr.", "client_vat_id": "USt-IdNr. Leistungsempfänger", "bank": "Bankverbindung",
        "terms": "Bitte überweisen Sie den Rechnungsbetrag bis zum {due} unter Angabe der Rechnungsnummer.",
        "terms_storno": "Der Betrag wird mit der ursprünglichen Rechnung verrechnet bzw. erstattet.",
        "greeting": "Vielen Dank für die Zusammenarbeit.", "draft": "ENTWURF – keine gültige Rechnung",
    },
    "en": {
        "invoice": "Invoice", "storno": "Cancellation invoice", "number": "Invoice number", "date": "Invoice date",
        "service": "Service period", "service_date": "Date of service", "pos": "No.", "desc": "Description",
        "qty": "Qty", "unit": "Unit", "price": "Unit price", "amount": "Amount", "net": "Net amount",
        "vat": "VAT", "gross": "Total", "total": "Invoice total", "tax_number": "Tax number",
        "vat_id": "VAT ID", "client_vat_id": "Client VAT ID", "bank": "Bank details",
        "terms": "Please transfer the amount by {due}, quoting the invoice number.",
        "terms_storno": "This amount will be offset against or refunded for the original invoice.",
        "greeting": "Thank you for your business.", "draft": "DRAFT – not a valid invoice",
    },
}

_env = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=select_autoescape())


def _fmt_date(d, lang):
    return "" if d is None else (d.strftime("%d.%m.%Y") if lang == "de" else d.strftime("%d %b %Y"))


def render_html(inv: Invoice, seller: dict | None = None, client: dict | None = None, vat_note: str | None = None) -> str:
    """For issued invoices the frozen snapshots are used; for draft previews the caller passes live data."""
    lang = inv.language
    seller = seller if seller is not None else json.loads(inv.seller_snapshot)
    client = client if client is not None else json.loads(inv.client_snapshot)
    tpl = _env.get_template("invoice_pdf.html")
    return tpl.render(
        inv=inv, s=seller, c=client, L=LABELS[lang], lang=lang,
        vat_note=inv.vat_note if vat_note is None else vat_note,
        eur=lambda v: money.fmt_eur(v, lang), qty=lambda v: money.fmt_qty(v, lang),
        d=lambda v: _fmt_date(v, lang), rate=money.fmt_qty(inv.vat_rate, lang),
    )


def render_pdf(html: str, pdfa: bool = False) -> bytes:
    try:
        from weasyprint import HTML
    except OSError as e:  # missing system libraries (pango) on macOS
        raise RuntimeError("WeasyPrint needs Pango. On macOS run: brew install pango") from e
    kwargs = {"pdf_variant": "pdf/a-3b"} if pdfa else {}
    return HTML(string=html, base_url=str(TEMPLATES)).write_pdf(**kwargs)
