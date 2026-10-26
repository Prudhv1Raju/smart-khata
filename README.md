# Kirana Khata — voice-first udhaar ledger on Sarvam

A shopkeeper speaks a ledger note in Hinglish — *"Ramesh ko 2 kilo atta aur ek packet chai patti udhaar, Suresh ne 500 jama kiye"* — and it becomes ledger entries. Supplier bills are read from a photo. Customers who owe money get a polite reminder call.

| Step | Sarvam piece |
|---|---|
| Speech → text, biased to your product & customer names | **Saaras v4** `/speech-to-text` with `keyterms` |
| Text → structured entries (customer, udhaar/jama, item, qty) | **Sarvam-105B** chat, JSON mode |
| Supplier bill photo → supplier, bill no, GST, total, line items | **Sarvam Vision** Doc AI `/doc-ai/v1/job/extract` |
| Reminder voice preview | **Bulbul v3** `/text-to-speech` |
| Outbound reminder calls | **Samvaad** campaign (CSV export + webhook back) |

Amounts are computed by the app, not the LLM: a spoken rupee amount wins. Otherwise it's qty × your price list, with g→kg and ml→litre conversion. If the units don't match (e.g. grams of a per-packet item), the amount is left blank for the shopkeeper to fill. Nothing is saved until the shopkeeper confirms.

## Run

```bash
pip install -r requirements.txt
cp .env.example .env        # put your SARVAM_API_KEY in it
python -m uvicorn app:app --reload --port 8000
```

Open http://localhost:8000. The mic needs `localhost` or HTTPS. To use it from a phone, tunnel it (e.g. `ngrok http 8000`). Without that, the page falls back to uploading a recording or typing the note.

Tests (Sarvam calls are faked): `python -m pytest -q`

## Set up the reminder calls (Samvaad)

Campaigns are created from the Sarvam dashboard; there's no campaign-creation API.

1. **Agent:** create a Samvaad agent with fields `customer_name`, `amount_due` and `shop_name`, and instructions like:
   > You are calling on behalf of {shop_name}, a neighbourhood kirana store. Greet {customer_name} politely in Hindi. Tell them {amount_due} rupees is pending in their khata. Never threaten or pressure. Ask when they can pay and record it in `promised_date`. If they say they already paid, set `payment_disputed` = yes and say the shop will check. Keep it under 60 seconds.
2. **Phones:** in the app's **Khata** tab, add a 10-digit phone for each customer.
3. **CSV:** in **Yaad dilao**, download the **Campaign CSV**. Columns: `phone_number` (E.164), `customer_name`, `amount_due`, `shop_name`, `last_activity`.
4. **Campaign:** Deploy → Campaigns → upload the CSV and map the agent fields to its columns. Set the calling window (e.g. 10:00–19:00) and retries.
5. **Webhook (optional):** under Advanced, set the webhook to `https://<your-tunnel>/api/webhooks/samvaad`. Call outcomes (`output_agent_variables`, transcript) then show up under **Call results**.

## Files

- `app.py`: API routes, parsing prompt, pricing, bill schema, reminders, webhook
- `sarvam.py`: Sarvam API client (STT, chat, TTS, Doc AI)
- `db.py`: SQLite schema and the seed price list (edit prices via `POST /api/products`)
- `static/index.html`: mobile web UI (Bolo / Khata / Bills / Yaad dilao)
