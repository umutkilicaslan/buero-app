from decimal import Decimal
from types import SimpleNamespace

from buero import vat


def c(**kw):
    base = dict(country="DE", is_business=True, vat_id="", exemption="", exemption_evidence="")
    return SimpleNamespace(**{**base, **kw})


def d(client, ku=False, vat_id="", override=""):
    return vat.decide(seller_kleinunternehmer=ku, seller_vat_id=vat_id, client=client, override=override)


def test_kleinunternehmer_domestic():
    r = d(c(), ku=True)
    assert r.treatment == "kleinunternehmer" and r.rate == 0
    assert r.note("de") == "Gemäß § 19 UStG wird keine Umsatzsteuer berechnet."


def test_regular_domestic_19():
    r = d(c())
    assert r.treatment == "domestic" and r.rate == Decimal("19") and r.note("de") == ""


def test_eu_business_with_vat_id_reverse_charge():
    r = d(c(country="AT", vat_id="ATU12345678"), vat_id="DE123456789")
    assert r.treatment == "reverse_charge" and r.rate == 0 and r.show_vat_ids
    assert "Reverse Charge" in r.note("de") and not r.warnings


def test_reverse_charge_also_for_kleinunternehmer_but_warns_without_own_vat_id():
    r = d(c(country="FR", vat_id="FR12345678901"), ku=True)
    assert r.treatment == "reverse_charge"
    assert any("USt-IdNr" in w for w in r.warnings)


def test_eu_business_without_vat_id_falls_back_with_warning():
    r = d(c(country="NL"), ku=False)
    assert r.treatment == "domestic" and r.warnings


def test_non_eu_business():
    r = d(c(country="US"), ku=False)
    assert r.treatment == "non_eu" and r.rate == 0 and "§ 3a Abs. 2" in r.note("de")
    assert d(c(country="CH"), ku=True).treatment == "non_eu"


def test_private_client_abroad_uses_german_rules():
    r = d(c(country="US", is_business=False), ku=False)
    assert r.treatment == "domestic" and r.warnings


def test_youth_welfare_exemption_beats_kleinunternehmer():
    r = d(c(exemption="4_25", exemption_evidence="Anerkennung § 75 SGB VIII, Senatsverwaltung 2019"), ku=True)
    assert r.treatment == "exempt_4_25" and r.rate == 0 and "§ 4 Nr. 25" in r.note("de") and not r.warnings


def test_exemption_without_evidence_warns():
    r = d(c(exemption="4_21"))
    assert r.treatment == "exempt_4_21" and r.warnings


def test_manual_override_warns():
    r = d(c(), ku=True, override="domestic")
    assert r.treatment == "domestic" and r.rate == Decimal("19") and r.warnings
