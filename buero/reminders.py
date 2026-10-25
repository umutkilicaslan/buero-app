"""Payment reminders (Zahlungserinnerung, 1. and 2. Mahnung), archived as PDF."""
import hashlib
import json
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from . import config, money, pdf
from .models import Invoice, Reminder
from .services import BusinessError, audit

LEVELS = {
    1: {"de": "Zahlungserinnerung", "en": "Payment reminder"},
    2: {"de": "1. Mahnung", "en": "First reminder"},
    3: {"de": "2. Mahnung", "en": "Final reminder"},
}
TEXT = {
    "de": {
        1: "sicher ist es Ihrer Aufmerksamkeit entgangen: Die folgende Rechnung ist noch offen. "
           "Bitte überweisen Sie den Betrag bis zum {due}. Sollten Sie bereits gezahlt haben, betrachten Sie "
           "dieses Schreiben bitte als gegenstandslos.",
        2: "leider konnte ich bis heute keinen Zahlungseingang für die folgende Rechnung feststellen. "
           "Bitte überweisen Sie den offenen Betrag bis spätestens {due}.",
        3: "trotz meiner bisherigen Erinnerungen ist die folgende Rechnung weiterhin unbezahlt. Bitte "
           "überweisen Sie den offenen Betrag bis spätestens {due}. Nach Ablauf dieser Frist behalte ich mir "
           "weitere Schritte vor. Hinweis: Sie befinden sich in Zahlungsverzug (§ 286 BGB).",
        "greeting": "Sehr geehrte Damen und Herren,", "closing": "Mit freundlichen Grüßen",
    },
    "en": {
        1: "this is a friendly reminder that the following invoice is still open. Please transfer the amount "
           "by {due}. If you have already paid, please disregard this letter.",
        2: "we have not yet received payment for the following invoice. Please transfer the outstanding "
           "amount by {due} at the latest.",
        3: "despite previous reminders the following invoice remains unpaid. Please transfer the outstanding "
           "amount by {due} at the latest. After this date I reserve the right to take further steps.",
        "greeting": "Dear Sir or Madam,", "closing": "Kind regards",
    },
}


def next_level(inv: Invoice) -> int:
    return min(len(inv.reminders) + 1, 3)


def create(s: Session, inv: Invoice, today: date | None = None, days: int = 10, fee: Decimal | None = None) -> Reminder:
    today = today or date.today()
    if not inv.is_open:
        raise BusinessError("Only open invoices can be reminded.")
    if inv.due_date and inv.due_date >= today:
        raise BusinessError("The invoice is not overdue yet.")
    level = next_level(inv)
    if inv.reminders and level == 3 and len(inv.reminders) >= 3:
        raise BusinessError("Three reminders were sent already. Next step: Mahnbescheid (online-mahnantrag.de).")
    fee = fee if (fee is not None and level > 1) else Decimal("0")
    r = Reminder(invoice=inv, level=level, date=today, new_due_date=today + timedelta(days=days), fee=fee)
    s.add(r)
    s.flush()
    html = render_html(inv, r)
    content = pdf.render_pdf(html)
    path = config.reminders_dir(today.year) / f"{LEVELS[level]['de'].replace(' ', '_').replace('.', '')}_{inv.number}_{today:%Y-%m-%d}.pdf"
    path.write_bytes(content)
    r.pdf_path, r.pdf_sha256 = str(path), hashlib.sha256(content).hexdigest()
    audit(s, "invoice", inv.id, "reminder", level=level, due=r.new_due_date, fee=fee)
    return r


def render_html(inv: Invoice, r: Reminder) -> str:
    lang = inv.language
    T = TEXT[lang]
    d = (lambda x: x.strftime("%d.%m.%Y")) if lang == "de" else (lambda x: x.strftime("%d %b %Y"))
    return pdf._env.get_template("reminder_pdf.html").render(
        inv=inv, r=r, s=json.loads(inv.seller_snapshot), c=json.loads(inv.client_snapshot), lang=lang,
        title=LEVELS[r.level][lang], body=T[r.level].format(due=d(r.new_due_date)), T=T, d=d,
        eur=lambda v: money.fmt_eur(v, lang), total=inv.outstanding + r.fee, L=pdf.LABELS[lang])
