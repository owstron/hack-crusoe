"""Model-free stand-ins for B's runner and improver, to test the loop end to end.

The stub runner follows a playbook by keyword; the stub improver appends canned rules in order,
including one harmful rule so the gate has something to reject.
"""
from __future__ import annotations

from sim import LINE_ITEMS
from sim.baselines import smart_holdout  # noqa: F401  (reference behavior)

STUB_V1 = """# Playbook v1

1. Trust last-click CPA: it is the best signal of what works.
2. Each week, scale winners: move half of the worst-CPA line item's budget to the best-CPA line item.
"""

RULES = [
    ("Rotate creative weekly",
     "Rotate creative on every line item every week; fatigue silently erodes true lift.",
     ["https://example.org/braun-moe-2013"]),
    ("Scale winners harder",
     "Scale aggressively: move 80% of the worst-CPA line item's budget to the best-CPA line item each week.",
     []),
    ("Holdout-test cheap last-click items",
     "Before trusting a line item with very cheap last-click CPA, run a 20% holdout for 2 weeks; "
     "cut it if measured incremental CPA is over 1.5x prospecting CPA. Do not scale on last-click alone.",
     ["https://example.org/google-ghost-ads-432-experiments"]),
    ("Start prospecting-heavy",
     "Start with 45% of budget in Meta prospecting: last-click under-credits prospecting.",
     ["https://example.org/gordon-et-al-663-rcts"]),
]


def runner(version: str, sim) -> None:
    text = open(version).read().lower()
    rotate = "rotate creative" in text
    holdout = "holdout" in text
    aggressive = "scale aggressively" in text
    suspects = ["meta_retargeting", "google_brand"]
    if "prospecting-heavy" in text or "45% of budget in meta prospecting" in text:
        alloc = {"meta_prospecting": 0.45, "meta_retargeting": 0.15, "google_nonbrand": 0.25, "google_brand": 0.15}
    else:
        alloc = {li: 1 for li in LINE_ITEMS}
    sim.apply_plan({"allocations": alloc, "holdouts": {li: 0.2 for li in suspects} if holdout else {}})
    while not sim.done:
        sim.step()
        if sim.done:
            break
        obs = sim.observe()
        last = obs["history"][-1]["line_items"]
        if rotate:
            for li in LINE_ITEMS:
                sim.act({"type": "rotate_creative", "line_item": li})
        if holdout:
            if obs["checkpoint"] == 2:
                pros_cpa = last["meta_prospecting"]["reported"]["cpa"] or 1e9
                for li in suspects:
                    share = last[li]["measured"]["holdout"]["incremental_share_est"]
                    sim.act({"type": "set_holdout", "line_item": li, "share": 0})
                    if (last[li]["reported"]["cpa"] or 1e9) / max(share, 0.01) > 1.5 * pros_cpa:
                        sim.act({"type": "shift_budget", "from": li, "to": "meta_prospecting",
                                 "amount": 0.8 * sim.budgets[li]})
            continue
        cpas = {li: m["reported"]["cpa"] for li, m in last.items() if m["reported"]["cpa"]}
        if len(cpas) >= 2:
            best, worst = min(cpas, key=cpas.get), max(cpas, key=cpas.get)
            sim.act({"type": "shift_budget", "from": worst, "to": best,
                     "amount": (0.8 if aggressive else 0.5) * sim.budgets[worst]})


def propose_edit(playbook: str, failures: list, history: list):
    tried = {h["summary"] for h in history}
    for summary, rule, sources in RULES:
        if summary not in tried:
            n = sum(1 for line in playbook.splitlines() if line[:1].isdigit()) + 1
            return {
                "new_playbook": playbook.rstrip() + f"\n{n}. {rule}\n",
                "summary": summary,
                "rationale": f"stub; worst train score {failures[0]['score']}",
                "sources": sources,
            }
    return None
