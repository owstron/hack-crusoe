"""Deterministic validation of model output. The code, not a model, decides what reaches a backend.

validate_plan / parse_actions use the exact line items, action types and capabilities from sim/interface.py
(campaign["capabilities"]), so a Meta-only campaign rejects Google items and set_holdout through the same schema.
"""
from __future__ import annotations

from typing import Iterable, List, Optional, Tuple

from sim.interface import ACTION_TYPES, check_action, check_plan

VERDICTS = ("pass", "fail", "inconclusive")


def validate_plan(raw, campaign: dict) -> dict:
    """Return a normalized plan or raise ValueError. Usable as call_model's schema_check."""
    if not isinstance(raw, dict):
        raise ValueError("plan must be a JSON object")
    alloc = raw.get("allocations")
    if not isinstance(alloc, dict) or not alloc:
        raise ValueError("plan needs a non-empty 'allocations' object")
    try:
        plan = {"allocations": {str(li): float(v) for li, v in alloc.items()}}
        if any(v < 0 for v in plan["allocations"].values()):
            raise ValueError("allocation shares must be non-negative")
        holdouts = raw.get("holdouts") or {}
        if not isinstance(holdouts, dict):
            raise ValueError("'holdouts' must be an object")
        plan["holdouts"] = {str(li): float(v) for li, v in holdouts.items() if v}
    except (TypeError, ValueError) as e:
        raise ValueError(f"malformed plan numbers: {e}") from e
    hyp = raw.get("hypotheses") or {}
    if not isinstance(hyp, dict):
        raise ValueError("'hypotheses' must be an object")
    plan["hypotheses"] = {str(k): str(v) for k, v in hyp.items()}
    creatives = raw.get("creatives")
    if creatives:
        plan["creatives"] = creatives
    err = check_plan(plan, campaign)
    if err:
        raise ValueError(err)
    return plan


def _coerce_action(a: dict) -> dict:
    """Keep only the fields the contract defines, with numeric fields coerced."""
    typ = a.get("type")
    if typ == "shift_budget":
        return {"type": typ, "from": a.get("from"), "to": a.get("to"), "amount": float(a["amount"])}
    if typ == "set_holdout":
        return {"type": typ, "line_item": a.get("line_item"), "share": float(a["share"])}
    return {"type": typ, "line_item": a.get("line_item")}


def check_actions_payload(raw) -> list:
    """Shape check for the model's reply: a list of actions, or {"actions": [...]}. Raises ValueError otherwise."""
    if isinstance(raw, dict):
        raw = raw.get("actions", [])
    if not isinstance(raw, list):
        raise ValueError("expected a JSON list of actions or an object with an 'actions' list")
    return raw


def parse_actions(raw, campaign: dict, line_items: Optional[Iterable[str]] = None) -> Tuple[List[dict], List[dict]]:
    """Split a model's reply into (valid actions, rejected [{"action", "error"}]). Only valid ones may reach a backend.

    `line_items` restricts a channel agent to its own items: the acted-on `line_item`, or a shift's `from`, must be
    one of them (a shift's `to` may be any supported line item, since cross-channel moves are allowed)."""
    own = set(line_items) if line_items is not None else None
    valid: List[dict] = []
    rejected: List[dict] = []
    for a in check_actions_payload(raw):
        if not isinstance(a, dict) or a.get("type") not in ACTION_TYPES:
            rejected.append({"action": a, "error": f"not an action object with type in {ACTION_TYPES}"})
            continue
        try:
            a = _coerce_action(a)
        except (KeyError, TypeError, ValueError) as e:
            rejected.append({"action": a, "error": f"malformed action: {e}"})
            continue
        err = check_action(a, campaign)
        if not err and own is not None:
            subject = a["from"] if a["type"] == "shift_budget" else a["line_item"]
            if subject not in own:
                err = f"{subject!r} is not one of this agent's line items {sorted(own)}"
        if err:
            rejected.append({"action": a, "error": err})
        else:
            valid.append(a)
    return valid, rejected


def validate_done_definition(raw) -> dict:
    """Verifier's definition of done, written before the first checkpoint: {"criteria": [str, ...]}."""
    if not isinstance(raw, dict) or not isinstance(raw.get("criteria"), list) or not raw["criteria"]:
        raise ValueError("definition of done needs a non-empty 'criteria' list")
    return {"criteria": [str(c) for c in raw["criteria"]],
            "notes": str(raw.get("notes", ""))}


def validate_verdict(raw) -> dict:
    """Verifier's final interpretation of the observed evidence."""
    if not isinstance(raw, dict) or raw.get("verdict") not in VERDICTS:
        raise ValueError(f"'verdict' must be one of {VERDICTS}")
    ev = raw.get("evidence", [])
    if not isinstance(ev, list):
        raise ValueError("'evidence' must be a list")
    return {"verdict": raw["verdict"], "summary": str(raw.get("summary", "")), "evidence": [str(e) for e in ev]}
