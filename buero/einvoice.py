"""E-invoices (EN 16931): Factur-X / ZUGFeRD PDF and XRechnung XML out, ZUGFeRD/XRechnung in.

The XML is produced and parsed by the `factur-x` library and validated against the official XSD
before it is used. The PDF is written as PDF/A-3 with the XML embedded.
"""
import json
from decimal import Decimal

import facturx

from .models import Invoice

XRECHNUNG_ID = "urn:cen.eu:en16931:2017#compliant#urn:xeinkauf.de:kosit:xrechnung_3.0"
FACTURX_ID = "urn:cen.eu:en16931:2017"

# EN 16931 VAT category per treatment
CATEGORY = {
    "domestic": ("S", None),
    "kleinunternehmer": ("E", "Kleinunternehmer gemäß § 19 UStG"),
    "exempt_4_25": ("E", "Steuerfrei gemäß § 4 Nr. 25 UStG"),
    "exempt_4_21": ("E", "Steuerfrei gemäß § 4 Nr. 21 UStG"),
    "reverse_charge": ("AE", "Reverse Charge"),
    "non_eu": ("O", "Nicht im Inland steuerbar (§ 3a Abs. 2 UStG)"),
}
UNIT_CODES = {"std": "HUR", "std.": "HUR", "h": "HUR", "stunde": "HUR", "stunden": "HUR", "hour": "HUR",
              "hours": "HUR", "tag": "DAY", "tage": "DAY", "day": "DAY", "days": "DAY", "stk": "H87",
              "stk.": "H87", "stück": "H87", "pcs": "H87", "monat": "MON", "month": "MON", "pauschal": "LS",
              "psch.": "LS", "flat": "LS", "km": "KMT"}


def _amt(x: Decimal) -> str:
    return f"{x:.2f}"


def _qty(x: Decimal) -> str:
    return format(x.normalize(), "f")


def build_data(inv: Invoice, xrechnung: bool = False) -> dict:
    seller = json.loads(inv.seller_snapshot)
    buyer = json.loads(inv.client_snapshot)
    storno = inv.kind == "storno"
    sign = Decimal(-1) if storno else Decimal(1)  # credit note (381) carries positive amounts
    cat, reason = CATEGORY[inv.vat_treatment]
    rate = inv.vat_rate if cat == "S" else Decimal("0")
    lines = []
    for ln in inv.lines:
        line = {
            "BT-126": str(ln.position),
            "BT-153": ln.description[:200] or "Leistung",
            "BT-129": _qty(ln.quantity * sign),
            "BT-130": UNIT_CODES.get((ln.unit or "").strip().lower(), "C62"),
            "BT-146": _amt(ln.unit_price),
            "BT-131": _amt(ln.line_net * sign),
            "BT-151": cat,
        }
        if cat != "O":
            line["BT-152"] = _amt(rate)
        lines.append(line)
    seller_party = {
        "name": seller.get("business_name") or seller["name"],
        "addr_l1": seller["street"], "postcode": seller["postal_code"], "city": seller["city"],
        "country_code": "DE",
    }
    if seller.get("vat_id") and cat != "O":
        seller_party["vat_identifier"] = seller["vat_id"]
    if seller.get("tax_number"):
        seller_party["tax_identifier"] = seller["tax_number"]
    contact = {k: v for k, v in (("name", seller["name"]), ("phone", seller.get("phone")),
                                 ("email", seller.get("email"))) if v}
    seller_party["contacts"] = [contact]
    if seller.get("email"):
        seller_party["einvoicing_addr"] = seller["email"]
        seller_party["einvoicing_addr_schemeid"] = "EM"
    buyer_party = {
        "name": buyer["name"], "addr_l1": buyer.get("street"), "postcode": buyer.get("postal_code"),
        "city": buyer.get("city"), "country_code": (buyer.get("country") or "DE").upper(),
    }
    if buyer.get("vat_id") and cat != "O":
        buyer_party["vat_identifier"] = buyer["vat_id"]
    if xrechnung and buyer.get("email"):
        buyer_party["einvoicing_addr"] = buyer["email"]
        buyer_party["einvoicing_addr_schemeid"] = "EM"

    net = inv.net_total * sign
    tax = inv.vat_total * sign
    breakdown = {"BT-116": _amt(net), "BT-117": _amt(tax), "BT-118": cat}
    if cat != "O":
        breakdown["BT-119"] = _amt(rate)
    if reason:
        breakdown["BT-120"] = reason
    data = {
        "BT-24": XRECHNUNG_ID if xrechnung else FACTURX_ID,
        "BT-1": inv.number,
        "BT-2": inv.issue_date,
        "BT-3": "381" if storno else "380",
        "BT-5": "EUR",
        "BT-9": inv.due_date,
        "BT-10": buyer.get("leitweg_id") or (inv.number if xrechnung else None),
        "BT-20": f"Zahlbar bis {inv.due_date:%d.%m.%Y}" if inv.due_date and not storno else None,
        "BT-72": inv.service_to or inv.service_from,
        "BT-73": inv.service_from,
        "BT-74": inv.service_to or inv.service_from,
        "BG-1": [{"BT-22": n} for n in (inv.intro_text, inv.vat_note) if n],
        "BG-4": seller_party,
        "BG-7": buyer_party,
        "BT-81": "58",  # SEPA credit transfer
        "BT-83": inv.number,
        "BT-84": seller.get("iban"),
        "BT-86": seller.get("bic") or None,
        "BG-23": [breakdown],
        "BG-25": lines,
        "BT-106": _amt(net),
        "BT-109": _amt(net),
        "BT-110": _amt(tax),
        "BT-112": _amt(net + tax),
        "BT-115": _amt(net + tax),
    }
    if storno and inv.cancels:
        data["BG-3"] = [{"BT-25": inv.cancels.number, "BT-26": inv.cancels.issue_date}]
    if inv.vat_treatment == "reverse_charge" or cat == "E":
        pass
    return {k: v for k, v in data.items() if v not in (None, "", [])}


def cii_xml(inv: Invoice, xrechnung: bool = False) -> bytes:
    xml = facturx.generate_cii_xml(build_data(inv, xrechnung), level="en16931", check_xsd=True,
                                   check_schematron=False)
    if isinstance(xml, str):
        xml = xml.encode("utf-8")
    if xrechnung:  # the library always writes the Factur-X guideline ID; XRechnung needs its own
        xml = xml.replace(f"<ram:ID>{FACTURX_ID}</ram:ID>".encode(), f"<ram:ID>{XRECHNUNG_ID}</ram:ID>".encode(), 1)
    facturx.xml_check_xsd(xml, flavor="factur-x", level="en16931")
    return xml


def embed(pdf_bytes: bytes, xml: bytes, inv: Invoice) -> bytes:
    seller = json.loads(inv.seller_snapshot)
    meta = {"author": seller["name"], "keywords": "Factur-X, ZUGFeRD, Rechnung",
            "title": f"Rechnung {inv.number}", "subject": f"Rechnung {inv.number}"}
    return facturx.generate_from_binary(pdf_bytes, xml, flavor="factur-x", level="en16931", check_xsd=False,
                                        pdf_metadata=meta, lang="de-DE" if inv.language == "de" else "en-GB")


# ---------- incoming ----------

def read_incoming(filename: str, content: bytes) -> dict:
    """Extract the key fields from an incoming XRechnung (XML) or ZUGFeRD/Factur-X (PDF)."""
    if filename.lower().endswith(".pdf"):
        _name, xml = facturx.get_xml_from_pdf(content, check_xsd=False)
        if not xml:
            raise ValueError("This PDF contains no e-invoice XML. Upload it as a normal receipt.")
    else:
        xml = content
    data = facturx.parse_ubl_cii_xml(xml, check_xsd=False, float_as="string")
    if isinstance(data, tuple):
        data = data[-1]
    seller = data.get("BG-4") or {}
    breakdown = data.get("BG-23") or []
    rate = Decimal(breakdown[0].get("BT-119") or 0) if breakdown else Decimal(0)
    category = breakdown[0].get("BT-118") if breakdown else ""
    return {
        "vendor": seller.get("name", ""),
        "vendor_invoice_number": data.get("BT-1", ""),
        "date": data.get("BT-2"),
        "currency": data.get("BT-5", "EUR"),
        "net": Decimal(str(data.get("BT-109") or 0)),
        "vat": Decimal(str(data.get("BT-110") or 0)),
        "gross": Decimal(str(data.get("BT-112") or 0)),
        "vat_rate": rate,
        "reverse_charge": category == "AE",
        "seller_country": (seller.get("country_code") or "").upper(),
        "description": "; ".join(l.get("BT-153", "") for l in (data.get("BG-25") or [])[:5]),
    }
