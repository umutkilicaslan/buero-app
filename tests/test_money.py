from decimal import Decimal

import pytest

from buero import money


def test_parse_german_and_english():
    assert money.parse_decimal("1.234,56") == Decimal("1234.56")
    assert money.parse_decimal("29,00") == Decimal("29.00")
    assert money.parse_decimal("29.5") == Decimal("29.5")
    assert money.parse_decimal("") is None
    with pytest.raises(ValueError):
        money.parse_decimal("abc")


def test_rounding_half_up():
    assert money.round_cents(Decimal("0.005")) == Decimal("0.01")
    assert money.round_cents(Decimal("2.675")) == Decimal("2.68")  # float would give 2.67


def test_line_net_fractional_hours():
    # 0.75 h at 29 €/h, as on real CABUWAZI invoices
    assert money.line_net(Decimal("0.75"), Decimal("29")) == Decimal("21.75")
    assert money.line_net(Decimal("23.17"), Decimal("29")) == Decimal("671.93")


def test_totals_vat_on_net_sum():
    net, vat, gross = money.totals([Decimal("33.33"), Decimal("33.33"), Decimal("33.34")], Decimal("19"))
    assert (net, vat, gross) == (Decimal("100.00"), Decimal("19.00"), Decimal("119.00"))
    net, vat, gross = money.totals([Decimal("10.05")], Decimal("19"))
    assert vat == Decimal("1.91") and gross == Decimal("11.96")


def test_format():
    assert money.fmt_eur(Decimal("1234.5")) == "1.234,50 €"
    assert money.fmt_eur(Decimal("1234.5"), "en") == "1,234.50 €"
