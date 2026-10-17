"""Shared backend boundary between agents and a campaign (simulator or live).

Observations and plans use the schema v2 dictionaries from sim/interface.py as-is.

Live-backend caveats. A live backend may mark the following observation data as
`unavailable` (None / missing) or `provisional` (period "status": "provisional"):
  - `measured.holdout` and other measured/incrementality fields: absent when holdouts or
    lift experiments are unsupported (capabilities["holdouts"] is False) or not yet read.
  - Google-only fields (e.g. impression share, data-driven attribution details) on Meta-only
    campaigns, and Meta-only delivery fields on Google rows.
  - `reported` attributed conversions / CPA inside the platform attribution window: recent
    conversions may still be under-reported and later revised (re-reads bump `read_no`).
Agents and playbooks must treat all of these as optional: tolerate None, and wait or
discount when a period is provisional. The simulator never emits them (always "final").
"""
from __future__ import annotations

from typing import Optional, Protocol, TypedDict, runtime_checkable


class ActionResult(TypedDict):
    ok: bool
    error: Optional[str]


def ok() -> ActionResult:
    return {"ok": True, "error": None}


def fail(msg: str) -> ActionResult:
    return {"ok": False, "error": str(msg)}


@runtime_checkable
class CampaignBackend(Protocol):
    def apply_plan(self, plan: dict) -> ActionResult: ...

    def observe(self) -> dict: ...

    def act(self, action: dict) -> ActionResult: ...

    def advance(self) -> dict:
        """Run/await one checkpoint. Returns {"due_at": ISO str | None}; never blocks."""
        ...

    @property
    def done(self) -> bool: ...

    @property
    def status(self) -> str:
        """"awaiting_data" | "ready" | "done"."""
        ...
