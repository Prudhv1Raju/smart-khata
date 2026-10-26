"""Thin async client for the Sarvam APIs Bolo Khata uses."""
import asyncio
import base64
import json
import os

import httpx

BASE = "https://api.sarvam.ai"
STT_MODEL = os.getenv("STT_MODEL", "saaras:v4")
STT_LANGUAGE = os.getenv("STT_LANGUAGE", "unknown")
CHAT_MODEL = os.getenv("CHAT_MODEL", "sarvam-105b")
TTS_MODEL = os.getenv("TTS_MODEL", "bulbul:v3")
TTS_SPEAKER = os.getenv("TTS_SPEAKER", "shubh")


class SarvamError(RuntimeError):
    pass


def _key() -> str:
    key = os.getenv("SARVAM_API_KEY")
    if not key:
        raise SarvamError("SARVAM_API_KEY is not set. Add it to .env and restart.")
    return key


def _headers(json_body: bool = False) -> dict:
    h = {"api-subscription-key": _key()}
    if json_body:
        h["Content-Type"] = "application/json"
    return h


def _check(r: httpx.Response) -> dict:
    if r.status_code >= 400:
        try:
            msg = r.json().get("error", {}).get("message") or r.text
        except ValueError:
            msg = r.text
        raise SarvamError(f"Sarvam {r.request.url.path} -> {r.status_code}: {msg}")
    return r.json()


async def transcribe(audio: bytes, filename: str, keyterms: list[str]) -> dict:
    """Saaras v4 speech-to-text. Keyterms bias recognition toward known products/customers."""
    data = {"model": STT_MODEL, "language_code": STT_LANGUAGE}
    # Docs: JSON-encoded array in one form field, max 50 terms x 64 chars, v4 only.
    terms = [t[:64] for t in keyterms if t][:50]
    if terms and STT_MODEL == "saaras:v4":
        data["keyterms"] = json.dumps(terms, ensure_ascii=False)
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post(f"{BASE}/speech-to-text", headers=_headers(),
                         data=data, files={"file": (filename, audio)})
    return _check(r)


async def chat_json(system: str, user: str, max_tokens: int = 2000) -> dict:
    """Sarvam-105B in JSON mode; returns the parsed object."""
    body = {
        "model": CHAT_MODEL,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "temperature": 0.1,
        "max_tokens": max_tokens,
        "reasoning_effort": None,  # parsing task; reasoning tokens would eat max_tokens
        "response_format": {"type": "json_object"},
    }
    headers = _headers(json_body=True) | {"Authorization": f"Bearer {_key()}"}
    async with httpx.AsyncClient(timeout=90) as c:
        r = await c.post(f"{BASE}/v1/chat/completions", headers=headers, json=body)
    content = _check(r)["choices"][0]["message"]["content"] or ""
    content = content.strip()
    if content.startswith("```"):
        content = content.strip("`").removeprefix("json").strip()
    try:
        return json.loads(content)
    except json.JSONDecodeError as e:
        raise SarvamError(f"Model did not return JSON: {content[:300]}") from e


async def speak(text: str, language_code: str = "hi-IN") -> bytes:
    """Bulbul text-to-speech; returns WAV bytes."""
    body = {"text": text, "language_code": language_code, "model": TTS_MODEL}
    if TTS_SPEAKER:
        body["speaker"] = TTS_SPEAKER
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post(f"{BASE}/text-to-speech", headers=_headers(json_body=True), json=body)
    return base64.b64decode(_check(r)["audios"][0])


async def extract_document(file: bytes, filename: str, schema: dict,
                           language: str = "en-IN", timeout_s: int = 150) -> dict:
    """Sarvam Vision schema extraction: submit job, poll, fetch results."""
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post(f"{BASE}/doc-ai/v1/job/extract", headers=_headers(),
                         data={"schema": json.dumps(schema), "language": language,
                               "output_format": "json"},
                         files={"file": (filename, file)})
        job_id = _check(r)["job_id"]
        status = "pending"
        for _ in range(timeout_s // 3):
            await asyncio.sleep(3)
            status = _check(await c.get(f"{BASE}/doc-ai/v1/job/{job_id}/status",
                                        headers=_headers()))["status"]
            if status in ("completed", "partially_completed", "failed", "rejected"):
                break
        if status not in ("completed", "partially_completed"):
            raise SarvamError(f"Bill extraction job {job_id} ended with status '{status}'")
        res = _check(await c.get(f"{BASE}/doc-ai/v1/job/{job_id}/results", headers=_headers()))
    return {"job_id": job_id, "status": status, "result": res.get("result", res)}
