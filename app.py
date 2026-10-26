"""Smart Khata: voice-first credit ledger + supplier bills + dues reminders on Sarvam."""
import base64
import csv
import difflib
import io
import json
import os
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent


def _load_env():
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env()

from fastapi import FastAPI, File, HTTPException, Request, UploadFile  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse, Response  # noqa: E402
from pydantic import BaseModel  # noqa: E402

import db  # noqa: E402
import sarvam  # noqa: E402

SHOP_NAME = os.getenv("SHOP_NAME", "Sharma Kirana Store")
APP_PASSWORD = os.getenv("APP_PASSWORD", "")
WEBHOOK_TOKEN = os.getenv("WEBHOOK_TOKEN") or APP_PASSWORD

app = FastAPI(title="Smart Khata")


def _same(a: str, b: str) -> bool:
    return secrets.compare_digest(a.encode(), b.encode())


@app.middleware("http")
async def require_password(request: Request, call_next):
    """Shared-password gate so a public deploy doesn't expose the ledger or spend the Sarvam key."""
    if APP_PASSWORD:
        if request.url.path == "/api/webhooks/samvaad":  # Samvaad can't do Basic auth; use ?token=
            if not _same(request.query_params.get("token", ""), WEBHOOK_TOKEN):
                return Response(status_code=401)
        else:
            auth = request.headers.get("authorization", "")
            try:
                password = base64.b64decode(auth[6:]).decode().split(":", 1)[1] if auth[:6] == "Basic " else ""
            except (ValueError, IndexError):
                password = ""
            if not _same(password, APP_PASSWORD):
                return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="Smart Khata"'})
    return await call_next(request)


@app.exception_handler(sarvam.SarvamError)
async def sarvam_error(_: Request, exc: sarvam.SarvamError):
    return JSONResponse(status_code=502, content={"detail": str(exc)})


@app.get("/")
def index():
    return FileResponse(ROOT / "static" / "index.html")


# ---------- Voice entry -> structured khata entries ----------

PARSE_PROMPT = """You convert a kirana (Indian grocery) shopkeeper's spoken ledger note into JSON.
The note may be Hindi, English, Hinglish or another Indian language, in any script.

Return exactly: {"entries": [{"customer": str, "kind": "udhaar"|"payment", "item": str|null,
"qty": number|null, "unit": str|null, "amount_spoken": number|null}], "unclear": str|null}

Rules:
- "udhaar", "likh do", "khate mein", "baad mein dega", "credit" => kind "udhaar" (customer took goods on credit).
- "jama", "de gaye", "paise diye", "chuka diya", "payment", "wapas kiye" => kind "payment" (item null).
- One entry per item. If one customer takes 3 items, output 3 entries.
- customer: the person's name in Latin script, title case, without honorifics like ji/bhai/didi/aunty.
- item: use the closest name from the PRODUCT LIST when it matches, else the item as spoken, lowercase Latin script.
- unit: one of kg, g, litre, ml, packet, piece, dozen when determinable.
- Convert number words: aadha=0.5, paav=0.25, dedh=1.5, dhai=2.5, sava=1.25, dus=10, bees=20, sau=100, hazaar=1000.
- amount_spoken: only a rupee amount actually said for that entry; never compute it yourself.
- If something can't be understood, put a short note in "unclear" and skip it.
"""


class ParseIn(BaseModel):
    text: str


def _products() -> list[dict]:
    return db.rows("SELECT name, unit, price FROM products ORDER BY name")


def _keyterms() -> list[str]:
    names = [r["name"] for r in db.rows(
        "SELECT cu.name FROM customers cu LEFT JOIN entries e ON e.customer_id = cu.id "
        "GROUP BY cu.id ORDER BY MAX(e.created_at) DESC LIMIT 35")]
    return [p["name"] for p in _products()] + names


UNIT_SCALE = {"g": ("kg", 0.001), "gm": ("kg", 0.001), "gram": ("kg", 0.001),
              "ml": ("litre", 0.001), "dozen": ("piece", 12)}


def _price_entry(e: dict, products: list[dict]) -> dict:
    """Fill amount from spoken rupees, else qty x price list; flag what needs a human."""
    by_name = {p["name"].lower(): p for p in products}
    item = (e.get("item") or "").strip().lower() or None
    if item and item not in by_name:
        close = difflib.get_close_matches(item, by_name, n=1, cutoff=0.75)
        item = close[0] if close else item
    out = {"customer": (e.get("customer") or "").strip(), "kind": e.get("kind"),
           "item": item, "qty": e.get("qty"), "unit": e.get("unit"),
           "amount": e.get("amount_spoken"), "amount_source": "spoken"}
    if out["kind"] not in ("udhaar", "payment"):
        out["kind"] = "udhaar"
    if out["amount"] is None:
        out["amount_source"] = "missing"
        p = by_name.get(item or "")
        if p and out["qty"] is not None:
            qty, unit = float(out["qty"]), (out["unit"] or p["unit"]).lower()
            if unit in UNIT_SCALE and UNIT_SCALE[unit][0] == p["unit"]:
                qty, unit = qty * UNIT_SCALE[unit][1], p["unit"]
            if unit == p["unit"]:  # e.g. grams of a per-packet item: leave for the shopkeeper
                out["amount"] = round(qty * p["price"], 2)
                out["amount_source"] = f"price list (₹{p['price']:g}/{p['unit']})"
    return out


async def _parse(text: str) -> dict:
    products = _products()
    plist = ", ".join(f"{p['name']} ({p['unit']})" for p in products)
    known = ", ".join(r["name"] for r in db.rows("SELECT name FROM customers ORDER BY name"))
    user = f"PRODUCT LIST: {plist}\nKNOWN CUSTOMERS: {known or 'none yet'}\n\nNOTE: {text}"
    parsed = await sarvam.chat_json(PARSE_PROMPT, user)
    entries = [_price_entry(e, products) for e in parsed.get("entries") or []]
    return {"transcript": text, "entries": [e for e in entries if e["customer"]],
            "unclear": parsed.get("unclear")}


@app.post("/api/voice")
async def voice(audio: UploadFile = File(...)):
    data = await audio.read()
    if not data:
        raise HTTPException(400, "Empty recording")
    stt = await sarvam.transcribe(data, audio.filename or "note.webm", _keyterms())
    text = (stt.get("transcript") or "").strip()
    if not text:
        return {"transcript": "", "entries": [], "unclear": "Kuch sunai nahi diya — dobara boliye."}
    result = await _parse(text)
    result["language"] = stt.get("language_code")
    return result


@app.post("/api/parse")
async def parse_text(body: ParseIn):
    return await _parse(body.text)


class EntryIn(BaseModel):
    customer: str
    kind: str
    amount: float
    item: str | None = None
    qty: float | None = None
    unit: str | None = None


class SaveIn(BaseModel):
    entries: list[EntryIn]
    transcript: str | None = None


@app.post("/api/entries")
def save_entries(body: SaveIn):
    with db.conn() as c:
        for e in body.entries:
            if e.kind not in ("udhaar", "payment") or e.amount <= 0 or not e.customer.strip():
                raise HTTPException(400, f"Invalid entry: {e}")
            cid = db.get_or_create_customer(c, e.customer)
            c.execute("INSERT INTO entries(customer_id, kind, item, qty, unit, amount, transcript) "
                      "VALUES (?,?,?,?,?,?,?)",
                      (cid, e.kind, e.item, e.qty, e.unit, e.amount, body.transcript))
    return {"saved": len(body.entries)}


@app.delete("/api/entries/{entry_id}")
def delete_entry(entry_id: int):
    with db.conn() as c:
        c.execute("DELETE FROM entries WHERE id = ?", (entry_id,))
    return {"ok": True}


# ---------- Ledger ----------

@app.get("/api/customers")
def customers():
    return db.rows(db.BALANCES_SQL)


@app.get("/api/customers/{cid}/entries")
def customer_entries(cid: int):
    return db.rows("SELECT * FROM entries WHERE customer_id = ? ORDER BY created_at DESC, id DESC", cid)


class CustomerPatch(BaseModel):
    phone: str | None = None


@app.patch("/api/customers/{cid}")
def update_customer(cid: int, body: CustomerPatch):
    with db.conn() as c:
        c.execute("UPDATE customers SET phone = ? WHERE id = ?", (body.phone, cid))
    return {"ok": True}


@app.get("/api/summary")
def summary():
    bal = db.rows(db.BALANCES_SQL)
    payable = db.rows("SELECT COALESCE(SUM(total),0) AS t, COUNT(*) AS n FROM bills WHERE status='unpaid'")[0]
    today = db.rows("SELECT COALESCE(SUM(amount),0) AS t FROM entries "
                    "WHERE kind='payment' AND substr(created_at, 1, 10) = ?",
                    datetime.now(timezone.utc).strftime("%Y-%m-%d"))[0]  # timestamps are stored in UTC
    out = {"receivable": sum(b["balance"] for b in bal if b["balance"] > 0),
           "debtors": sum(1 for b in bal if b["balance"] > 0),
           "payable": payable["t"], "unpaid_bills": payable["n"],
           "collected_today": today["t"], "shop": SHOP_NAME, "storage": db.STORAGE,
           "version": os.getenv("VERCEL_GIT_COMMIT_SHA", "local")[:7]}
    if db.STORAGE == "sqlite-ephemeral":
        out["db_env_names"] = db.db_env_names()
    return out


class ProductIn(BaseModel):
    name: str
    unit: str
    price: float


@app.get("/api/products")
def products():
    return _products()


@app.post("/api/products")
def upsert_product(p: ProductIn):
    with db.conn() as c:
        c.execute("INSERT INTO products(name, unit, price) VALUES (?,?,?) "
                  "ON CONFLICT(name) DO UPDATE SET unit = excluded.unit, price = excluded.price",
                  (p.name.strip().lower(), p.unit.strip().lower(), p.price))
    return {"ok": True}


# ---------- Supplier bills (Sarvam Vision) ----------

BILL_SCHEMA = {
    "type": "object",
    "properties": {
        "supplier_name": {"type": "string", "description": "Name of the wholesaler/distributor that issued the bill"},
        "bill_number": {"type": "string", "description": "Invoice or bill number"},
        "bill_date": {"type": "string", "description": "Bill date as DD-MM-YYYY"},
        "line_items": {"type": "array", "description": "Each product row on the bill", "items": {
            "type": "object", "properties": {
                "item": {"type": "string", "description": "Product name"},
                "quantity": {"type": "number"},
                "unit": {"type": "string", "description": "kg, litre, packet, box, piece, etc."},
                "rate": {"type": "number", "description": "Price per unit"},
                "amount": {"type": "number", "description": "Line total"}}}},
        "gst_amount": {"type": "number", "description": "Total GST if printed as a single line"},
        "cgst_amount": {"type": "number", "description": "CGST amount"},
        "sgst_amount": {"type": "number", "description": "SGST amount"},
        "igst_amount": {"type": "number", "description": "IGST amount"},
        "total_amount": {"type": "number", "description": "Grand total payable including tax"},
    },
}


def _find_bill(obj):
    """Results nesting isn't pinned down in the docs; find the dict holding our fields."""
    if isinstance(obj, dict):
        if "total_amount" in obj or "supplier_name" in obj:
            return obj
        children = obj.values()
    elif isinstance(obj, list):
        children = obj
    else:
        return None
    for v in children:
        if isinstance(v, str) and v.lstrip().startswith("{"):
            try:
                v = json.loads(v)
            except ValueError:
                continue
        found = _find_bill(v)
        if found:
            return found
    return None


def _num(v):
    if isinstance(v, (int, float)):
        return float(v)
    m = re.search(r"-?\d[\d,]*\.?\d*", str(v or ""))
    return float(m.group().replace(",", "")) if m else None


@app.post("/api/bills")
async def upload_bill(file: UploadFile = File(...)):
    data = await file.read()
    job = await sarvam.extract_document(data, file.filename or "bill.jpg", BILL_SCHEMA)
    bill = _find_bill(job["result"]) or {}
    if _num(bill.get("gst_amount")) is None:  # bills usually print CGST/SGST separately
        parts = [_num(bill.get(k)) for k in ("cgst_amount", "sgst_amount", "igst_amount")]
        if any(p is not None for p in parts):
            bill["gst_amount"] = round(sum(p or 0 for p in parts), 2)
    with db.conn() as c:
        bid = db.insert(
            c, "INSERT INTO bills(supplier, bill_no, bill_date, total, gst, line_items, job_id) "
            "VALUES (?,?,?,?,?,?,?)",
            (bill.get("supplier_name"), bill.get("bill_number"), bill.get("bill_date"),
             _num(bill.get("total_amount")), _num(bill.get("gst_amount")),
             json.dumps(bill.get("line_items") or [], ensure_ascii=False), job["job_id"]))
    return {"id": bid, "extracted": bill, "job_status": job["status"]}


@app.get("/api/bills")
def bills():
    out = db.rows("SELECT * FROM bills ORDER BY status DESC, created_at DESC")
    for b in out:
        b["line_items"] = json.loads(b["line_items"] or "[]")
    return out


class BillPatch(BaseModel):
    status: str | None = None
    supplier: str | None = None
    total: float | None = None


@app.patch("/api/bills/{bid}")
def update_bill(bid: int, body: BillPatch):
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    if fields.get("status") not in (None, "paid", "unpaid"):
        raise HTTPException(400, "status must be paid or unpaid")
    if fields:
        with db.conn() as c:
            c.execute(f"UPDATE bills SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?",
                      (*fields.values(), bid))
    return {"ok": True}


# ---------- Reminders (Bulbul preview + Samvaad campaign) ----------

def _rupees(x: float) -> str:
    return f"{x:,.0f}"  # Bulbul docs: use commas in numbers above four digits


REMINDER = {
    "hi-IN": "नमस्ते {name} जी, मैं {shop} से बोल रहा हूँ। आपके खाते में {amount} रुपये बाकी हैं। "
             "जब सुविधा हो, दुकान पर आकर या UPI से चुका दीजिए। धन्यवाद!",
    "en-IN": "Hello {name} ji, this is {shop}. You have {amount} rupees pending in your khata. "
             "Please clear it at your convenience, at the shop or by UPI. Thank you!",
}


@app.get("/api/reminders/{cid}")
async def reminder_audio(cid: int, lang: str = "hi-IN"):
    cust = next((b for b in db.rows(db.BALANCES_SQL) if b["id"] == cid), None)
    if not cust:
        raise HTTPException(404, "Customer not found")
    text = REMINDER.get(lang, REMINDER["hi-IN"]).format(
        name=cust["name"], shop=SHOP_NAME, amount=_rupees(cust["balance"]))
    wav = await sarvam.speak(text, lang if lang in REMINDER else "hi-IN")
    return {"text": text, "audio_b64": base64.b64encode(wav).decode()}


def _e164(phone: str | None) -> str | None:
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) == 10:
        return "+91" + digits
    if len(digits) == 12 and digits.startswith("91"):
        return "+" + digits
    return None


@app.get("/api/campaign.csv")
def campaign_csv(min_due: float = 1):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["phone_number", "customer_name", "amount_due", "shop_name", "last_activity"])
    for b in db.rows(db.BALANCES_SQL):
        phone = _e164(b["phone"])
        if phone and b["balance"] >= min_due:
            w.writerow([phone, b["name"], _rupees(b["balance"]), SHOP_NAME, (b["last_activity"] or "")[:10]])
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=khata_dues_campaign.csv"})


@app.post("/api/webhooks/samvaad")
async def samvaad_webhook(req: Request):
    p = await req.json()
    phone = p.get("user_phone_number") or ""
    cust = next((b for b in db.rows("SELECT id, phone FROM customers")
                 if _e164(b["phone"]) and _e164(b["phone"])[-10:] == phone[-10:]), None)
    turns = p.get("interaction_transcript") or []
    with db.conn() as c:
        c.execute("INSERT INTO calls(phone, customer_id, campaign_id, status, outcome, transcript) "
                  "VALUES (?,?,?,?,?,?)",
                  (phone, cust["id"] if cust else None, p.get("campaign_id"),
                   p.get("connectivity_status") or p.get("completion_status"),
                   json.dumps(p.get("output_agent_variables") or {}, ensure_ascii=False),
                   "\n".join(f"{t.get('role')}: {t.get('en_text')}" for t in turns)))
    return {"ok": True}


@app.get("/api/calls")
def calls():
    return db.rows("SELECT ca.*, cu.name FROM calls ca LEFT JOIN customers cu ON cu.id = ca.customer_id "
                   "ORDER BY ca.created_at DESC LIMIT 100")
