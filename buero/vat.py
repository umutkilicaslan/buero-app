"""Chooses the VAT treatment and the mandatory invoice note for a client.

Order of checks:
1. Domestic tax exemption recorded on the client (§ 4 Nr. 25 / § 4 Nr. 21 UStG).
2. Foreign business clients: place of supply is abroad (§ 3a Abs. 2 UStG), so neither
   German VAT nor § 19 applies. EU with VAT ID -> reverse charge; outside EU -> not taxable here.
3. Kleinunternehmer (§ 19 UStG).
4. Otherwise 19 % German VAT.
"""
from dataclasses import dataclass, field
from decimal import Decimal

EU_COUNTRIES = {
    "AT", "BE", "BG", "CY", "CZ", "DE", "DK", "EE", "ES", "FI", "FR", "GR", "HR", "HU", "IE", "IT",
    "LT", "LU", "LV", "MT", "NL", "PL", "PT", "RO", "SE", "SI", "SK",
}

STANDARD_RATE = Decimal("19")

NOTES = {
    "exempt_4_25": {
        "de": "Steuerfreie Leistung der Kinder- und Jugendhilfe gemäß § 4 Nr. 25 Satz 1 i. V. m. "
              "Satz 2 Buchst. b Doppelbuchst. bb UStG.",
        "en": "VAT-exempt child and youth welfare service pursuant to § 4 No. 25 sentence 1 in conjunction "
              "with sentence 2 letter b double letter bb UStG (German VAT Act).",
    },
    "exempt_4_21": {
        "de": "Umsatzsteuerfrei nach § 4 Nr. 21 Buchst. b UStG.",
        "en": "VAT-exempt pursuant to § 4 No. 21 letter b UStG (German VAT Act).",
    },
    "reverse_charge": {
        "de": "Steuerschuldnerschaft des Leistungsempfängers (Reverse Charge).",
        "en": "Reverse charge: VAT is payable by the recipient of the service.",
    },
    "non_eu": {
        "de": "Nicht im Inland steuerbare Leistung. Leistungsort beim Leistungsempfänger "
              "gemäß § 3a Abs. 2 UStG.",
        "en": "Service not taxable in Germany. Place of supply is the recipient's country "
              "pursuant to § 3a(2) UStG (German VAT Act).",
    },
    "kleinunternehmer": {
        "de": "Gemäß § 19 UStG wird keine Umsatzsteuer berechnet.",
        "en": "No VAT is charged pursuant to § 19 UStG (small business exemption).",
    },
    "domestic": {"de": "", "en": ""},
}

LABELS = {
    "exempt_4_25": "Steuerfrei § 4 Nr. 25 UStG (Jugendhilfe)",
    "exempt_4_21": "Steuerfrei § 4 Nr. 21 b UStG (Unterricht)",
    "reverse_charge": "Reverse Charge (EU business)",
    "non_eu": "Nicht steuerbar, § 3a Abs. 2 UStG (non-EU business)",
    "kleinunternehmer": "Kleinunternehmer § 19 UStG",
    "domestic": "19 % USt",
}


@dataclass
class VatDecision:
    treatment: str
    rate: Decimal
    reason: str
    warnings: list[str] = field(default_factory=list)
    show_vat_ids: bool = False

    def note(self, lang: str) -> str:
        return NOTES[self.treatment][lang]

    @property
    def label(self) -> str:
        return LABELS[self.treatment]


def decide(*, seller_kleinunternehmer: bool, seller_vat_id: str, client, override: str = "") -> VatDecision:
    country = (client.country or "DE").upper()
    warnings: list[str] = []
    decision = _automatic(seller_kleinunternehmer, country, client, warnings)

    if override and override != decision.treatment:
        warnings.append(f"Manually set to '{LABELS[override]}' instead of '{decision.label}'. "
                        "Make sure this is correct.")
        decision = VatDecision(override, STANDARD_RATE if override == "domestic" else Decimal("0"),
                               "Manual choice", warnings, show_vat_ids=override == "reverse_charge")

    if decision.treatment == "reverse_charge":
        if not seller_vat_id:
            warnings.append("Reverse-charge invoices must show your USt-IdNr. Apply for one at the BZSt "
                            "and enter it in Settings. You also have to file a Zusammenfassende Meldung.")
        if not client.vat_id:
            warnings.append("The client has no VAT ID on file.")
    if decision.treatment in ("exempt_4_25", "exempt_4_21") and not client.exemption_evidence.strip():
        warnings.append("No evidence recorded for this exemption (e.g. § 75 SGB VIII Anerkennungsbescheid "
                        "or § 4 Nr. 21 Bescheinigung). Add it on the client page.")
    decision.warnings = warnings
    return decision


def _automatic(ku: bool, country: str, client, warnings: list[str]) -> VatDecision:
    if country == "DE" and client.exemption == "4_25":
        return VatDecision("exempt_4_25", Decimal("0"), "Client is marked as recognised youth welfare provider")
    if country == "DE" and client.exemption == "4_21":
        return VatDecision("exempt_4_21", Decimal("0"), "Client is marked as certified education institution")

    if country != "DE" and client.is_business:
        if country in EU_COUNTRIES:
            if client.vat_id:
                return VatDecision("reverse_charge", Decimal("0"),
                                   "EU business client with VAT ID", show_vat_ids=True)
            warnings.append("EU business client without VAT ID: reverse charge needs the client's VAT ID. "
                            "Treated like a German invoice until you add it.")
        else:
            return VatDecision("non_eu", Decimal("0"), "Business client outside the EU")
    elif country != "DE":
        warnings.append("Private client abroad: German rules applied. Some services (e.g. electronic "
                        "services) follow other rules, check if unsure.")

    if ku:
        return VatDecision("kleinunternehmer", Decimal("0"), "You are a Kleinunternehmer")
    return VatDecision("domestic", STANDARD_RATE, "German VAT applies")
