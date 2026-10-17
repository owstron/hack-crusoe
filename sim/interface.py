"""Campaign backend contract, schema_version 2. Implemented by sim.Simulator and, later, a live Meta backend.

Usage:
    from sim import generate_campaigns, Simulator, score

    for campaign in generate_campaigns(6, "train", seed=0):
        sim = Simulator(campaign, seed=1)
        sim.apply_plan(plan)                # once, before checkpoint 1 (planner output, see PLAN below)
        while not sim.done:
            obs = sim.observe()             # visible metrics only (see OBSERVATION below)
            for action in actions:          # channel agent tool calls (see ACTIONS below)
                result = sim.act(action)    # {"ok": bool, "error": str | None}
            sim.step()                      # run one checkpoint (a backend's advance(); see TIME below)
        s = score(sim)                      # 0..1, from hidden truth. Never shown to agents mid-run.
        debrief = sim.debrief()             # hidden truth + score. TRAIN SPLIT ONLY (improver post-mortems).

Rules:
- 4 checkpoints. Period budget is fixed = total_budget / 4. Agents only split it.
- Same (campaign, seed) + same actions -> identical observations. Score uses expected
  (noise-free) true incremental conversions, so it depends only on the actions taken.
- Splits: "train" (improver studies), "validation" (gate decides), "test" (final eval, once).
  Test campaigns contain trap variations not seen in train/validation.
- Scores from schema v1 runs (weeks, no capabilities) are not comparable with v2; keep them as archive only.

schema_version 2 notes: the simulator reports every period as status "final" (no provisional data), so
baseline policies score exactly as under v1. Provisional data and unavailable fields exist in the contract
for live backends; agents and playbooks must tolerate them.
"""
from __future__ import annotations

from typing import Optional

SCHEMA_VERSION = 2

# The simulator's own line-item catalog. Backends declare what they actually support through
# campaign["capabilities"]; validators below read that, not these constants.
LINE_ITEMS = ["meta_prospecting", "meta_retargeting", "google_nonbrand", "google_brand"]
N_CHECKPOINTS = 4
SPLITS = ("train", "validation", "test")

HOLDOUT_MIN, HOLDOUT_MAX = 0.05, 0.5

ACTION_TYPES = ("shift_budget", "pause", "rotate_creative", "set_holdout")
STATUSES = ("awaiting_data", "ready", "done")
CREATIVE_STATUSES = ("pending", "approved", "live", "retired")

# CAMPAIGN (obs["campaign"]):
# {
#   "schema_version": 2,
#   "id", "product", "avg_order_value",
#   "total_budget": float,          # lifetime budget, dollars
#   "period_budget": float,         # budget per checkpoint period, dollars (simulator: total_budget / n_checkpoints)
#   "n_checkpoints": int,
#   "max_total_spend": float,       # safety limit on lifetime spend; enforced by act() and apply_plan()
#   "max_period_spend": float,      # safety limit on spend within one checkpoint period; same enforcement
#   "line_items": [li, ...],        # same keys as capabilities["line_items"]
#   "capabilities": {
#       "line_items": {li: {"channel": "meta" | "google"}},   # every line item declares its channel
#       "holdouts": bool,           # False: set_holdout and plan holdouts are rejected
#       "attribution": {li: "7d_click_1d_view" | "data_driven" | ...},   # each platform's default
#   },
# }

# PLAN (planner output, passed to apply_plan):
# {
#   "allocations": {"meta_prospecting": 0.4, "meta_retargeting": 0.2, ...},  # shares of period budget, normalized to sum 1
#   "holdouts":    {"meta_retargeting": 0.2},                               # optional, share in [HOLDOUT_MIN, HOLDOUT_MAX]
#   "hypotheses":  {"meta_retargeting": "cheap CPA may be over-credited"},   # optional, stored in trace only
#   "creatives":   {li: [{"id", "headline", "body", "cta", "image_ref"?}]},  # optional; see CREATIVES
# }
# Missing line items get 0. Line items the campaign does not support are rejected (the whole plan fails).
# Holdouts are rejected when capabilities["holdouts"] is False. A holdout withholds ads from that share of the
# audience: it costs that share of the line item's true incremental conversions, and in return the observation
# reports a measured incremental estimate with a 95% CI.

# CREATIVES: the plan may carry creative text per line item. The observation's registry lists them:
#   obs["creatives"] = [{"id", "line_item", "age_checkpoints": int, "status": "pending"|"approved"|"live"|"retired"}]
# rotate_creative makes an already approved creative live for that line item (the old one retires, age resets).
# If none is approved it returns {"ok": False, "error": ...} and changes nothing. It never generates or submits
# new content. Simulator only: when the plan carries no creatives, it provisions an implicit unlimited pool of
# approved creatives per line item, so rotate_creative always succeeds (v1 behavior).
# Deferred: set_creative action and per-creative fatigue modeling.

# ACTIONS (act), applied to the NEXT period:
# {"type": "shift_budget", "from": li, "to": li, "amount": dollars}   # amount capped at from's period budget
# {"type": "pause", "line_item": li}            # budget -> 0, freed money spread over active items pro rata
# {"type": "rotate_creative", "line_item": li}  # attach an approved creative; resets creative fatigue clock to 0
# {"type": "set_holdout", "line_item": li, "share": 0.2}   # share 0 ends the holdout
# Every action is checked against capabilities (unknown/unsupported line item, holdouts unsupported) and the
# spend caps; failures return {"ok": False, "error": str} and change nothing.

# TIME: advance() / step() runs one checkpoint and returns {"due_at": str | None}: the ISO timestamp when the
# next checkpoint's data is available. The simulator returns None (instant). A live backend only records the
# timestamp and never blocks. Backends expose `status`: "awaiting_data" (not yet due) | "ready" | "done".
# The simulator is always "ready" until "done".

# OBSERVATION (observe()):
# {
#   "campaign": {...},                  # see CAMPAIGN
#   "checkpoint": int,                  # checkpoints completed so far (0 before the first)
#   "checkpoints_left": int,
#   "status": "awaiting_data" | "ready" | "done",
#   "budgets": {li: dollars},           # budget for the next period
#   "holdouts": {li: share},            # active holdouts
#   "creatives": [...],                 # registry, see CREATIVES
#   "history": [                        # one entry per completed period (re-reads of a period share `checkpoint`)
#     {"checkpoint": 1,
#      "read_no": 1,                    # increments when the same period is re-read (e.g. after the attribution window closes)
#      "period": {"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"},   # simulator: 7 synthetic days
#      "status": "final" | "provisional",           # provisional: recent conversions may still be under-reported; wait
#      "attribution_window_closes_at": str | None,  # ISO date after which this period's attributed numbers are final
#      "delivery_ok": bool,                         # False: delivery problem (e.g. below minimum-data threshold)
#      "line_items": {li: {
#         "spend": float,
#         "impressions": int, "clicks": int,
#         "reach": int | None,                 # Meta only
#         "frequency": float | None,           # impressions / reach; Meta only. Rises as spend saturates the audience
#         "ctr": float | None,                 # percent, as Meta reports it (2.39 = 2.39%). Falls with frequency and creative age
#         "cpm": float | None, "cpc": float | None,
#         "search_impression_share": float | None,              # Google only, fraction 0-1: share of eligible searches shown.
#         "search_budget_lost_impression_share": float | None,  #   Near the ceiling with ~0 lost to budget = saturated
#         "search_rank_lost_impression_share": float | None,    #   (more budget buys little). None for Meta.
#         "creative_age_checkpoints": int,     # checkpoints since last rotation
#         "unavailable": [field, ...],         # fields the platform does not report for this line item.
#                                              #   Distinct from zero or from a transient None (e.g. no spend).
#         "reported": {                        # platform-attributed numbers, noisy, credited per `attribution`
#             "attribution": str,              # each platform's default: Meta "7d_click_1d_view"; Google "data_driven"
#             "attributed_conversions": int,   # platform-reported purchases
#             "cpa": float | None,             # spend / attributed_conversions (cost per purchase)
#             "purchase_value": float, "roas": float | None,   # attributed revenue and revenue / spend
#         },
#         "measured": {                        # experiment-based estimates (the only incrementality evidence)
#             "holdout": None | {              # cumulative since this holdout began
#                 "share": float, "checkpoints": int,
#                 "incremental_conversions_est": float,
#                 "incremental_share_est": float,  # est. incremental / attributed (1.0 = last-click is right)
#                 "ci95": [lo, hi],                # on incremental_share_est
#                 "conclusive": bool,              # CI excludes 0
#             },
#         },
#     }}},
#   ],
# }
# Units are unified across platforms (dollars; CTR in percent like Meta). A live Google Ads backend must
# convert: metrics.cost_micros / average_cpc / cost_per_conversion are micros (/ 1e6), metrics.ctr is a
# fraction (x 100), metrics.conversions can be fractional. Impression-share fields are fractions in both.
# A live Meta backend marks holdout/measured data and Google-only fields unavailable, and may mark an
# entry provisional until its attribution window closes.


def check_plan(plan: dict, campaign: dict) -> Optional[str]:
    """Error string if the plan is not valid for this campaign's capabilities, else None."""
    caps = campaign["capabilities"]
    supported = caps["line_items"]
    alloc = plan.get("allocations") or {}
    bad = [li for li in alloc if li not in supported]
    if bad:
        return f"unsupported line item(s) {bad}; supported: {sorted(supported)}"
    if sum(max(0.0, float(v)) for v in alloc.values()) <= 0:
        return "allocations must contain positive shares for supported line items"
    holdouts = {li: s for li, s in (plan.get("holdouts") or {}).items() if s}
    if holdouts:
        if not caps["holdouts"]:
            return "holdouts are not supported by this campaign"
        for li, share in holdouts.items():
            if li not in supported:
                return f"unsupported line item {li!r} in holdouts"
            if not HOLDOUT_MIN <= float(share) <= HOLDOUT_MAX:
                return f"holdout share must be 0 or in [{HOLDOUT_MIN}, {HOLDOUT_MAX}]"
    for li, items in (plan.get("creatives") or {}).items():
        if li not in supported:
            return f"unsupported line item {li!r} in creatives"
        if not isinstance(items, list) or any(not isinstance(c, dict) or not c.get("id") for c in items):
            return f"creatives for {li!r} must be a list of objects with an id"
    return None


def check_action(action: dict, campaign: dict) -> Optional[str]:
    """Error string if the action is not valid for this campaign's capabilities, else None."""
    typ = action.get("type")
    if typ not in ACTION_TYPES:
        return f"unknown action type {typ!r}; expected one of {ACTION_TYPES}"
    caps = campaign["capabilities"]
    supported = caps["line_items"]
    if typ == "shift_budget":
        src, dst = action.get("from"), action.get("to")
        if src not in supported or dst not in supported or src == dst:
            return "from/to must be two different supported line items"
        return None
    li = action.get("line_item")
    if li not in supported:
        return f"unsupported line item {li!r}"
    if typ == "set_holdout":
        if not caps["holdouts"]:
            return "holdouts are not supported by this campaign"
        share = float(action["share"])
        if share != 0 and not HOLDOUT_MIN <= share <= HOLDOUT_MAX:
            return f"holdout share must be 0 or in [{HOLDOUT_MIN}, {HOLDOUT_MAX}]"
    return None


def check_spend(budgets: dict, spent_to_date: float, campaign: dict) -> Optional[str]:
    """Error string if next-period budgets would break the campaign's spend caps, else None.
    Every backend calls this before any write that sets budgets (apply_plan, act)."""
    nxt = sum(budgets.values())
    if nxt > campaign["max_period_spend"] + 1e-6:
        return f"period budget ${nxt:,.2f} exceeds max_period_spend ${campaign['max_period_spend']:,.2f}"
    if spent_to_date + nxt > campaign["max_total_spend"] + 1e-6:
        return (f"spend to date ${spent_to_date:,.2f} + period budget ${nxt:,.2f} exceeds "
                f"max_total_spend ${campaign['max_total_spend']:,.2f}")
    return None
