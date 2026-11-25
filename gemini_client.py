"""
CPF — Google Gemini client (18 Nov 2026).
Used for Agent 1's supplier-page reading: Gemini 3.5 Flash-Lite, with Haiku as the automatic backup.
Trial on 29 suppliers: same location answers as Haiku, closer catalogue calls, 0 failures, about a third
of Haiku's list price and $0 within Google's free-tier limits.

Returns the model's JSON text, or raises GeminiUnavailable so the caller falls back to Haiku.
A rate-limit or server error is NOT retried for long: the backup is faster than waiting.
"""
import atexit
import json
import os
import time
import urllib.error
import urllib.request

MODEL = os.environ.get("CPF_GEMINI_MODEL", "gemini-3.5-flash-lite")
PRICE = (0.30, 2.50)          # list price per million tokens in / out (free tier: $0 within limits)
STATS = {"calls": 0, "ok": 0, "fallbacks": 0, "in": 0, "out": 0}


class GeminiUnavailable(Exception):
    pass


def enabled() -> bool:
    return bool(os.environ.get("GEMINI_API_KEY")) and os.environ.get("CPF_ENRICH_PROVIDER", "gemini") == "gemini"


def message(system: str, user: str, max_tokens: int = 2000) -> str:
    STATS["calls"] += 1
    body = {"systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {"responseMimeType": "application/json", "maxOutputTokens": max_tokens,
                                 "temperature": 0}}
    req = urllib.request.Request(
        f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent",
        data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json", "x-goog-api-key": os.environ["GEMINI_API_KEY"]})
    for attempt in range(2):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                out = json.loads(r.read())
            break
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 503) and attempt == 0:
                time.sleep(5)
                continue
            STATS["fallbacks"] += 1
            raise GeminiUnavailable(f"Gemini HTTP {e.code}")
        except Exception as e:
            STATS["fallbacks"] += 1
            raise GeminiUnavailable(f"Gemini {e.__class__.__name__}")
    try:
        cand = out["candidates"][0]
        text = "".join(p.get("text", "") for p in cand["content"]["parts"] if not p.get("thought"))
    except Exception:
        STATS["fallbacks"] += 1
        raise GeminiUnavailable("Gemini gave no answer")
    u = out.get("usageMetadata", {})
    STATS["in"] += u.get("promptTokenCount", 0)
    STATS["out"] += u.get("candidatesTokenCount", 0) + u.get("thoughtsTokenCount", 0)
    STATS["ok"] += 1
    return text


def summary() -> str:
    if not STATS["calls"]:
        return ""
    cost = (STATS["in"] * PRICE[0] + STATS["out"] * PRICE[1]) / 1e6
    return (f"[Gemini {MODEL}] {STATS['ok']} pages read by Gemini, {STATS['fallbacks']} handed to Haiku | "
            f"{STATS['in']:,} in / {STATS['out']:,} out tokens = ${cost:.3f} at list price "
            f"($0 if within Google's free tier)")


atexit.register(lambda: print(summary()) if STATS["calls"] else None)
