"""Meta Marketing (Graph) API helpers: the HTTP calls the old ad_swarm_full.py made inline, behind one class.

`MetaAPI(dry_run=True)` never touches the network: writes return deterministic mock ids and are only recorded in
`api.writes`; reads of Insights come from a caller-supplied synthetic generator. Everything else (response shapes,
ids, state) is the same as a live run.
"""
from __future__ import annotations

import json
import os
import time
from typing import Callable, List, Optional

GRAPH_VERSION = "v21.0"


class MetaError(RuntimeError):
    pass


class MetaAPI:
    def __init__(self, token: Optional[str] = None, account_id: Optional[str] = None, page_id: Optional[str] = None,
                 link: str = "https://example.com", dry_run: bool = False, version: str = GRAPH_VERSION,
                 session=None, sleep: Callable[[float], None] = time.sleep, poll_interval: float = 3.0,
                 max_polls: int = 15):
        self.token, self.account_id, self.page_id, self.link = token, account_id, page_id, link
        self.dry_run, self.version = dry_run, version
        self.sleep, self.poll_interval, self.max_polls = sleep, poll_interval, max_polls
        self._session = session
        self.writes: List[dict] = []   # every write attempted (dry-run: not sent)
        self._n = 0

    @classmethod
    def from_env(cls, dry_run: bool = False, **kw) -> "MetaAPI":
        env = os.environ.get
        return cls(token=env("META_ACCESS_TOKEN"), account_id=env("META_AD_ACCOUNT_ID"), page_id=env("META_PAGE_ID"),
                   link=env("META_LINK", "https://example.com"), dry_run=dry_run, **kw)

    # ---- raw HTTP ----
    def _http(self):
        if self._session is None:
            import requests
            self._session = requests
        return self._session

    def _url(self, path: str) -> str:
        return f"https://graph.facebook.com/{self.version}/{path}"

    def get(self, path: str, params: Optional[dict] = None) -> dict:
        if not self.token:
            raise MetaError("META_ACCESS_TOKEN not set")
        r = self._http().get(self._url(path), params={**(params or {}), "access_token": self.token}, timeout=30)
        if r.status_code != 200:
            raise MetaError(f"GET {path}: {r.status_code} {r.text[:500]}")
        return r.json()

    def post(self, path: str, data: dict, read_only: bool = False) -> dict:
        """A write (`read_only` marks the POST that only starts a report job). Dry-run records writes and returns a
        mock id; it never reaches the network."""
        if not read_only:
            self.writes.append({"path": path, "data": data})
        if self.dry_run:
            self._n += 1
            return {"id": f"dry_{self._n:04d}", "report_run_id": f"dry_report_{self._n:04d}"}
        if not self.token:
            raise MetaError("META_ACCESS_TOKEN not set")
        r = self._http().post(self._url(path), data={**data, "access_token": self.token}, timeout=30)
        if r.status_code != 200:
            raise MetaError(f"POST {path}: {r.status_code} {r.text[:500]}")
        return r.json()

    # ---- account discovery ----
    def list_ad_accounts(self) -> List[dict]:
        fields = "id,name,account_status,balance,amount_spent,currency,timezone_name,account_id"
        return self.get("me/adaccounts", {"fields": fields, "limit": 50}).get("data", [])

    def account_details(self, account_id: Optional[str] = None) -> dict:
        return self.get(account_id or self.account_id, {
            "fields": "id,name,account_status,balance,amount_spent,spend_cap,currency,timezone_name"})

    # ---- paused object creation ----
    def create_campaign(self, name: str) -> str:
        return self.post(f"{self.account_id}/campaigns", {
            "name": name, "objective": "OUTCOME_TRAFFIC", "status": "PAUSED", "special_ad_categories": "[]",
            "is_adset_budget_sharing_enabled": "false"})["id"]

    def create_adset(self, campaign_id: str, name: str, daily_budget_cents: int, targeting: Optional[dict] = None) -> str:
        return self.post(f"{self.account_id}/adsets", {
            "name": name, "campaign_id": campaign_id, "daily_budget": int(daily_budget_cents),
            "billing_event": "IMPRESSIONS", "optimization_goal": "LINK_CLICKS", "bid_strategy": "LOWEST_COST_WITHOUT_CAP",
            "targeting": json.dumps(targeting or {"geo_locations": {"countries": ["US"]}, "age_min": 18, "age_max": 65}),
            "status": "PAUSED"})["id"]

    def create_creative(self, name: str, headline: str, body: str, cta: str = "LEARN_MORE") -> str:
        return self.post(f"{self.account_id}/adcreatives", {
            "name": name,
            "object_story_spec": json.dumps({"page_id": self.page_id, "link_data": {
                "message": f"{headline}\n\n{body}", "link": self.link, "call_to_action": {"type": cta},
                "name": headline[:40]}})})["id"]

    def create_ad(self, name: str, adset_id: str, creative_id: str, status: str = "PAUSED") -> str:
        return self.post(f"{self.account_id}/ads", {
            "name": name, "adset_id": adset_id, "creative": json.dumps({"creative_id": creative_id}),
            "status": status})["id"]

    # ---- updates ----
    def set_status(self, object_id: str, status: str) -> None:
        self.post(object_id, {"status": status})

    def set_adset_budget(self, adset_id: str, daily_budget_cents: int) -> None:
        self.post(adset_id, {"daily_budget": int(daily_budget_cents)})

    # ---- insights ----
    def insights(self, object_id: str, since: str, until: str, synth: Optional[Callable[[], List[dict]]] = None) -> List[dict]:
        """Ad-set-level rows for [since, until]. Live: async report job, polled. Dry-run: `synth()` (no network)."""
        if self.dry_run:
            if synth is None:
                raise MetaError("dry-run insights need a synthetic generator")
            return synth()
        job = self.post(f"{object_id}/insights", {
            "fields": "adset_id,spend,impressions,clicks,reach,frequency,ctr,cpm,cpc,actions,action_values",
            "level": "adset", "time_range": json.dumps({"since": since, "until": until}),
            "action_attribution_windows": json.dumps(["7d_click", "1d_view"])}, read_only=True)
        report_id = job.get("report_run_id")
        if not report_id:
            raise MetaError(f"no report_run_id in insights response: {job}")
        for _ in range(self.max_polls):
            st = self.get(report_id, {"fields": "async_status,async_percent_completion"})
            if st.get("async_status") == "Job Completed":
                return self.get(f"{report_id}/insights", {"limit": 100}).get("data", [])
            if st.get("async_status") in ("Job Failed", "Job Skipped"):
                raise MetaError(f"insights job {report_id} {st.get('async_status')}")
            self.sleep(self.poll_interval)
        raise MetaError(f"insights job {report_id} did not finish in {self.max_polls} polls")
