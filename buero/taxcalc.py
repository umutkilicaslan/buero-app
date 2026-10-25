"""Income tax estimate (single person, Grundtarif).

§ 32a EStG tariffs: 2025 and 2026 checked against gesetze-im-internet.de / buzer.de (Oct 2026),
2024 = tariff after the Steuerfortentwicklungsgesetz (Grundfreibetrag 11,784 €).
For later years the newest known tariff is used and the result is marked as provisional.
"""
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal

D = Decimal

# year: (grundfreibetrag, zone2_end, a2, zone3_end, a3, c3, c4, c5)
TARIFFS = {
    2024: (11784, 17005, D("954.80"), 66760, D("181.19"), D("991.21"), D("10636.31"), D("18971.06")),
    2025: (12096, 17443, D("932.30"), 68480, D("176.64"), D("1015.13"), D("10911.92"), D("19246.67")),
    2026: (12348, 17799, D("914.51"), 69878, D("173.10"), D("1034.87"), D("11135.63"), D("19470.38")),
}
ZONE5_START = 277826
# Solidaritätszuschlag: Freigrenze on the income tax amount (single)
SOLI_FREIGRENZE = {2024: D("18130"), 2025: D("19950"), 2026: D("20350")}
CHURCH_RATE_BERLIN = D("0.09")
GEWST_FREIBETRAG = D("24500")


def tariff_year(year: int) -> int:
    known = sorted(TARIFFS)
    return year if year in TARIFFS else (known[-1] if year > known[-1] else known[0])


def income_tax(zve: Decimal, year: int) -> Decimal:
    gfb, z2, a2, z3, a3, c3, c4, c5 = TARIFFS[tariff_year(year)]
    x = int(max(zve, D(0)).to_integral_value(ROUND_DOWN))
    if x <= gfb:
        tax = D(0)
    elif x <= z2:
        y = D(x - gfb) / 10000
        tax = (a2 * y + 1400) * y
    elif x <= z3:
        z = D(x - z2) / 10000
        tax = (a3 * z + 2397) * z + c3
    elif x < ZONE5_START:
        tax = D("0.42") * x - c4
    else:
        tax = D("0.45") * x - c5
    return tax.to_integral_value(ROUND_DOWN)


def soli(est: Decimal, year: int) -> Decimal:
    limit = SOLI_FREIGRENZE[tariff_year(year)]
    if est <= limit:
        return D(0)
    full = est * D("0.055")
    milder = (est - limit) * D("0.119")
    return min(full, milder).quantize(D("0.01"), ROUND_DOWN)


@dataclass
class Estimate:
    year: int
    profit: Decimal
    sonderausgaben: Decimal
    zve: Decimal
    income_tax: Decimal
    soli: Decimal
    church: Decimal
    gewerbesteuer: Decimal
    gewst_credit: Decimal
    total: Decimal
    provisional_tariff: bool

    @property
    def rate(self) -> Decimal:
        return (self.total / self.profit * 100).quantize(D("0.1")) if self.profit > 0 else D(0)


def estimate(year: int, profit: Decimal, sonderausgaben: Decimal, church: bool, gewerbe: bool,
             hebesatz: int = 410) -> Estimate:
    gewst = credit = D(0)
    if gewerbe and profit > GEWST_FREIBETRAG:
        ertrag = (profit / 100).to_integral_value(ROUND_DOWN) * 100 - GEWST_FREIBETRAG
        messbetrag = (ertrag * D("0.035")).to_integral_value(ROUND_DOWN)
        gewst = messbetrag * D(hebesatz) / 100
        credit_cap = messbetrag * 4  # § 35 EStG
    zve = max(profit - sonderausgaben, D(0))
    est = income_tax(zve, year)
    if gewerbe and gewst:
        credit = min(credit_cap, gewst, est)
        est -= credit
    s = soli(est, year)
    church_tax = (est * CHURCH_RATE_BERLIN).quantize(D("0.01")) if church else D(0)
    total = est + s + church_tax + gewst
    return Estimate(year, profit, sonderausgaben, zve, est, s, church_tax, gewst, credit, total,
                    year not in TARIFFS)
