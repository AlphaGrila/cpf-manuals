"""
CPF Agent System — Anthropic API Client
Handles both real-time and batch API calls with prompt caching.
"""

import atexit
import json
import time
import urllib.request
import urllib.error
from typing import Any, Optional
from config import ANTHROPIC_API_KEY, USE_BATCH_API, CACHE_SYSTEM_PROMPTS, PRICES, WEB_SEARCH_PRICE_USD, SEARCH_TOOL


def clean_json_text(text: str) -> str:
    """Strip markdown code fences / stray text so the reply is plain JSON.
    Claude often wraps JSON in ```json ... ``` — the agents expect bare JSON.
    Also copes with prose before the JSON that contains brackets, such as citation marks
    like [1], by taking the first piece that really parses as an object (or a list of objects)."""
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t[3:]
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
        t = t.strip()
    if t and t[0] not in "{[":
        starts = [i for i in (t.find("{"), t.find("[")) if i != -1]
        if starts:
            t = t[min(starts):]
            end = max(t.rfind("}"), t.rfind("]"))
            if end != -1:
                t = t[:end + 1]
    return _first_real_json(text or "", t)


def _first_real_json(original: str, fallback: str) -> str:
    """The first {...} or [{...}] in `original` that parses; otherwise `fallback` unchanged."""
    try:
        json.loads(fallback)
        return fallback
    except Exception:
        pass
    dec = json.JSONDecoder()
    for i, ch in enumerate(original):
        if ch not in "{[":
            continue
        try:
            obj, end = dec.raw_decode(original, i)
        except ValueError:
            continue
        if isinstance(obj, dict) or (isinstance(obj, list) and obj and all(isinstance(x, dict) for x in obj)):
            return original[i:end]
    return fallback


# Set to True the moment Anthropic says the account is out of credits. The agents
# check this at the end of every run so a silent "0 records" night can't happen.
CREDITS_EXHAUSTED = False
CREDITS_MESSAGE = "ANTHROPIC CREDITS EXHAUSTED - top up at console.anthropic.com (Settings > Billing)"


class AnthropicClient:
    BASE_URL = "https://api.anthropic.com/v1"

    def __init__(self):
        if not ANTHROPIC_API_KEY:
            raise ValueError("ANTHROPIC_API_KEY environment variable not set")
        self.headers = {
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
            # Enable prompt caching and batch API betas
            "anthropic-beta": "prompt-caching-2024-07-31,message-batches-2024-09-24",
        }
        self.spent_usd = 0.0      # running estimate of API spend this run
        self.searches = 0         # web searches performed this run
        self.last_usage = {}      # token counts of the most recent call (for cost checks)
        # Prompt-caching totals for this run, printed at the end so cache health is visible in every log
        self.tok = {"calls": 0, "input": 0, "cache_write": 0, "cache_read": 0}
        atexit.register(self.print_cache_summary)

    def print_cache_summary(self) -> None:
        t = self.tok
        if not t["calls"]:
            return
        total = t["input"] + t["cache_write"] + t["cache_read"]
        hit = (100 * t["cache_read"] / total) if total else 0
        print(f"\n[Prompt caching] {t['calls']} calls | input tokens: {total:,} total — "
              f"{t['cache_read']:,} read from cache ({hit:.0f}%), {t['cache_write']:,} written to cache, "
              f"{t['input']:,} uncached | est. spend ${self.spent_usd:.2f}")

    def _track(self, model: str, usage: dict) -> None:
        """Add this call's estimated cost to the running total."""
        self.last_usage = dict(usage or {})
        self.tok["calls"] += 1
        self.tok["input"] += usage.get("input_tokens", 0)
        self.tok["cache_write"] += usage.get("cache_creation_input_tokens", 0)
        self.tok["cache_read"] += usage.get("cache_read_input_tokens", 0)
        tier = "haiku" if "haiku" in model else "sonnet"
        p_in, p_out, p_cw, p_cr = PRICES[tier]
        write_total = usage.get("cache_creation_input_tokens", 0)
        write_1h = (usage.get("cache_creation") or {}).get("ephemeral_1h_input_tokens", 0)
        cost = (usage.get("input_tokens", 0) * p_in
                + usage.get("output_tokens", 0) * p_out
                + (write_total - write_1h) * p_cw
                + write_1h * p_in * 2          # one-hour cache writes cost twice the normal input rate
                + usage.get("cache_read_input_tokens", 0) * p_cr) / 1_000_000
        n = (usage.get("server_tool_use") or {}).get("web_search_requests", 0)
        self.searches += n
        cost += n * WEB_SEARCH_PRICE_USD
        self.spent_usd += cost

    def _post(self, path: str, body: dict, extra_headers: Optional[dict] = None) -> dict:
        url = f"{self.BASE_URL}{path}"
        data = json.dumps(body).encode()
        last_err = None
        headers = {**self.headers, **(extra_headers or {})}
        for attempt in range(4):
            req = urllib.request.Request(url, data=data, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=300) as resp:
                    return json.loads(resp.read())
            except urllib.error.HTTPError as e:
                detail = e.read().decode()
                last_err = RuntimeError(f"Anthropic API error {e.code}: {detail}")
                # Both mean "no more AI until Steve acts": an empty credit balance, or the
                # monthly spend limit set in the Anthropic console (added 18 Nov 2026 after
                # the limit silently stopped every agent from 14 Nov).
                low = detail.lower()
                if "credit balance is too low" in low or "reached your specified api usage limits" in low:
                    global CREDITS_EXHAUSTED
                    CREDITS_EXHAUSTED = True
                    print(f"  !!! {CREDITS_MESSAGE}")
                    raise last_err
                if e.code in (429, 500, 502, 503, 529) and attempt < 3:
                    time.sleep(20 * (attempt + 1))   # busy or rate limited: wait and retry
                    continue
                raise last_err
        raise last_err

    def _get(self, path: str) -> dict:
        url = f"{self.BASE_URL}{path}"
        req = urllib.request.Request(url, headers=self.headers, method="GET")
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())

    def _build_system(self, system_prompt: str, context: Optional[str] = None,
                      context_ttl: str = "1h") -> list | str:
        """Wrap system prompt with cache_control if caching is enabled. `context` is a long
        block that stays the same from call to call (such as the do-not-suggest list); it is
        cached separately, for an hour by default, so repeat calls pay a tenth of the price."""
        if CACHE_SYSTEM_PROMPTS:
            # A longer-lived cache block may not come after a shorter-lived one, so when there is
            # a long-lived context block the instructions in front of it get the same lifetime.
            first = {"type": "ephemeral", "ttl": context_ttl} if context else {"type": "ephemeral"}
            blocks = [{"type": "text", "text": system_prompt, "cache_control": first}]
            if context:
                blocks.append({"type": "text", "text": context,
                               "cache_control": {"type": "ephemeral", "ttl": context_ttl}})
            return blocks
        return system_prompt + ("\n\n" + context if context else "")

    # ── Single message (for testing / manager calls) ──────────────────────
    def message(self, model: str, system: str, user: str,
                max_tokens: int = 1024, context: Optional[str] = None) -> str:
        """context: a long block that is identical from call to call (reference lists etc.).
        It goes in the cached part of the prompt, so repeat calls pay a tenth of the price."""
        body = {
            "model": model,
            "max_tokens": max_tokens,
            "system": self._build_system(system, context),
            "messages": [{"role": "user", "content": user}],
        }
        resp = self._post("/messages", body)
        self._track(model, resp.get("usage", {}))
        return clean_json_text(resp["content"][0]["text"])

    # ── Single message WITH live web search ───────────────────────────────
    def message_with_search(self, model: str, system: str, user: str,
                            max_tokens: int = 2048, max_searches: int = 3,
                            allowed_domains: Optional[list] = None,
                            context: Optional[str] = None,
                            tool_type: Optional[str] = None) -> str:
        """Ask Claude a question it can answer by searching the web.
        allowed_domains restricts searching to those sites (e.g. the supplier's own).
        context is a long, unchanging block placed in the cached part of the prompt.
        tool_type picks the search tool version (the newer ones filter results before Claude
        reads them, which uses fewer tokens); it defaults to the SEARCH_TOOL setting."""
        tool_type = tool_type or SEARCH_TOOL
        tool = {"type": tool_type, "name": "web_search", "max_uses": max_searches}
        if allowed_domains:
            tool["allowed_domains"] = allowed_domains
        if tool_type != "web_search_20250305" and "haiku" in model:
            tool["allowed_callers"] = ["direct"]   # older models cannot call tools from code
        headers = None
        if tool_type == "web_search_20260209":
            headers = {"anthropic-beta": self.headers["anthropic-beta"] + ",code-execution-web-tools-2026-02-09"}
        messages = [{"role": "user", "content": user}]
        resp, container = {}, None
        for _ in range(4):
            body = {
                "model": model,
                "max_tokens": max_tokens,
                "system": self._build_system(system, context),
                "messages": messages,
                "tools": [tool],
            }
            if container:
                body["container"] = container
            resp = self._post("/messages", body, extra_headers=headers)
            self._track(model, resp.get("usage", {}))
            if isinstance(resp.get("container"), dict) and resp["container"].get("id"):
                container = resp["container"]["id"]
            if resp.get("stop_reason") == "pause_turn":
                messages.append({"role": "assistant", "content": resp["content"]})
                continue
            break
        # keep only the text that comes after the last search block
        texts = []
        for block in resp.get("content", []):
            if block.get("type") == "text":
                texts.append(block.get("text", ""))
            else:
                texts = []
        return clean_json_text("".join(texts))

    # ── Batch API (50% cheaper, async) ────────────────────────────────────
    def submit_batch(self, model: str, system: str, requests: list[dict],
                     max_tokens: int = 1024) -> str:
        """
        Submit a batch of requests. Returns batch_id.
        Each request: {"custom_id": str, "user_prompt": str}
        """
        batch_requests = []
        for req in requests:
            batch_requests.append({
                "custom_id": req["custom_id"],
                "params": {
                    "model": model,
                    "max_tokens": max_tokens,
                    "system": self._build_system(system),
                    "messages": [{"role": "user", "content": req["user_prompt"]}],
                }
            })

        body = {"requests": batch_requests}
        resp = self._post("/messages/batches", body)
        batch_id = resp["id"]
        print(f"  Batch submitted: {batch_id} ({len(requests)} requests)")
        return batch_id

    def wait_for_batch(self, batch_id: str, poll_interval: int = 30,
                       timeout: int = 3600) -> list[dict]:
        """
        Poll until batch completes. Returns list of results.
        Each result: {"custom_id": str, "text": str, "error": str|None}
        """
        start = time.time()
        print(f"  Waiting for batch {batch_id}...")

        while True:
            if time.time() - start > timeout:
                raise TimeoutError(f"Batch {batch_id} timed out after {timeout}s")

            status_resp = self._get(f"/messages/batches/{batch_id}")
            processing_status = status_resp.get("processing_status")

            counts = status_resp.get("request_counts", {})
            print(f"  Status: {processing_status} | "
                  f"succeeded={counts.get('succeeded',0)} "
                  f"errored={counts.get('errored',0)} "
                  f"processing={counts.get('processing',0)}")

            if processing_status == "ended":
                break

            time.sleep(poll_interval)

        # Fetch results
        results_url = status_resp.get("results_url")
        if not results_url:
            return []

        req = urllib.request.Request(results_url, headers=self.headers)
        with urllib.request.urlopen(req) as resp:
            raw = resp.read().decode()

        results = []
        for line in raw.strip().split("\n"):
            if not line.strip():
                continue
            item = json.loads(line)
            custom_id = item.get("custom_id", "")
            result = item.get("result", {})

            if result.get("type") == "succeeded":
                text = clean_json_text(result["message"]["content"][0]["text"])
                results.append({"custom_id": custom_id, "text": text, "error": None})
            else:
                error = result.get("error", {}).get("message", "unknown error")
                results.append({"custom_id": custom_id, "text": "", "error": error})

        return results


claude = AnthropicClient()
