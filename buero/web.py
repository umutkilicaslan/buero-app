import hashlib
import json
from html import escape
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlencode

from fastapi import Body, Depends, FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import (assistant, backup, bank, bookkeeping, catalog, config, db, deadlines, einvoice, export, ledger, money, pdf,
               reminders, services, taxcalc, vat)
from .models import (AuditLog, BankImport, Client, Expense, Invoice, InvoiceLine, OtherIncome, Receipt, Reminder,
                     Transaction, VatYear)
from .services import BusinessError

HERE = Path(__file__).parent
DOCS = HERE.parent / "docs"
templates = Jinja2Templates(directory=HERE / "templates")
templates.env.globals.update(eur=money.fmt_eur, qty=money.fmt_qty, vat_labels=vat.LABELS,
                             categories=catalog.CATEGORIES, category_by_key=catalog.CATEGORY_BY_KEY,
                             euer_lines=catalog.EUER_LINES, payment_methods=bookkeeping.PAYMENT_METHODS)
templates.env.globals["data_dir"] = config.data_dir
templates.env.filters["d"] = lambda v: v.strftime("%d.%m.%Y") if v else ""

app = FastAPI(title="Büro")
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

TX_STATUS = {"open": "To do", "invoice": "Invoice paid", "income": "Income", "expense": "Expense",
             "private": "Private", "tax": "Tax office", "transfer": "Own transfer", "ignored": "Ignored"}


def get_session():
    with db.SessionLocal() as s:
        yield s


def page(request: Request, name: str, s: Session, **ctx):
    ctx.setdefault("settings", services.get_settings(s))
    ctx.setdefault("today", date.today())
    ctx.setdefault("pdf_error", pdf.engine_error())
    ctx.setdefault("nav_open_tx", s.scalar(select(func.count()).select_from(Transaction)
                                           .where(Transaction.status == "open")))
    return templates.TemplateResponse(request, name, ctx)


def back(url: str, msg: str = "", err: str = "") -> RedirectResponse:
    url, _, fragment = url.partition("#")
    q = urlencode({k: v for k, v in (("msg", msg), ("err", err)) if v})
    sep = "&" if "?" in url else "?"
    return RedirectResponse(f"{url}{sep + q if q else ''}{'#' + fragment if fragment else ''}", status_code=303)


def _date(v) -> date | None:
    return date.fromisoformat(v) if v else None


def _dec(v, default=None) -> Decimal | None:
    return money.parse_decimal(v, default)


def _get(s: Session, model, id_):
    obj = s.get(model, id_)
    if obj is None:
        raise BusinessError("Not found.")
    return obj


@app.exception_handler(BusinessError)
async def business_error(request: Request, exc: BusinessError):
    return back(request.headers.get("referer", "/").split("?")[0], err=str(exc))


# ---------- dashboard ----------

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, s: Session = Depends(get_session)):
    today = date.today()
    year = today.year
    open_inv = services.open_invoices(s)
    drafts = list(s.scalars(select(Invoice).where(Invoice.status == "draft").order_by(Invoice.id.desc())))
    eu = ledger.euer(s, year, until=today)
    revenue = sum((i.gross for i in ledger.income_items(s, date(year, 1, 1), today)), Decimal("0"))
    ty = s.get(bookkeeping.TaxYear, year)
    elapsed = (today - date(year, 1, 1)).days + 1
    projected_profit = money.round_cents(eu.profit * Decimal(365) / Decimal(elapsed)) if elapsed > 30 else eu.profit
    st = services.get_settings(s)
    est_full = taxcalc.estimate(year, projected_profit, ty.sonderausgaben if ty else Decimal(0), st.church_tax,
                                st.business_type == "gewerbe", st.gewerbesteuer_hebesatz)
    est_ytd = money.round_cents(est_full.total * Decimal(elapsed) / Decimal(365))
    paid = ledger.tax_payments(s, year)
    monthly = []
    for m in range(1, 13):
        start = date(year, m, 1)
        end = (date(year + (m == 12), m % 12 + 1, 1) - timedelta(days=1))
        monthly.append((start.strftime("%b"), sum((i.gross for i in ledger.income_items(s, start, end)), Decimal(0))))
    hour = datetime.now().hour
    greeting = "Good morning" if 5 <= hour < 12 else "Good afternoon" if hour < 18 else "Good evening"
    return page(request, "dashboard.html", s, year=year, open_invoices=open_inv, drafts=drafts, greeting=greeting,
                has_clients=bool(s.scalar(select(func.count()).select_from(Client))),
                has_invoices=bool(s.scalar(select(func.count()).select_from(Invoice))),
                overdue=[i for i in open_inv if i.is_overdue(today)],
                open_total=sum((i.outstanding for i in open_inv), Decimal("0")),
                problems=services.check_numbering(s), revenue=revenue, euer=eu,
                est_full=est_full, est_ytd=est_ytd, tax_paid=paid, set_aside=max(est_ytd - paid, Decimal(0)),
                projected_profit=projected_profit, ku=ledger.ku_status(s, year),
                is_ku=services.is_kleinunternehmer(s, year), deadlines=deadlines.upcoming(s, today),
                missing_receipts=len(bank.missing_receipts(s)),
                open_tx=s.scalar(select(func.count()).select_from(Transaction).where(Transaction.status == "open")),
                monthly=monthly, monthly_max=max((v for _, v in monthly), default=Decimal(0)) or Decimal(1))


# ---------- settings ----------

@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, s: Session = Depends(get_session)):
    year = date.today().year
    years = sorted({year, year - 1, year - 2} | {v.year for v in s.scalars(select(VatYear))}, reverse=True)
    return page(request, "settings.html", s, year=year, next_number=services.next_number_preview(s, year),
                vat_years=[(y, services.is_kleinunternehmer(s, y), s.get(VatYear, y) is not None) for y in years],
                backups=sorted(config.backups_dir().glob("*.sqlite3"))[-5:],
                ai_status=assistant.status(st.ai_url, st.ai_model) if (st := services.get_settings(s)).ai_enabled else None)


@app.post("/settings/ai")
async def settings_ai(request: Request, s: Session = Depends(get_session)):
    f = await request.form()
    url = (f.get("ai_url") or assistant.DEFAULT_URL).strip()
    if not url.startswith(("http://127.0.0.1", "http://localhost", "http://[::1]")):
        return back("/settings#ai", err="The assistant only talks to Ollama on this computer (http://127.0.0.1:…).")
    services.save_settings(s, {"ai_enabled": f.get("ai_enabled") == "on", "ai_url": url,
                               "ai_model": (f.get("ai_model") or assistant.DEFAULT_MODEL).strip()})
    s.commit()
    return back("/settings#ai", msg="Assistant settings saved.")


@app.post("/settings")
async def settings_save(request: Request, s: Session = Depends(get_session)):
    f = await request.form()
    text_fields = ("name", "business_name", "street", "postal_code", "city", "email", "phone", "tax_number",
                   "vat_id", "profession", "bank_name", "iban", "bic", "business_type", "invoice_language",
                   "ustva_period", "backup_dir")
    data = {k: f.get(k, "").strip() for k in text_fields}
    data["iban"] = data["iban"].replace(" ", "").upper()
    data["vat_id"] = data["vat_id"].replace(" ", "").upper()
    for k in ("kleinunternehmer", "ist_versteuerung", "dauerfristverlaengerung", "church_tax", "einvoice"):
        data[k] = f.get(k) == "on"
    data["number_digits"] = int(f.get("number_digits") or 2)
    data["payment_terms_days"] = int(f.get("payment_terms_days") or 14)
    data["gewerbesteuer_hebesatz"] = int(f.get("gewerbesteuer_hebesatz") or 410)
    data["reminder_fee"] = _dec(f.get("reminder_fee"))
    if data["iban"] and not _iban_ok(data["iban"]):
        return back("/settings", err="The IBAN is not valid. Please check it.")
    services.save_settings(s, data)
    s.commit()
    return back("/settings", msg="Settings saved.")


def _iban_ok(iban: str) -> bool:
    if len(iban) < 15 or not iban[:2].isalpha():
        return False
    digits = "".join(str(int(c, 36)) for c in iban[4:] + iban[:4])
    return int(digits) % 97 == 1


@app.post("/settings/vat-year")
async def vat_year_save(request: Request, s: Session = Depends(get_session)):
    f = await request.form()
    services.set_vat_year(s, int(f["year"]), f.get("kleinunternehmer") == "1")
    s.commit()
    return back("/settings", msg=f"VAT status for {f['year']} saved.")


@app.post("/settings/sequence")
async def sequence_save(request: Request, s: Session = Depends(get_session)):
    f = await request.form()
    try:
        services.set_next_number(s, int(f["year"]), int(f["next_number"]))
        s.commit()
    except ValueError as e:
        return back("/settings", err=str(e))
    return back("/settings", msg="Numbering updated.")


@app.post("/settings/backup")
def backup_now(s: Session = Depends(get_session)):
    st = services.get_settings(s)
    if not st.backup_dir:
        return back("/settings", err="Choose a backup folder first (e.g. an external disk or iCloud Drive folder).")
    try:
        out = backup.make_backup(Path(st.backup_dir))
    except OSError as e:
        return back("/settings", err=f"Backup failed: {e}")
    services.audit(s, "backup", None, "created", file=str(out))
    s.commit()
    return back("/settings", msg=f"Backup written: {out}")


# ---------- clients ----------

@app.get("/clients", response_class=HTMLResponse)
def clients(request: Request, s: Session = Depends(get_session)):
    rows = []
    for c in s.scalars(select(Client).order_by(Client.name)):
        total = sum((p.amount for i in c.invoices for p in i.payments), Decimal(0))
        open_ = sum((i.outstanding for i in c.invoices if i.is_open), Decimal(0))
        rows.append((c, total, open_))
    return page(request, "clients.html", s, rows=rows)


@app.get("/clients/new", response_class=HTMLResponse)
def client_new(request: Request, s: Session = Depends(get_session)):
    return page(request, "client_form.html", s, client=Client(country="DE", is_business=True, exemption=""))


@app.get("/clients/{cid}", response_class=HTMLResponse)
def client_edit(cid: int, request: Request, s: Session = Depends(get_session)):
    c = _get(s, Client, cid)
    invs = list(s.scalars(select(Invoice).where(Invoice.client_id == cid).order_by(Invoice.id.desc())))
    return page(request, "client_form.html", s, client=c, invoices=invs)


@app.post("/clients/save")
async def client_save(request: Request, s: Session = Depends(get_session)):
    f = await request.form()
    cid = f.get("id")
    client = s.get(Client, int(cid)) if cid else None
    data = {k: f.get(k, "").strip() for k in ("name", "contact_person", "street", "postal_code", "city",
                                              "country", "vat_id", "email", "exemption", "exemption_evidence",
                                              "notes", "leitweg_id")}
    data["is_business"] = f.get("is_business") == "on"
    data["vat_id"] = data["vat_id"].replace(" ", "").upper()
    try:
        data["default_hourly_rate"] = _dec(f.get("default_hourly_rate"))
        client = services.save_client(s, client, data)
        s.commit()
    except (BusinessError, ValueError) as e:
        return back(f"/clients/{cid}" if cid else "/clients/new", err=str(e))
    return back(f"/clients/{client.id}", msg="Client saved.")


# ---------- invoices ----------

@app.get("/invoices", response_class=HTMLResponse)
def invoices(request: Request, status: str = "", year: int | None = None, s: Session = Depends(get_session)):
    q = select(Invoice).order_by(Invoice.year.desc().nulls_first(), Invoice.seq.desc().nulls_first(), Invoice.id.desc())
    if status == "open":
        q = q.where(Invoice.kind == "invoice", Invoice.status == "issued")
    elif status == "overdue":
        q = q.where(Invoice.kind == "invoice", Invoice.status == "issued", Invoice.due_date < date.today())
    elif status:
        q = q.where(Invoice.status == status)
    if year:
        q = q.where(Invoice.year == year)
    rows = list(s.scalars(q))
    years = sorted({y for (y,) in s.execute(select(Invoice.year).where(Invoice.year.is_not(None)).distinct())}, reverse=True)
    return page(request, "invoices.html", s, invoices=rows, status=status, year=year, years=years,
                clients=list(s.scalars(select(Client).order_by(Client.name))))


@app.post("/invoices/new")
async def invoice_new(request: Request, s: Session = Depends(get_session)):
    f = await request.form()
    client = _get(s, Client, int(f["client_id"]))
    inv = services.new_draft(s, client)
    if client.default_hourly_rate:
        inv.lines.append(InvoiceLine(position=1, description="", quantity=Decimal("1"), unit="Std.",
                                     unit_price=client.default_hourly_rate,
                                     line_net=money.line_net(Decimal("1"), client.default_hourly_rate)))
    services._recalc(s, inv)
    s.commit()
    return back(f"/invoices/{inv.id}")


@app.post("/invoices/{iid}/duplicate")
def invoice_duplicate(iid: int, s: Session = Depends(get_session)):
    inv = services.copy_as_draft(s, _get(s, Invoice, iid))
    s.commit()
    return back(f"/invoices/{inv.id}", msg="Copied into a new draft. Check the dates.")


@app.get("/invoices/{iid}", response_class=HTMLResponse)
def invoice_page(iid: int, request: Request, s: Session = Depends(get_session)):
    inv = s.get(Invoice, iid)
    if inv is None:
        return back("/invoices", err="Invoice not found.")
    ctx = dict(inv=inv, decision=services.vat_decision(s, inv))
    if inv.is_draft:
        y = (inv.issue_date or date.today()).year
        ctx.update(clients=list(s.scalars(select(Client).order_by(Client.name))),
                   problems=services.issue_problems(s, inv),
                   next_number=services.format_number(y, services.next_number_preview(s, y),
                                                      services.get_settings(s).number_digits))
        return page(request, "invoice_edit.html", s, **ctx)
    ctx.update(storno=s.scalar(select(Invoice).where(Invoice.cancels_invoice_id == inv.id)),
               log=list(s.scalars(select(AuditLog).where(AuditLog.entity == "invoice", AuditLog.entity_id == inv.id)
                                  .order_by(AuditLog.id))),
               can_remind=inv.is_overdue(date.today()) and len(inv.reminders) < 3,
               next_level=reminders.LEVELS[reminders.next_level(inv)]["de"],
               has_xml=any(a.action == "einvoice_embedded" for a in ctx_log(s, inv)))
    return page(request, "invoice_view.html", s, **ctx)


def ctx_log(s: Session, inv: Invoice) -> list[AuditLog]:
    return list(s.scalars(select(AuditLog).where(AuditLog.entity == "invoice", AuditLog.entity_id == inv.id)))


def _apply_draft_form(s: Session, inv: Invoice, f) -> None:
    header = {"client_id": int(f["client_id"]), "language": f.get("language", "de"),
              "issue_date": _date(f.get("issue_date")), "service_from": _date(f.get("service_from")),
              "service_to": _date(f.get("service_to")), "intro_text": f.get("intro_text", "").strip(),
              "vat_override": f.get("vat_override", "")}
    lines = []
    for d_, q_, u_, p_ in zip(f.getlist("description"), f.getlist("quantity"), f.getlist("unit"),
                              f.getlist("unit_price")):
        if not d_.strip() and not p_.strip():
            continue
        if not d_.strip():
            raise BusinessError("Every line needs a description.")
        lines.append({"description": d_.strip(), "quantity": _dec(q_, Decimal("1")),
                      "unit": u_.strip(), "unit_price": _dec(p_, Decimal("0"))})
    services.update_draft(s, inv, header, lines)


@app.post("/invoices/{iid}/save")
async def invoice_save(iid: int, request: Request, s: Session = Depends(get_session)):
    inv = _get(s, Invoice, iid)
    f = await request.form()
    try:
        _apply_draft_form(s, inv, f)
        if f.get("action") == "preview":  # show what is in the form, without saving it
            html = _draft_html(s, inv)
            s.rollback()
            return _pdf_or_html(html, f"Entwurf_{iid}.pdf")
        s.commit()
    except (BusinessError, ValueError) as e:
        s.rollback()
        return back(f"/invoices/{iid}", err=str(e))
    if f.get("action") == "issue":
        return await invoice_issue(iid, s)
    return back(f"/invoices/{iid}", msg="Draft saved.")


@app.post("/invoices/{iid}/issue")
async def invoice_issue(iid: int, s: Session = Depends(get_session)):
    inv = _get(s, Invoice, iid)
    try:
        services.issue(s, inv)
        note = _archive_pdf(s, inv)
        s.commit()
    except (BusinessError, RuntimeError) as e:
        s.rollback()
        return back(f"/invoices/{iid}", err=str(e))
    except OSError as e:
        s.rollback()
        return back(f"/invoices/{iid}", err=f"The PDF could not be created ({e}). Nothing was issued. "
                    "On a Mac run 'brew install pango' once, then try again.")
    return back(f"/invoices/{iid}", msg=f"Invoice {inv.number} issued and archived as PDF{note}.")


def _archive_pdf(s: Session, inv: Invoice) -> str:
    """Archive the PDF once. With e-invoices on, it is a PDF/A-3 with embedded EN 16931 XML.
    If the XML cannot be built, a normal PDF is archived and the reason is logged."""
    st = services.get_settings(s)
    html = pdf.render_html(inv)
    note = ""
    if st.einvoice:
        try:
            xml = einvoice.cii_xml(inv)
            data = einvoice.embed(pdf.render_pdf(html, pdfa=True), xml, inv)
            services.audit(s, "invoice", inv.id, "einvoice_embedded", standard="Factur-X EN 16931 / ZUGFeRD")
            note = " (e-invoice: ZUGFeRD/Factur-X)"
        except Exception as e:  # noqa: BLE001 - never block invoicing because of the XML
            services.audit(s, "invoice", inv.id, "einvoice_failed", error=str(e)[:500])
            data = pdf.render_pdf(html)
            note = " (without e-invoice XML, see history)"
    else:
        data = pdf.render_pdf(html)
    path = config.invoices_dir(inv.year) / services.pdf_filename(inv)
    services.attach_pdf(s, inv, data, path)
    return note


@app.get("/invoices/{iid}/xrechnung")
def invoice_xrechnung(iid: int, s: Session = Depends(get_session)):
    inv = _get(s, Invoice, iid)
    if inv.is_draft:
        return back(f"/invoices/{iid}", err="Issue the invoice first.")
    seller, buyer = json.loads(inv.seller_snapshot), json.loads(inv.client_snapshot)
    missing = [label for label, ok in (("your phone number", seller.get("phone")), ("your email", seller.get("email")),
                                       ("the client's email", buyer.get("email"))) if not ok]
    if missing:
        return back(f"/invoices/{iid}", err="XRechnung requires " + ", ".join(missing) +
                    ". These were not set when the invoice was issued; add them and issue the next invoice again.")
    try:
        xml = einvoice.cii_xml(inv, xrechnung=True)
    except Exception as e:  # noqa: BLE001
        return back(f"/invoices/{iid}", err=f"XRechnung could not be created: {e}")
    return Response(xml, media_type="application/xml",
                    headers={"Content-Disposition": f'attachment; filename="XRechnung_{inv.number}.xml"'})


@app.post("/invoices/{iid}/delete")
def invoice_delete(iid: int, s: Session = Depends(get_session)):
    services.delete_draft(s, _get(s, Invoice, iid))
    s.commit()
    return back("/invoices", msg="Draft deleted.")


def _draft_html(s: Session, inv: Invoice) -> str:
    d = services.vat_decision(s, inv)
    return pdf.render_html(inv, services.seller_snapshot(services.get_settings(s)),
                           services.client_snapshot(inv.client, d.show_vat_ids), d.note(inv.language))


def _pdf_or_html(html: str, filename: str) -> Response:
    """The PDF, or if PDFs cannot be made on this computer, the same page as HTML with the reason on top."""
    try:
        return Response(pdf.render_pdf(html), media_type="application/pdf",
                        headers={"Content-Disposition": f'inline; filename="{filename}"'})
    except RuntimeError as e:
        banner = ('<div style="font:14px/1.5 -apple-system,sans-serif;background:#fbe9e7;color:#a3302a;'
                  'padding:12px 16px;margin:0 0 16px;border-radius:8px">'
                  f'<strong>Showing a web preview instead of the PDF.</strong> {escape(str(e))}</div>')
        return HTMLResponse(html.replace("<body>", "<body>" + banner, 1))


@app.post("/invoices/{iid}/live", response_class=HTMLResponse)
async def invoice_live(iid: int, request: Request, s: Session = Depends(get_session)):
    """The draft as it is in the form right now, for the preview next to the editor. Nothing is saved."""
    inv = _get(s, Invoice, iid)
    f = await request.form()
    try:
        _apply_draft_form(s, inv, f)
        return HTMLResponse(_draft_html(s, inv))
    except (BusinessError, ValueError) as e:
        return HTMLResponse(escape(str(e)), status_code=422)
    finally:
        s.rollback()


@app.get("/invoices/{iid}/preview", response_class=HTMLResponse)
def invoice_preview(iid: int, s: Session = Depends(get_session)):
    inv = _get(s, Invoice, iid)
    return HTMLResponse(_draft_html(s, inv) if inv.is_draft else pdf.render_html(inv))


@app.get("/invoices/{iid}/pdf")
def invoice_pdf(iid: int, s: Session = Depends(get_session)):
    inv = _get(s, Invoice, iid)
    if inv.is_draft:
        return _pdf_or_html(_draft_html(s, inv), f"Entwurf_{iid}.pdf")
    if not inv.pdf_path or not Path(inv.pdf_path).exists():
        return back(f"/invoices/{iid}", err="Archived PDF not found on disk. Restore it from your backup.")
    if hashlib.sha256(Path(inv.pdf_path).read_bytes()).hexdigest() != inv.pdf_sha256:
        return back(f"/invoices/{iid}", err="Warning: the archived PDF was changed on disk (checksum differs). "
                                            "Restore the original from your backup.")
    return FileResponse(inv.pdf_path, media_type="application/pdf", filename=Path(inv.pdf_path).name,
                        content_disposition_type="inline")


@app.post("/invoices/{iid}/paid")
async def invoice_paid(iid: int, request: Request, s: Session = Depends(get_session)):
    f = await request.form()
    try:
        services.mark_paid(s, _get(s, Invoice, iid), _date(f.get("paid_date")) or date.today(), _dec(f.get("paid_amount")))
        s.commit()
    except (BusinessError, ValueError) as e:
        return back(f"/invoices/{iid}", err=str(e))
    return back(f"/invoices/{iid}", msg="Payment recorded.")


@app.post("/invoices/{iid}/unpaid")
def invoice_unpaid(iid: int, s: Session = Depends(get_session)):
    services.unmark_paid(s, _get(s, Invoice, iid))
    s.commit()
    return back(f"/invoices/{iid}", msg="Payments removed.")


@app.post("/invoices/{iid}/storno")
async def invoice_storno(iid: int, request: Request, s: Session = Depends(get_session)):
    f = await request.form()
    original = _get(s, Invoice, iid)
    try:
        storno = services.cancel(s, original, reason=f.get("reason", "").strip())
        _archive_pdf(s, storno)
        new = services.copy_as_draft(s, original) if f.get("copy") == "on" else None
        s.commit()
    except (BusinessError, RuntimeError) as e:
        s.rollback()
        return back(f"/invoices/{iid}", err=str(e))
    if new:
        return back(f"/invoices/{new.id}", msg=f"Cancelled by {storno.number}. This is the new draft to replace it.")
    return back(f"/invoices/{storno.id}", msg=f"Cancellation invoice {storno.number} issued.")


@app.post("/invoices/{iid}/remind")
async def invoice_remind(iid: int, request: Request, s: Session = Depends(get_session)):
    f = await request.form()
    inv = _get(s, Invoice, iid)
    try:
        r = reminders.create(s, inv, days=int(f.get("days") or 10), fee=_dec(f.get("fee")))
        s.commit()
    except (BusinessError, ValueError, RuntimeError) as e:
        s.rollback()
        return back(f"/invoices/{iid}", err=str(e))
    return back(f"/invoices/{iid}", msg=f"{reminders.LEVELS[r.level]['de']} created.")


@app.get("/reminders/{rid}/pdf")
def reminder_pdf(rid: int, s: Session = Depends(get_session)):
    r = _get(s, Reminder, rid)
    return FileResponse(r.pdf_path, media_type="application/pdf", filename=Path(r.pdf_path).name,
                        content_disposition_type="inline")


# ---------- expenses ----------

@app.get("/expenses", response_class=HTMLResponse)
def expenses(request: Request, year: int | None = None, category: str = "", missing: int = 0,
             s: Session = Depends(get_session)):
    year = year or date.today().year
    q = select(Expense).where(Expense.date >= date(year, 1, 1), Expense.date <= date(year, 12, 31)) \
        .order_by(Expense.date.desc(), Expense.id.desc())
    if category:
        q = q.where(Expense.category == category)
    rows = list(s.scalars(q))
    if missing:
        rows = [e for e in rows if not e.receipts and e.category not in ("meals_travel", "car_km") and not e.voided]
    totals: dict[str, Decimal] = {}
    for e in rows:
        if not e.voided:
            totals[e.category] = totals.get(e.category, Decimal(0)) + e.gross_eur
    years = sorted({d.year for (d,) in s.execute(select(Expense.date))} | {date.today().year}, reverse=True)
    return page(request, "expenses.html", s, rows=rows, year=year, years=years, category=category,
                totals=sorted(totals.items(), key=lambda kv: -kv[1]), missing=missing,
                total=sum((e.gross_eur for e in rows if not e.voided), Decimal(0)))


def _expense_form(request, s, expense, **extra):
    return page(request, "expense_form.html", s, e=expense, **extra)


@app.get("/expenses/new", response_class=HTMLResponse)
def expense_new(request: Request, tx: int | None = None, s: Session = Depends(get_session)):
    e = Expense(date=date.today(), currency="EUR", business_share=Decimal(100), vat_rate=Decimal(19),
                payment_method="bank", reverse_charge="", category="")
    t = None
    if tx:
        t = _get(s, Transaction, tx)
        for k, v in bank.suggest_expense(s, t).items():
            if v is not None:
                setattr(e, k, v)
        e.transaction_id = t.id
        e.source = "bank"
    return _expense_form(request, s, e, tx=t)


@app.get("/expenses/{eid}", response_class=HTMLResponse)
def expense_edit(eid: int, request: Request, s: Session = Depends(get_session)):
    e = _get(s, Expense, eid)
    return _expense_form(request, s, e, tx=s.get(Transaction, e.transaction_id) if e.transaction_id else None,
                         log=list(s.scalars(select(AuditLog).where(AuditLog.entity == "expense", AuditLog.entity_id == eid)
                                            .order_by(AuditLog.id))),
                         schedule=ledger.depreciation_schedule(e, services.is_kleinunternehmer(s, e.date.year))
                         if e.category in ledger.ASSET_CATEGORIES and not ledger.is_gwg(e) else None)


@app.post("/expenses/save")
async def expense_save(request: Request, s: Session = Depends(get_session)):
    f = await request.form()
    eid = f.get("id")
    expense = s.get(Expense, int(eid)) if eid else None
    try:
        rate_choice = f.get("vat_rate", "19")
        data = {
            "date": _date(f.get("date")), "vendor": f.get("vendor", "").strip(),
            "description": f.get("description", "").strip(),
            "vendor_invoice_number": f.get("vendor_invoice_number", "").strip(),
            "category": f.get("category", ""), "payment_method": f.get("payment_method", "bank"),
            "currency": (f.get("currency") or "EUR").upper()[:3], "amount_original": _dec(f.get("amount_original")),
            "gross_eur": _dec(f.get("gross_eur")), "vat_rate": _dec(rate_choice, Decimal(0)),
            "vat_eur": _dec(f.get("vat_eur")), "business_share": _dec(f.get("business_share"), Decimal(100)),
            "reverse_charge": f.get("reverse_charge", ""),
            "useful_life_years": int(f.get("useful_life_years")) if f.get("useful_life_years") else None,
        }
        if f.get("transaction_id"):
            data["transaction_id"] = int(f.get("transaction_id"))
            data["source"] = f.get("source") or "bank"
        elif f.get("source"):
            data["source"] = f.get("source")
        if data["currency"] == "EUR":
            data["amount_original"] = None
        expense = bookkeeping.save_expense(s, expense, data)
        uploads = [u for u in f.getlist("receipts") if getattr(u, "filename", "")]
        for u in uploads:
            bookkeeping.store_receipt(s, expense, u.filename, await u.read())
        token = f.get("pending_receipt")
        if token:
            pending = _pending_path(token)
            if pending and pending.exists():
                bookkeeping.store_receipt(s, expense, pending.name.split("__", 1)[1], pending.read_bytes())
                pending.unlink()
        s.commit()
    except (BusinessError, ValueError) as e:
        s.rollback()
        return back(f"/expenses/{eid}" if eid else "/expenses/new" + (f"?tx={f.get('transaction_id')}" if f.get("transaction_id") else ""), err=str(e))
    if f.get("next") == "bank":
        return back("/bank", msg="Expense saved.")
    return back(f"/expenses/{expense.id}", msg="Expense saved.")


@app.post("/expenses/{eid}/receipt")
async def expense_receipt(eid: int, request: Request, s: Session = Depends(get_session)):
    f = await request.form()
    e = _get(s, Expense, eid)
    uploads = [u for u in f.getlist("receipts") if getattr(u, "filename", "")]
    if not uploads:
        return back(f"/expenses/{eid}", err="Choose a file.")
    for u in uploads:
        bookkeeping.store_receipt(s, e, u.filename, await u.read())
    s.commit()
    return back(f"/expenses/{eid}", msg="Receipt stored.")


@app.post("/expenses/{eid}/void")
async def expense_void(eid: int, request: Request, s: Session = Depends(get_session)):
    f = await request.form()
    bookkeeping.void_expense(s, _get(s, Expense, eid), f.get("reason", ""))
    s.commit()
    return back(f"/expenses/{eid}", msg="Expense voided. It stays in the records but no longer counts.")


@app.get("/receipts/{rid}")
def receipt_file(rid: int, s: Session = Depends(get_session)):
    r = _get(s, Receipt, rid)
    if not bookkeeping.verify_receipt(r):
        return back(f"/expenses/{r.expense_id}", err="The stored file is missing or was changed (checksum differs).")
    return FileResponse(r.path, filename=r.original_name, content_disposition_type="inline")


def _pending_dir() -> Path:
    p = config.data_dir() / "pending"
    p.mkdir(exist_ok=True)
    return p


def _pending_path(token: str) -> Path | None:
    if "/" in token or ".." in token:
        return None
    return _pending_dir() / token


@app.post("/expenses/einvoice")
async def expense_from_einvoice(request: Request, s: Session = Depends(get_session)):
    f = await request.form()
    u = f.get("file")
    if not getattr(u, "filename", ""):
        return back("/expenses", err="Choose an XRechnung (.xml) or ZUGFeRD (.pdf) file.")
    content = await u.read()
    try:
        data = einvoice.read_incoming(u.filename, content)
    except Exception as e:  # noqa: BLE001
        return back("/expenses", err=f"Could not read the e-invoice: {e}")
    token = f"{hashlib.sha256(content).hexdigest()[:16]}__{Path(u.filename).name}"
    (_pending_dir() / token).write_bytes(content)
    rc = ""
    if data["reverse_charge"] or (data["seller_country"] and data["seller_country"] != "DE" and not data["vat"]):
        rc = "eu" if data["seller_country"] in vat.EU_COUNTRIES else "non_eu"
    e = Expense(date=data["date"] or date.today(), vendor=data["vendor"], description=data["description"],
                vendor_invoice_number=data["vendor_invoice_number"], currency=data["currency"],
                gross_eur=data["gross"], vat_rate=data["vat_rate"], vat_eur=data["vat"], business_share=Decimal(100),
                payment_method="bank", reverse_charge=rc, category="", source="einvoice")
    note = "Read from the e-invoice. Check the values, choose a category and save. The file is stored as receipt."
    if data["currency"] != "EUR":
        note += " The invoice is not in EUR: enter the EUR amount from your bank statement."
    return _expense_form(request, s, e, pending=token, notice=note)


# ---------- other income ----------

@app.get("/income", response_class=HTMLResponse)
def income_list(request: Request, year: int | None = None, s: Session = Depends(get_session)):
    year = year or date.today().year
    items = ledger.income_items(s, date(year, 1, 1), date(year, 12, 31))
    others = list(s.scalars(select(OtherIncome).where(OtherIncome.date >= date(year, 1, 1),
                                                      OtherIncome.date <= date(year, 12, 31)).order_by(OtherIncome.date.desc())))
    return page(request, "income.html", s, year=year, items=items, others=others,
                total=sum((i.gross for i in items), Decimal(0)),
                years=sorted({date.today().year, date.today().year - 1, date.today().year - 2}, reverse=True))


def _income_form(request, s, item, tx=None):
    return page(request, "income_form.html", s, o=item, tx=tx,
                clients=list(s.scalars(select(Client).order_by(Client.name))))


@app.get("/income/new", response_class=HTMLResponse)
def income_new(request: Request, tx: int | None = None, s: Session = Depends(get_session)):
    t = _get(s, Transaction, tx) if tx else None
    default = "kleinunternehmer" if services.is_kleinunternehmer(s, (t.booking_date if t else date.today()).year) else "domestic"
    o = OtherIncome(date=t.booking_date if t else date.today(), gross_eur=t.amount if t else None,
                    description=t.purpose if t else "", vat_treatment=default, transaction_id=t.id if t else None)
    if t:
        for c in s.scalars(select(Client)):
            if bank._similar(c.name, t.counterparty):
                o.client_id = c.id
                if c.exemption:
                    o.vat_treatment = f"exempt_{c.exemption}"
                break
    return _income_form(request, s, o, t)


@app.get("/income/{oid}", response_class=HTMLResponse)
def income_edit(oid: int, request: Request, s: Session = Depends(get_session)):
    o = _get(s, OtherIncome, oid)
    return _income_form(request, s, o, s.get(Transaction, o.transaction_id) if o.transaction_id else None)


@app.post("/income/save")
async def income_save(request: Request, s: Session = Depends(get_session)):
    f = await request.form()
    oid = f.get("id")
    item = s.get(OtherIncome, int(oid)) if oid else None
    try:
        data = {"date": _date(f.get("date")), "client_id": int(f["client_id"]) if f.get("client_id") else None,
                "description": f.get("description", "").strip(), "gross_eur": _dec(f.get("gross_eur")),
                "vat_treatment": f.get("vat_treatment"), "vat_eur": _dec(f.get("vat_eur"))}
        if f.get("transaction_id"):
            data["transaction_id"] = int(f["transaction_id"])
        item = bookkeeping.save_other_income(s, item, data)
        s.commit()
    except (BusinessError, ValueError) as e:
        s.rollback()
        return back(f"/income/{oid}" if oid else "/income/new", err=str(e))
    if f.get("next") == "bank":
        return back("/bank", msg="Income booked.")
    return back(f"/income/{item.id}", msg="Income saved.")


@app.post("/income/{oid}/void")
def income_void(oid: int, s: Session = Depends(get_session)):
    bookkeeping.void_other_income(s, _get(s, OtherIncome, oid))
    s.commit()
    return back("/income", msg="Income voided.")


# ---------- bank ----------

@app.get("/bank", response_class=HTMLResponse)
def bank_page(request: Request, status: str = "open", year: int | None = None, q: str = "",
              s: Session = Depends(get_session)):
    query = select(Transaction).order_by(Transaction.booking_date.desc(), Transaction.id.desc())
    if status and status != "all":
        query = query.where(Transaction.status == status)
    if year:
        query = query.where(Transaction.booking_date >= date(year, 1, 1), Transaction.booking_date <= date(year, 12, 31))
    rows = list(s.scalars(query))
    if q:
        ql = q.lower()
        rows = [t for t in rows if ql in f"{t.counterparty} {t.purpose} {t.amount}".lower()]
    rows = rows[:400]
    suggestions = {t.id: bank.suggest_invoices(s, t) for t in rows if t.status == "open" and t.amount > 0}
    counts = dict(s.execute(select(Transaction.status, func.count()).group_by(Transaction.status)).all())
    return page(request, "bank.html", s, rows=rows, status=status, year=year, q=q, suggestions=suggestions,
                counts=counts, tx_status=TX_STATUS, imports=list(s.scalars(select(BankImport).order_by(BankImport.id.desc()).limit(5))),
                open_invoices=services.open_invoices(s), ignore_default=date(date.today().year, 1, 1).isoformat())


@app.post("/bank/import")
async def bank_import(request: Request, s: Session = Depends(get_session)):
    f = await request.form()
    u = f.get("file")
    if not getattr(u, "filename", ""):
        return back("/bank", err="Choose your bank's CSV export.")
    content = await u.read()
    mapping = None
    if f.get("map_date"):
        mapping = {k: int(f.get(f"map_{k}")) for k in bank.FIELDS if f.get(f"map_{k}") not in (None, "")}
    try:
        imp = bank.import_csv(s, u.filename, content, mapping)
        s.commit()
    except BusinessError as e:
        s.rollback()
        try:
            header, _ = bank._sniff(bank.decode(content))
        except BusinessError:
            header = next(iter(__import__("csv").reader(bank.decode(content).splitlines()[:1], delimiter=";")), [])
        token = f"{hashlib.sha256(content).hexdigest()[:16]}__{Path(u.filename).name}"
        (_pending_dir() / token).write_bytes(content)
        return page(request, "bank_mapping.html", s, header=header, error=str(e), token=token, fields=bank.FIELDS)
    return back("/bank", msg=f"{imp.rows_new} new transactions imported ({imp.rows_total - imp.rows_new} already known).")


@app.post("/bank/import-mapped")
async def bank_import_mapped(request: Request, s: Session = Depends(get_session)):
    f = await request.form()
    path = _pending_path(f.get("token", ""))
    if not path or not path.exists():
        return back("/bank", err="Upload the file again.")
    mapping = {k: int(f.get(f"map_{k}")) for k in bank.FIELDS if f.get(f"map_{k}") not in (None, "")}
    try:
        imp = bank.import_csv(s, path.name.split("__", 1)[1], path.read_bytes(), mapping)
        s.commit()
        path.unlink()
    except BusinessError as e:
        s.rollback()
        return back("/bank", err=str(e))
    return back("/bank", msg=f"{imp.rows_new} new transactions imported.")


@app.post("/bank/{tid}/match")
async def bank_match(tid: int, request: Request, s: Session = Depends(get_session)):
    f = await request.form()
    ids = [int(x) for x in (f.get("invoice_ids") or "").split(",") if x] + [int(x) for x in f.getlist("invoice_id")]
    if not ids:
        return back("/bank", err="Select at least one invoice.")
    tx = _get(s, Transaction, tid)
    try:
        bank.apply_match(s, tx, [_get(s, Invoice, i) for i in ids])
        s.commit()
    except BusinessError as e:
        s.rollback()
        return back("/bank", err=str(e))
    return back("/bank", msg="Payment assigned to the invoice(s).")


@app.post("/bank/{tid}/status")
async def bank_status(tid: int, request: Request, s: Session = Depends(get_session)):
    f = await request.form()
    tx = _get(s, Transaction, tid)
    status = f.get("status", "open")
    year = int(f["tax_year"]) if f.get("tax_year") else (tx.tax_year or tx.booking_date.year)
    bank.set_status(s, tx, status, f.get("tax_kind") or tx.tax_kind or "est", year, f.get("note", ""))
    s.commit()
    return back(f"/bank?status={f.get('return_status', 'open')}", msg=f"Marked as {TX_STATUS[status].lower()}.")


@app.post("/bank/ignore-before")
async def bank_ignore_before(request: Request, s: Session = Depends(get_session)):
    f = await request.form()
    try:
        before = date.fromisoformat(f.get("before", ""))
    except ValueError:
        return back("/bank", err="Choose a date.")
    n = bank.ignore_open_before(s, before, f.get("note", ""))
    s.commit()
    return back("/bank", msg=f"{n} older transactions marked as ignored. You can undo each one under 'Ignored'.")


@app.post("/bank/{tid}/undo")
def bank_undo(tid: int, s: Session = Depends(get_session)):
    tx = _get(s, Transaction, tid)
    if tx.status == "invoice":
        for p in list(tx.payments):
            services.unmark_paid(s, p.invoice)
    elif tx.status == "expense":
        for e in tx.expenses:
            if not e.voided:
                bookkeeping.void_expense(s, e, "Bank assignment undone")
    elif tx.status == "income":
        for o in s.scalars(select(OtherIncome).where(OtherIncome.transaction_id == tx.id, OtherIncome.voided.is_(False))):
            bookkeeping.void_other_income(s, o)
    tx.status = "open"
    services.audit(s, "transaction", tx.id, "reset")
    s.commit()
    return back("/bank", msg="Assignment undone. The transaction is open again.")


# ---------- reports ----------

@app.get("/reports", response_class=HTMLResponse)
def reports(request: Request, year: int | None = None, tab: str = "euer", s: Session = Depends(get_session)):
    year = year or date.today().year
    st = services.get_settings(s)
    ctx = dict(year=year, tab=tab, years=list(range(date.today().year, date.today().year - 6, -1)),
               is_ku=services.is_kleinunternehmer(s, year))
    if tab == "euer":
        ctx["euer"] = ledger.euer(s, year)
    elif tab == "ustva":
        kind = st.ustva_period
        ctx["rows"] = [(label, ledger.ustva(s, a, b)) for label, a, b in ledger.periods(year, kind)]
        ctx["year_total"] = ledger.ustva(s, date(year, 1, 1), date(year, 12, 31))
    elif tab == "zm":
        ctx["rows"] = [(label, ledger.zm(s, a, b)) for label, a, b in ledger.periods(year, "quarterly")]
    elif tab == "ku":
        ctx["ku"] = ledger.ku_status(s, year)
        ctx["history"] = [(y, ledger.gesamtumsatz(s, y), services.is_kleinunternehmer(s, y))
                          for y in range(year - 3, year + 1)]
    elif tab == "tax":
        eu = ledger.euer(s, year)
        ty = bookkeeping.tax_year(s, year)
        s.commit()
        ctx.update(euer=eu, ty=ty, est=taxcalc.estimate(year, eu.profit, ty.sonderausgaben, st.church_tax,
                                                        st.business_type == "gewerbe", st.gewerbesteuer_hebesatz),
                   paid=ledger.tax_payments(s, year))
    elif tab == "assets":
        assets = []
        for e in s.scalars(select(Expense).where(Expense.category.in_(ledger.ASSET_CATEGORIES), Expense.voided.is_(False))
                           .order_by(Expense.date)):
            ku = services.is_kleinunternehmer(s, e.date.year)
            cost, _ = ledger.expense_base(e, ku)
            sched = {e.date.year: cost} if ledger.is_gwg(e) else ledger.depreciation_schedule(e, ku)
            before = sum((v for y, v in sched.items() if y < year), Decimal(0))
            this = sched.get(year, Decimal(0))
            if e.date.year <= year:
                assets.append((e, cost, before, this, cost - before - this, ledger.is_gwg(e)))
        ctx["assets"] = assets
    return page(request, "reports.html", s, **ctx)


@app.post("/reports/tax-year")
async def tax_year_save(request: Request, s: Session = Depends(get_session)):
    f = await request.form()
    year = int(f["year"])
    try:
        bookkeeping.save_tax_year(s, year, int(f.get("homeoffice_days") or 0), _dec(f.get("sonderausgaben"), Decimal(0)),
                                  _dec(f.get("prepayments_other"), Decimal(0)))
        s.commit()
    except (BusinessError, ValueError) as e:
        return back(f"/reports?year={year}&tab=tax", err=str(e))
    return back(f"/reports?year={year}&tab=tax", msg="Saved.")


@app.get("/reports/euer.pdf")
def euer_pdf(year: int, s: Session = Depends(get_session)):
    html = templates.get_template("euer_pdf.html").render(euer=ledger.euer(s, year), settings=services.get_settings(s),
                                                         today=date.today(), eur=money.fmt_eur)
    return _pdf_or_html(html, f"EUER_{year}.pdf")


@app.get("/export/{name}.csv")
def export_csv(name: str, year: int | None = None, s: Session = Depends(get_session)):
    funcs = {"invoices": export.invoices_csv, "payments": export.payments_csv, "expenses": export.expenses_csv,
             "income": export.income_csv, "transactions": export.transactions_csv}
    if name == "euer":
        data = export.euer_csv(s, year or date.today().year)
    elif name == "audit":
        data = export.audit_csv(s)
    elif name in funcs:
        data = funcs[name](s, year)
    else:
        return back("/reports?tab=export", err="Unknown export.")
    fname = f"{name}_{year}.csv" if year else f"{name}.csv"
    return Response(data, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@app.get("/export/archive.zip")
def export_archive(year: int, s: Session = Depends(get_session)):
    data = export.archive(s, year, DOCS)
    services.audit(s, "export", None, "archive", year=year)
    s.commit()
    return Response(data, media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="Buero_Export_{year}.zip"'})


# ---------- audit ----------

@app.get("/audit", response_class=HTMLResponse)
def audit_page(request: Request, s: Session = Depends(get_session)):
    rows = list(s.scalars(select(AuditLog).order_by(AuditLog.id.desc()).limit(500)))
    return page(request, "audit.html", s, rows=rows)


# ---------- assistant ----------

def _chat_html(request: Request, conv) -> str:
    return templates.get_template("_chat.html").render(request=request, conv=conv, eur=money.fmt_eur)


@app.get("/assistant", response_class=HTMLResponse)
def assistant_page(request: Request, c: str = "", s: Session = Depends(get_session)):
    st = services.get_settings(s)
    if not st.ai_enabled:
        return back("/settings#ai", err="Switch on the assistant first.")
    conv = assistant.conversation(c or None)
    return page(request, "assistant.html", s, conv=conv, chat_html=_chat_html(request, conv),
                example_client=s.scalar(select(Client.name).order_by(Client.id.desc()).limit(1)),
                ai_status=assistant.status(st.ai_url, st.ai_model))


@app.post("/assistant/send")
def assistant_send(request: Request, payload: dict = Body(...), s: Session = Depends(get_session)):
    st = services.get_settings(s)
    if not st.ai_enabled:
        return JSONResponse({"error": "The assistant is switched off in Settings."}, status_code=400)
    conv = assistant.conversation(payload.get("c") or None)
    error = ""
    try:
        assistant.chat(s, conv, str(payload.get("text", ""))[:4000], st.ai_url, st.ai_model)
    except (assistant.AssistantError, BusinessError) as e:
        s.rollback()
        error = str(e)
        conv.display.append({"role": "error", "text": error})
    return {"c": conv.id, "html": _chat_html(request, conv), "error": error}


@app.post("/assistant/proposals/{pid}/{action}")
def assistant_proposal(pid: str, action: str, request: Request, s: Session = Depends(get_session)):
    try:
        conv, p = assistant.find_proposal(pid)
    except BusinessError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    if p.status == "pending" and action == "dismiss":
        p.status = "dismissed"
    elif p.status == "pending" and action == "apply":
        try:
            p.url = assistant.apply(s, p)
            s.commit()
            p.status, p.error = "applied", ""
        except (BusinessError, ValueError, KeyError) as e:
            s.rollback()
            p.error = str(e) or "Could not be applied."
    note = {"applied": "Done, saved.", "dismissed": "Dismissed."}.get(p.status, "")
    if note and not p.error:
        conv.messages.append({"role": "user", "content": f"(App note: the user {'applied' if p.status == 'applied' else 'dismissed'} "
                                                       f"proposal {p.id}: {p.title}.)"})
        conv.messages.append({"role": "assistant", "content": "Noted."})
    return {"c": conv.id, "html": _chat_html(request, conv)}
