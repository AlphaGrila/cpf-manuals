"""
CPF Agent System — Supabase Client
Updated to match actual agent_runs schema.
"""

import json
import urllib.request
import urllib.parse
import uuid as _uuid
from datetime import datetime, timezone
import urllib.error
from typing import Any, Optional
from config import SUPABASE_URL, SUPABASE_SERVICE_KEY


class SupabaseClient:
    def __init__(self):
        if not SUPABASE_SERVICE_KEY:
            raise ValueError("SUPABASE_SERVICE_KEY environment variable not set")
        self.url = SUPABASE_URL.rstrip("/")
        self._run_uuids = {}  # maps text run label -> agent_runs.id (uuid)
        self.headers = {
            "apikey": SUPABASE_SERVICE_KEY,
            "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        }

    def _request(self, method: str, path: str, body: Optional[dict] = None,
                 params: Optional[dict] = None) -> Any:
        url = f"{self.url}{path}"
        if params:
            query = urllib.parse.urlencode(params, safe=",.()*:", quote_via=urllib.parse.quote)
            url = f"{url}?{query}"
        data = json.dumps(body).encode() if body else None
        req = urllib.request.Request(url, data=data, headers=self.headers, method=method)
        try:
            with urllib.request.urlopen(req) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            error_body = e.read().decode()
            raise RuntimeError(f"Supabase {method} {path} failed {e.code}: {error_body}")

    def select(self, table: str, columns: str = "*", filters: Optional[dict] = None,
               limit: int = 1000) -> list:
        # Supabase returns at most 1,000 rows per request whatever limit is asked for, so a
        # bigger limit is fetched page by page (18 Nov 2026: tables had passed 1,000 rows and
        # duplicate checks were silently missing the rest).
        page_size = 1000
        out, offset = [], int((filters or {}).get("offset", 0) or 0)
        while True:
            params = {"select": columns, "limit": str(min(page_size, limit - len(out)))}
            if filters:
                for k, v in filters.items():
                    params[k] = v
            params["offset"] = str(offset)
            page = self._request("GET", f"/rest/v1/{table}", params=params) or []
            out.extend(page)
            if len(page) < page_size or len(out) >= limit:
                return out
            offset += len(page)

    def run_uuid(self, run_label: str):
        """Return the agent_runs.id (uuid) for a text run label like run_20260918_235320.
        Staging tables link to agent_runs by uuid, not by the text label."""
        if not run_label:
            return None
        try:
            _uuid.UUID(str(run_label))
            return str(run_label)  # already a uuid
        except ValueError:
            pass
        if run_label in self._run_uuids:
            return self._run_uuids[run_label]
        try:
            found = self.select("agent_runs", columns="id",
                                filters={"run_id": f"eq.{run_label}"}, limit=1)
            if found:
                self._run_uuids[run_label] = found[0]["id"]
                return found[0]["id"]
        except Exception:
            pass
        return None  # no run row -> store NULL (column allows it)

    def insert(self, table: str, rows: list | dict) -> list:
        if isinstance(rows, dict):
            rows = [rows]
        if table in ("staging_suppliers", "staging_parts", "staging_supply_tool_suppliers"):
            fixed = []
            for row in rows:
                row = dict(row)
                if "agent_run_id" in row:
                    row["agent_run_id"] = self.run_uuid(row["agent_run_id"])
                fixed.append(row)
            rows = fixed
        return self._request("POST", f"/rest/v1/{table}", body=rows) or []

    def update(self, table: str, data: dict, filters: dict) -> list:
        params = {}
        for k, v in filters.items():
            params[k] = v
        return self._request("PATCH", f"/rest/v1/{table}", body=data, params=params) or []

    def rpc(self, function_name: str, params: dict) -> Any:
        return self._request("POST", f"/rest/v1/rpc/{function_name}", body=params)

    def insert_agent_run(self, agent_name: str, run_id: str, config: dict) -> str:
        """Create an agent_run record matching actual schema."""
        try:
            created = self.insert("agent_runs", {
                "run_id": run_id,
                "agent_name": agent_name,
                "status": "running",
                "config": config,
                "records_processed": 0,
                "records_staged": 0,
            })
            if created and created[0].get("id"):
                self._run_uuids[run_id] = created[0]["id"]
        except Exception as e:
            print(f"  [Warning] Could not log agent run: {e}")
        return run_id

    def complete_agent_run(self, run_id: str, records_processed: int,
                           records_passed: int, records_flagged: int,
                           estimated_cost_usd: float, notes: str = "") -> None:
        """Update agent_run on completion matching actual schema.
        If Anthropic reported the account is out of credits during this run, the run is
        marked FAILED (red in the Agent Queue) and the process exits non-zero so GitHub
        shows a failed run and emails the owner."""
        import anthropic_client
        out_of_credits = anthropic_client.CREDITS_EXHAUSTED
        if out_of_credits:
            notes = f"{anthropic_client.CREDITS_MESSAGE}. {notes}"
        try:
            self.update("agent_runs", {
                "status": "failed" if out_of_credits else "done",
                "error": anthropic_client.CREDITS_MESSAGE if out_of_credits else None,
                "records_processed": records_processed,
                "records_staged": records_processed,
                "estimated_cost_usd": estimated_cost_usd,
                "notes": notes,
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "output_summary": notes,
            }, {"run_id": f"eq.{run_id}"})
        except Exception as e:
            print(f"  [Warning] Could not update agent run: {e}")
        if out_of_credits:
            raise SystemExit(3)


db = SupabaseClient()
