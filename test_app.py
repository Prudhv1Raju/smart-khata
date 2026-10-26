"""Offline tests: Sarvam calls are faked, everything else (DB, parsing, pricing, CSV, webhook) is real."""
import os
import tempfile

os.environ["KHATA_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")

from fastapi.testclient import TestClient  # noqa: E402

import app as khata  # noqa: E402

client = TestClient(khata.app)


async def fake_transcribe(audio, filename, keyterms):
    assert "atta" in keyterms
    return {"transcript": "Ramesh ko do kilo atta aur paav kilo chai patti udhaar, Suresh ne 500 jama kiye",
            "language_code": "hi-IN"}


async def fake_chat_json(system, user, max_tokens=2000):
    assert "PRODUCT LIST" in user
    return {"entries": [
        {"customer": "Ramesh", "kind": "udhaar", "item": "atta", "qty": 2, "unit": "kg", "amount_spoken": None},
        {"customer": "Ramesh", "kind": "udhaar", "item": "chai pati", "qty": 1, "unit": "packet", "amount_spoken": None},
        {"customer": "Ramesh", "kind": "udhaar", "item": "dal", "qty": 500, "unit": "g", "amount_spoken": None},
        {"customer": "Ramesh", "kind": "udhaar", "item": "namak", "qty": 500, "unit": "g", "amount_spoken": None},
        {"customer": "Suresh", "kind": "payment", "item": None, "qty": None, "unit": None, "amount_spoken": 500},
        {"customer": "Ramesh", "kind": "udhaar", "item": "kesar", "qty": 1, "unit": "packet", "amount_spoken": None},
    ], "unclear": None}


async def fake_extract(file, filename, schema, language="en-IN", timeout_s=150):
    return {"job_id": "job_1", "status": "completed", "result": {"pages": [{"data": {
        "supplier_name": "Gupta Traders", "bill_number": "GT-104", "bill_date": "20-10-2026",
        "line_items": [{"item": "Aashirvaad Atta 10kg", "quantity": 5, "unit": "bag", "rate": 420, "amount": 2100}],
        "gst_amount": "105.00", "total_amount": "₹2,205.00"}}]}}


_real_chat_json = khata.sarvam.chat_json


async def fake_speak(text, language_code="hi-IN"):
    return b"RIFFfake"


khata.sarvam.transcribe = fake_transcribe
khata.sarvam.chat_json = fake_chat_json
khata.sarvam.extract_document = fake_extract
khata.sarvam.speak = fake_speak


def test_voice_to_ledger_flow():
    r = client.post("/api/voice", files={"audio": ("note.webm", b"fakeaudio", "audio/webm")})
    assert r.status_code == 200, r.text
    res = r.json()
    atta, chai, dal, namak, pay, kesar = res["entries"]
    assert atta["amount"] == 90 and atta["amount_source"].startswith("price list")
    assert chai["item"] == "chai patti" and chai["amount"] == 120  # fuzzy name match
    assert dal["amount"] == 60  # 500 g of a per-kg item
    assert namak["amount"] is None  # grams of a per-packet item: don't guess
    assert pay["kind"] == "payment" and pay["amount"] == 500 and pay["amount_source"] == "spoken"
    assert kesar["amount"] is None and kesar["amount_source"] == "missing"

    kesar["amount"] = 60
    namak["amount"] = 25
    r = client.post("/api/entries", json={"entries": res["entries"], "transcript": res["transcript"]})
    assert r.json() == {"saved": 6}

    bal = {c["name"]: c for c in client.get("/api/customers").json()}
    assert bal["Ramesh"]["balance"] == 355 and bal["Suresh"]["balance"] == -500
    s = client.get("/api/summary").json()
    assert s["receivable"] == 355 and s["collected_today"] == 500

    # Campaign CSV only includes debtors with a valid phone; webhook links back to them
    rid = bal["Ramesh"]["id"]
    client.patch(f"/api/customers/{rid}", json={"phone": "98765 43210"})
    csv_text = client.get("/api/campaign.csv").text
    assert "+919876543210,Ramesh,355" in csv_text and "Suresh" not in csv_text
    client.post("/api/webhooks/samvaad", json={
        "campaign_id": "c1", "user_phone_number": "+919876543210", "connectivity_status": "connected",
        "output_agent_variables": {"promised_date": "Sunday"},
        "interaction_transcript": [{"role": "agent", "en_text": "Hello Ramesh"}]})
    call = client.get("/api/calls").json()[0]
    assert call["name"] == "Ramesh" and "Sunday" in call["outcome"]

    rem = client.get(f"/api/reminders/{rid}").json()
    assert "355" in rem["text"] and rem["audio_b64"]


def test_bill_upload():
    r = client.post("/api/bills", files={"file": ("bill.jpg", b"img", "image/jpeg")})
    assert r.status_code == 200, r.text
    b = client.get("/api/bills").json()[0]
    assert b["supplier"] == "Gupta Traders" and b["total"] == 2205 and b["gst"] == 105
    client.patch(f"/api/bills/{b['id']}", json={"status": "paid"})
    assert client.get("/api/summary").json()["payable"] == 0


def test_missing_key_is_clear_error():
    os.environ.pop("SARVAM_API_KEY", None)
    khata.sarvam.chat_json = _real_chat_json
    r = client.post("/api/parse", json={"text": "Ramesh ko atta"})
    assert r.status_code == 502 and "SARVAM_API_KEY" in r.json()["detail"]


def test_password_gate():
    import base64
    khata.APP_PASSWORD, khata.WEBHOOK_TOKEN = "secret", "hooktok"
    try:
        r = client.get("/api/summary")
        assert r.status_code == 401 and r.headers["www-authenticate"].startswith("Basic")
        bad = base64.b64encode(b"x:wrong").decode()
        assert client.get("/api/summary", headers={"Authorization": f"Basic {bad}"}).status_code == 401
        good = base64.b64encode(b"anyone:secret").decode()
        assert client.get("/api/summary", headers={"Authorization": f"Basic {good}"}).status_code == 200
        assert client.post("/api/webhooks/samvaad", json={}).status_code == 401
        assert client.post("/api/webhooks/samvaad?token=hooktok", json={"user_phone_number": ""}).status_code == 200
    finally:
        khata.APP_PASSWORD = khata.WEBHOOK_TOKEN = ""
