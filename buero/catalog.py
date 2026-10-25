"""Expense categories and the Anlage EÜR lines they belong to.

Kennzahlen checked against the official Anlage EÜR (BMF form, unchanged codes 2022–2025) and the
2024 form change for the Homeoffice-Tagespauschale (own line, Kz 163). The form is published
every year; if a code changes, adjust EUER_LINES below. The app always shows the label too, so a
changed number is easy to spot.
"""
from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class Line:
    kz: str
    label: str
    section: str  # income | expense | info


EUER_LINES = {
    # Betriebseinnahmen
    "111": Line("111", "Betriebseinnahmen als umsatzsteuerlicher Kleinunternehmer", "income"),
    "119": Line("119", "davon nicht steuerbare Umsätze sowie steuerfreie Umsätze nach § 4 Nr. 8–29 UStG "
                       "(nicht im Gesamtumsatz nach § 19 UStG)", "info"),
    "112": Line("112", "Umsatzsteuerpflichtige Betriebseinnahmen", "income"),
    "103": Line("103", "Umsatzsteuerfreie, nicht umsatzsteuerbare Betriebseinnahmen sowie Betriebseinnahmen, "
                       "für die der Leistungsempfänger die Umsatzsteuer nach § 13b UStG schuldet", "income"),
    "140": Line("140", "Vereinnahmte Umsatzsteuer", "income"),
    "141": Line("141", "Vom Finanzamt erstattete und ggf. verrechnete Umsatzsteuer", "income"),
    # Betriebsausgaben
    "100": Line("100", "Waren, Rohstoffe und Hilfsstoffe einschl. Nebenkosten", "expense"),
    "110": Line("110", "Bezogene Fremdleistungen", "expense"),
    "130": Line("130", "AfA auf bewegliche Wirtschaftsgüter", "expense"),
    "131": Line("131", "AfA auf immaterielle Wirtschaftsgüter", "expense"),
    "132": Line("132", "Aufwendungen für geringwertige Wirtschaftsgüter (§ 6 Abs. 2 EStG)", "expense"),
    "150": Line("150", "Miete/Pacht für Geschäftsräume und betrieblich genutzte Grundstücke", "expense"),
    "280": Line("280", "Aufwendungen für Telekommunikation (z. B. Telefon, Internet)", "expense"),
    "221": Line("221", "Übernachtungs- und Reisenebenkosten bei Geschäftsreisen", "expense"),
    "281": Line("281", "Fortbildungskosten (ohne Reisekosten)", "expense"),
    "194": Line("194", "Kosten für Rechts- und Steuerberatung, Buchführung", "expense"),
    "222": Line("222", "Miete/Leasing für bewegliche Wirtschaftsgüter (ohne Kraftfahrzeuge)", "expense"),
    "225": Line("225", "Erhaltungsaufwendungen (ohne Gebäude und Kraftfahrzeuge)", "expense"),
    "223": Line("223", "Beiträge, Gebühren, Abgaben und Versicherungen (ohne Gebäude und Kraftfahrzeuge)", "expense"),
    "228": Line("228", "Laufende EDV-Kosten", "expense"),
    "229": Line("229", "Arbeitsmittel (z. B. Bürobedarf, Porto, Fachliteratur)", "expense"),
    "224": Line("224", "Werbekosten", "expense"),
    "185": Line("185", "Gezahlte Vorsteuerbeträge", "expense"),
    "186": Line("186", "An das Finanzamt gezahlte und ggf. verrechnete Umsatzsteuer", "expense"),
    "183": Line("183", "Übrige unbeschränkt abziehbare Betriebsausgaben", "expense"),
    "174": Line("174", "Geschenke (abziehbar)", "expense"),
    "175": Line("175", "Bewirtungsaufwendungen (abziehbar, 70 %)", "expense"),
    "165": Line("165", "Bewirtungsaufwendungen (nicht abziehbar, 30 %)", "info"),
    "171": Line("171", "Verpflegungsmehraufwendungen", "expense"),
    "163": Line("163", "Tagespauschale für die Tätigkeit in der häuslichen Wohnung (Homeoffice)", "expense"),
    "146": Line("146", "Sonstige tatsächliche Fahrtkosten (öffentliche Verkehrsmittel, Bahn, Flug, Taxi)", "expense"),
    "147": Line("147", "Fahrtkosten für nicht zum Betriebsvermögen gehörende Fahrzeuge (Nutzungseinlage)", "expense"),
}


@dataclass(frozen=True)
class Category:
    key: str
    label: str
    kz: str
    hint: str = ""
    default_vat: Decimal = Decimal("19")


CATEGORIES = [
    Category("equipment", "Work equipment & material (Arbeitsmittel, ≤ 800 € net)", "229",
             "Props, costumes for work, office supplies, books. Over 800 € net: choose 'Asset'."),
    Category("asset", "Asset over 800 € net (depreciation, AfA)", "130",
             "Spread over the useful life. Computers & software: 1 year allowed."),
    Category("asset_software", "Software licence over 800 € net (AfA, immaterial)", "131",
             "Bought software (not subscriptions). 1 year useful life allowed."),
    Category("software", "Software & IT subscriptions", "228"),
    Category("telecom", "Phone & internet", "280",
             "Only the business share. Without proof: 20 % of the bill, max. 20 €/month."),
    Category("transport", "Public transport, train, flights, taxi", "146",
             "Fully deductible for business trips (no fixed workplace)."),
    Category("car_km", "Private car for business trips (0.30 €/km)", "147",
             "Enter km × 0.30 € as amount, no VAT.", Decimal("0")),
    Category("travel", "Hotel & travel side costs", "221", default_vat=Decimal("7")),
    Category("meals_travel", "Meal allowance on trips (Verpflegungspauschale)", "171",
             "Flat rate: 14 € (> 8 h away), 28 € (24 h). No receipt, no VAT.", Decimal("0")),
    Category("education", "Training, courses, workshops", "281"),
    Category("advice", "Tax advice, bookkeeping, legal", "194"),
    Category("fees_insurance", "Fees, memberships, business insurance", "223",
             "Business liability insurance, professional associations. Health insurance is NOT a business expense.",
             Decimal("0")),
    Category("rent_movable", "Renting equipment or rooms by the hour", "222"),
    Category("repairs", "Repairs & maintenance", "225"),
    Category("advertising", "Advertising, website, business cards", "224"),
    Category("subcontractor", "Subcontractors (Fremdleistungen)", "110"),
    Category("goods", "Goods for resale", "100"),
    Category("office_rent", "Rent for business premises", "150", default_vat=Decimal("0")),
    Category("gifts", "Gifts to business partners (max. 50 €/person/year)", "174"),
    Category("hospitality", "Business meals (Bewirtung, 70 % deductible)", "175",
             "Keep the receipt with names of guests and occasion."),
    Category("bank_fees", "Bank & payment fees", "183", default_vat=Decimal("0")),
    Category("other", "Other business expense", "183"),
]
CATEGORY_BY_KEY = {c.key: c for c in CATEGORIES}

GWG_LIMIT = Decimal("800")  # net, § 6 Abs. 2 EStG
HOMEOFFICE_PER_DAY = Decimal("6")
HOMEOFFICE_MAX_DAYS = 210
