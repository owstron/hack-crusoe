"""Channel agents: one per channel, each restricted to its own line items."""
from __future__ import annotations

import json
from typing import Callable, Dict, List, Optional, Tuple

from .models import CallResult, call_model
from .schemas import check_actions_payload, parse_actions
from .views import compact_observation, cpa_table

SYSTEM = """You are the {channel} channel agent of an ad-campaign team. Follow the PLAYBOOK exactly; it is the team's policy.
You manage only these line items: {items}. After each checkpoint you may take actions that apply to the NEXT period.
Reply with ONE JSON object and nothing else: {{"actions": [ ... ]}}  (an empty list means do nothing). Allowed actions:
 {{"type": "shift_budget", "from": "<one of your line items>", "to": "<any line item>", "amount": <dollars>}}
 {{"type": "pause", "line_item": "<yours>"}}
 {{"type": "rotate_creative", "line_item": "<yours>"}}   // attaches an already approved creative; fails if none is available
 {{"type": "set_holdout", "line_item": "<yours>", "share": <0 to end, else 0.05-0.5>}}   // only if capabilities.holdouts is true
Rules: data marked "unavailable", null, or status "provisional" may be incomplete; do not make budget-moving
decisions from provisional data. Never exceed the campaign's spend limits. Actions that break the rules are rejected."""


def decide(channel: str, line_items: List[str], playbook: str, obs: dict, *, client=None,
           recorder: Optional[Callable] = None, ctx: Optional[dict] = None) -> Tuple[List[dict], List[dict], CallResult]:
    """Returns (valid actions, rejected actions, the model CallResult). Falls back to no actions on model failure."""
    view = compact_observation(obs)
    user = (f"PLAYBOOK:\n{playbook}\n\nOBSERVATION (your line items: {line_items}):\n{json.dumps(view)}\n\n"
            f"LATEST REPORTED CPA, ALL LINE ITEMS (read-only context):\n{json.dumps(cpa_table(obs))}")
    messages = [{"role": "system", "content": SYSTEM.format(channel=channel, items=", ".join(line_items))},
                {"role": "user", "content": user}]
    res = call_model("channel", messages, client=client, recorder=recorder, schema_check=check_actions_payload,
                     fallback=[], expect="any", ctx=ctx)
    valid, rejected = parse_actions(res.parsed, obs["campaign"], line_items)
    return valid, rejected, res


def channel_groups(campaign: dict) -> Dict[str, List[str]]:
    """{channel: [line items]} from the campaign's capabilities, in declaration order."""
    groups: Dict[str, List[str]] = {}
    for li, info in campaign["capabilities"]["line_items"].items():
        groups.setdefault(info["channel"], []).append(li)
    return groups
