"""The local assistant, with a scripted fake Ollama: tools, proposals, apply, web flow."""
import json
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from buero import assistant, bank, db
from buero.models import AuditLog, Expense, Invoice, Transaction

DATA = Path(__file__).parent / "data"


def call(tool, /, **args):
    return {"message": {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": tool, "arguments": args}}]}}


def say(text):
    return {"message": {"role": "assistant", "content": text}}


class FakeOllama:
    """Returns the scripted answers in order and records what the app sent."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.requests = []

    def __call__(self, url, payload):
        self.requests.append(json.loads(json.dumps(payload)))
        return self.answers.pop(0)


@pytest.fixture
def conv():
    return assistant.conversation(None)


def test_invoice_draft_is_only_proposed_until_applied(s, client, conv, monkeypatch):
    fake = FakeOllama(
        call("find_clients", query="grenz"),
        call("propose_invoice_draft", client_id=client.id, service_from="2026-09-01", service_to="2026-09-30",
             lines=[{"description": "Zirkuspädagogik", "quantity": 12.5, "unit": "Std.", "unit_price": 29}]),
        say("<think>ok</think>Taslağı hazırladım, kontrol edip Uygula'ya bas."))
    monkeypatch.setattr(assistant, "_post", fake)
    reply = assistant.chat(s, conv, "GrenzKultur eylül 12,5 saat", "http://127.0.0.1:11434", "qwen3:8b")
    assert reply == "Taslağı hazırladım, kontrol edip Uygula'ya bas."
    # tools were offered, the system prompt came first, tool results went back to the model
    first = fake.requests[0]
    assert first["messages"][0]["role"] == "system" and first["options"]["num_ctx"] >= 8192
    assert {t["function"]["name"] for t in first["tools"]} >= {"find_clients", "propose_invoice_draft"}
    tool_msgs = [m for m in fake.requests[1]["messages"] if m["role"] == "tool"]
    assert "GrenzKultur" in tool_msgs[0]["content"]
    # nothing written yet
    assert s.scalars(select(Invoice)).first() is None
    (p,) = conv.proposals.values()
    assert p.kind == "invoice_draft" and ("Total", "362,50 €") in p.rows
    url = assistant.apply(s, p)
    s.commit()
    inv = s.scalars(select(Invoice)).one()
    assert url == f"/invoices/{inv.id}" and inv.status == "draft" and inv.number is None
    assert inv.lines[0].quantity == Decimal("12.5") and inv.gross_total == Decimal("362.50")
    assert inv.service_from == date(2026, 9, 1)
    assert s.scalar(select(AuditLog).where(AuditLog.action == "proposal_applied")) is not None


def test_bad_arguments_go_back_to_the_model(s, client, conv, monkeypatch):
    fake = FakeOllama(call("propose_invoice_draft", client_id=999, lines=[{"description": "x", "unit_price": 1}]),
                      call("propose_invoice_draft", client_id=client.id, lines=[]),
                      call("delete_everything"),
                      say("Hangi müşteri?"))
    monkeypatch.setattr(assistant, "_post", fake)
    assert assistant.chat(s, conv, "fatura yaz", "u", "m") == "Hangi müşteri?"
    errors = [json.loads(m["content"])["error"] for m in fake.requests[-1]["messages"] if m["role"] == "tool"]
    assert "no client with id 999" in errors[0] and "non-empty list" in errors[1] and "Unknown tool" in errors[2]
    assert conv.proposals == {}


def test_expense_and_bank_proposals(s, client, conv, monkeypatch):
    bank.import_csv(s, "k.csv", (DATA / "kontist_sample.csv").read_bytes())
    s.commit()
    out = next(t for t in s.scalars(select(Transaction)) if t.amount < 0 and t.status == "open")
    other = next(t for t in s.scalars(select(Transaction)) if t.status == "open" and t.id != out.id)
    fake = FakeOllama(
        call("expense_categories"),
        call("propose_expense", date=str(out.booking_date), vendor=out.counterparty or "Shop", gross_eur=float(-out.amount),
             vat_rate=19, category="equipment", transaction_id=out.id),
        call("propose_bank_status", transaction_id=other.id, status="private"),
        say("İki öneri hazır."))
    monkeypatch.setattr(assistant, "_post", fake)
    assistant.chat(s, conv, "bunları düzenle", "u", "m")
    exp_p, st_p = conv.proposals.values()
    assert exp_p.kind == "expense" and st_p.kind == "bank_status"
    assistant.apply(s, exp_p)
    assistant.apply(s, st_p)
    s.commit()
    e = s.scalars(select(Expense)).one()
    assert e.gross_eur == -out.amount and e.vat_eur == (e.gross_eur - e.gross_eur / Decimal("1.19")).quantize(Decimal("0.01"))
    assert s.get(Transaction, out.id).status == "expense" and s.get(Transaction, other.id).status == "private"


def test_future_expense_rejected_at_proposal(s, conv):
    out = assistant.run_tool(s, conv, "propose_expense", {"date": str(date.today() + timedelta(days=3)), "vendor": "X",
                                                          "gross_eur": 10, "category": "equipment"})
    assert "future" in json.loads(out)["error"]


def test_reads_never_write(s, client, conv):
    for name, args in [("overview", {}), ("list_invoices", {"status": "open"}), ("report", {"kind": "euer"}),
                       ("report", {"kind": "tax"}), ("report", {"kind": "kleinunternehmer"}), ("report", {"kind": "ustva"}),
                       ("list_bank_transactions", {}), ("list_expenses", {"year": 2026})]:
        res = json.loads(assistant.run_tool(s, conv, name, args))
        assert "error" not in res, (name, res)
    assert not s.new and not s.dirty


def test_ollama_not_running_gives_a_clear_message(s, conv):
    with pytest.raises(assistant.AssistantError, match="Ollama is not running"):
        assistant.chat(s, conv, "merhaba", "http://127.0.0.1:9", "qwen3:8b")
    assert assistant.status("http://127.0.0.1:9", "qwen3:8b")["running"] is False


def test_web_flow(tmp_path, monkeypatch):
    monkeypatch.setenv("BUERO_DATA_DIR", str(tmp_path))
    db.init(tmp_path / "web.sqlite3")
    from buero.web import app
    c = TestClient(app)
    assert "Switch on the assistant" in c.get("/assistant").text
    assert "only talks to Ollama on this computer" in c.post("/settings/ai", data={"ai_enabled": "on",
                                                                                 "ai_url": "https://example.com"}).text
    r = c.post("/settings/ai", data={"ai_enabled": "on", "ai_url": "http://127.0.0.1:11434", "ai_model": "qwen3:4b"})
    assert "Assistant settings saved" in r.text and "✦ Assistant" in r.text
    monkeypatch.setattr(assistant, "status", lambda url, model: {"running": True, "installed": True, "models": [model]})
    assert "What should I do?" in c.get("/assistant").text
    c.post("/clients/save", data=dict(name="Katakids gUG", street="Weg 3", postal_code="10115", city="Berlin",
                                      country="DE", is_business="on", default_hourly_rate="29,00"))
    fake = FakeOllama(call("propose_client", name="Zirkus Berlin e.V.", city="Berlin", country="DE", exemption="4_25"),
                      say("Müşteriyi hazırladım."))
    monkeypatch.setattr(assistant, "_post", fake)
    d = c.post("/assistant/send", json={"text": "Zirkus Berlin e.V. diye yeni müşteri ekle"}).json()
    assert fake.requests[0]["model"] == "qwen3:4b"
    assert "Zirkus Berlin e.V." in d["html"] and "Apply" in d["html"] and not d["error"]
    pid = next(iter(assistant.CONVERSATIONS[d["c"]].proposals))
    d2 = c.post(f"/assistant/proposals/{pid}/apply").json()
    assert "Saved" in d2["html"] and "Open →" in d2["html"]
    assert "Zirkus Berlin e.V." in c.get("/clients").text
    # applying twice does nothing
    assert "Saved" in c.post(f"/assistant/proposals/{pid}/apply").json()["html"]
    assert c.post("/assistant/proposals/nope/apply").status_code == 404
    # page after reload shows the history
    assert "Müşteriyi hazırladım." in c.get(f"/assistant?c={d['c']}").text
    # Ollama error is shown in the chat, not as a crash
    def down(url, payload):
        raise assistant.AssistantError("Ollama is not running.")
    monkeypatch.setattr(assistant, "_post", down)
    assert "Ollama is not running" in c.post("/assistant/send", json={"c": d["c"], "text": "hi"}).json()["error"]
