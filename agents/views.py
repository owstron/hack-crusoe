"""What agents see: a compact, public view of the observation. Never includes hidden truth (the backend never
exposes it); the 'unavailable' and 'provisional' markers are kept so agents can tolerate missing data."""
from __future__ import annotations

from typing import Iterable, Optional


def _row(m: dict) -> dict:
    rep, mea = m["reported"], m["measured"]
    out = {
        "spend": m["spend"], "attributed_conversions": rep["attributed_conversions"], "cpa": rep["cpa"],
        "roas": rep["roas"], "ctr_pct": m.get("ctr"), "frequency": m.get("frequency"),
        "creative_age_checkpoints": m["creative_age_checkpoints"],
    }
    if m.get("search_impression_share") is not None:
        out["impression_share"] = m["search_impression_share"]
        out["budget_lost_impression_share"] = m["search_budget_lost_impression_share"]
    if m.get("unavailable"):
        out["unavailable"] = m["unavailable"]
    if mea.get("holdout"):
        out["holdout"] = {k: mea["holdout"][k] for k in ("share", "checkpoints", "incremental_share_est", "ci95", "conclusive")}
    return out


def compact_observation(obs: dict, line_items: Optional[Iterable[str]] = None) -> dict:
    """Observation with only decision-relevant fields. `line_items` limits per-item detail (budgets stay complete)."""
    keep = set(line_items) if line_items is not None else set(obs["campaign"]["line_items"])
    camp = obs["campaign"]
    return {
        "campaign": {k: camp[k] for k in ("product", "total_budget", "period_budget", "n_checkpoints", "avg_order_value",
                                          "max_total_spend", "max_period_spend", "capabilities")},
        "checkpoint": obs["checkpoint"], "checkpoints_left": obs["checkpoints_left"], "status": obs["status"],
        "budgets": obs["budgets"], "holdouts": obs["holdouts"],
        "creatives": [c for c in obs.get("creatives", []) if c["line_item"] in keep and c["status"] != "retired"],
        "history": [{
            "checkpoint": h["checkpoint"], "period": h["period"], "status": h["status"], "delivery_ok": h["delivery_ok"],
            "line_items": {li: _row(m) for li, m in h["line_items"].items() if li in keep},
        } for h in obs["history"]],
    }


def cpa_table(obs: dict) -> dict:
    """Latest reported CPA for every line item (read-only context for channel agents)."""
    if not obs["history"]:
        return {}
    return {li: m["reported"]["cpa"] for li, m in obs["history"][-1]["line_items"].items()}
