"""Planner: reads the playbook and the public campaign object, returns the contract's plan."""
from __future__ import annotations

import json
from typing import Callable, Optional

from .models import CallResult, call_model
from .schemas import validate_plan

SYSTEM = """You are the Planner of an ad-campaign agent team. Follow the PLAYBOOK exactly; it is the team's policy.
Decide how to split the period budget across the campaign's line items before the first checkpoint.
Reply with ONE JSON object and nothing else:
{"allocations": {"<line_item>": <share>, ...},        // non-negative shares, normalized to sum 1; omitted items get 0
 "holdouts": {"<line_item>": <share 0.05-0.5>},        // optional: withhold ads from that audience share to measure incrementality
 "hypotheses": {"<line_item>": "<one sentence>"}}      // optional: what you expect and why
Use only the line items listed in campaign.capabilities.line_items. Only request holdouts if campaign.capabilities.holdouts is true."""

CREATIVES_HINT = """Also include "creatives": {"<line_item>": [{"id": "<unique id>", "headline": "...", "body": "...", "cta": "..."}]}
with 2 distinct options per line item. They need human approval before any live use."""


def default_plan(campaign: dict) -> dict:
    """Safe fallback: equal split across supported line items, no holdouts."""
    return {"allocations": {li: 1.0 for li in campaign["capabilities"]["line_items"]}, "holdouts": {}, "hypotheses": {}}


def plan(playbook: str, campaign: dict, *, client=None, recorder: Optional[Callable] = None,
         ctx: Optional[dict] = None, want_creatives: bool = False) -> CallResult:
    """Returns a CallResult whose .parsed is a validated plan (the equal-split fallback if the model fails)."""
    system = SYSTEM + ("\n" + CREATIVES_HINT if want_creatives else "")
    public = {k: campaign[k] for k in ("product", "total_budget", "period_budget", "n_checkpoints", "avg_order_value",
                                       "max_total_spend", "max_period_spend", "capabilities")}
    messages = [{"role": "system", "content": system},
                {"role": "user", "content": f"PLAYBOOK:\n{playbook}\n\nCAMPAIGN:\n{json.dumps(public)}"}]
    return call_model("planner", messages, client=client, recorder=recorder,
                      schema_check=lambda raw: validate_plan(raw, campaign),
                      fallback=default_plan(campaign), ctx=ctx)
