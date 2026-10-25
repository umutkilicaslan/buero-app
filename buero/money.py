"""Money helpers. All amounts are Decimal, rounded commercially to cents."""
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

CENT = Decimal("0.01")
ZERO = Decimal("0.00")


def round_cents(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def parse_decimal(text: str | None, default: Decimal | None = None) -> Decimal | None:
    """Parse '1.234,56', '1234,56' or '1234.56' into a Decimal."""
    if text is None or str(text).strip() == "":
        return default
    s = str(text).strip().replace("€", "").replace(" ", "")
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    try:
        return Decimal(s)
    except InvalidOperation:
        raise ValueError(f"Not a number: {text!r}")


def line_net(quantity: Decimal, unit_price: Decimal) -> Decimal:
    return round_cents(quantity * unit_price)


def totals(line_nets: list[Decimal], vat_rate: Decimal) -> tuple[Decimal, Decimal, Decimal]:
    """VAT is computed once on the invoice net total (not per line)."""
    net = round_cents(sum(line_nets, ZERO))
    vat = round_cents(net * vat_rate / Decimal(100))
    return net, vat, net + vat


def fmt_eur(value: Decimal | None, lang: str = "de") -> str:
    if value is None:
        return ""
    s = f"{round_cents(value):,.2f}"
    if lang == "de":
        s = s.replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{s} €"


def fmt_qty(value: Decimal, lang: str = "de") -> str:
    s = format(value.normalize(), "f")
    return s.replace(".", ",") if lang == "de" else s
