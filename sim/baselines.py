"""Non-AI strategies for the sanity check: naive must score below smart on every split.

Run: .venv/bin/python -m sim.baselines
"""
from __future__ import annotations

import numpy as np

from . import LINE_ITEMS, Simulator, generate_campaigns, score


def equal_split(sim: Simulator) -> None:
    sim.apply_plan({"allocations": {li: 1 for li in LINE_ITEMS}})
    while not sim.done:
        sim.step()


def naive_last_click(sim: Simulator) -> None:
    """What playbook v1 does: trust last-click CPA, scale winners."""
    sim.apply_plan({"allocations": {li: 1 for li in LINE_ITEMS}})
    while not sim.done:
        sim.step()
        last = sim.observe()["history"][-1]["line_items"]
        cpas = {li: m["cpa"] for li, m in last.items() if m["cpa"]}
        if len(cpas) >= 2 and not sim.done:
            best, worst = min(cpas, key=cpas.get), max(cpas, key=cpas.get)
            sim.act({"type": "shift_budget", "from": worst, "to": best, "amount": 0.5 * sim.budgets[worst]})


def smart_holdout(sim: Simulator) -> None:
    """What a good playbook should converge to: prospecting-heavy start, holdout-test the cheap
    last-click items for 2 weeks, fund by measured incremental CPA, rotate creative weekly."""
    suspects = ["meta_retargeting", "google_brand"]
    sim.apply_plan({
        "allocations": {"meta_prospecting": 0.45, "meta_retargeting": 0.15, "google_nonbrand": 0.25, "google_brand": 0.15},
        "holdouts": {li: 0.2 for li in suspects},
    })
    while not sim.done:
        sim.step()
        if sim.done:
            break
        obs = sim.observe()
        for li in LINE_ITEMS:
            sim.act({"type": "rotate_creative", "line_item": li})
        if obs["week"] == 2:
            last = obs["history"][-1]["line_items"]
            pros_cpa = last["meta_prospecting"]["cpa"] or 1e9
            for li in suspects:
                share = last[li]["holdout"]["incremental_share_est"]
                sim.act({"type": "set_holdout", "line_item": li, "share": 0})
                inc_cpa = (last[li]["cpa"] or 1e9) / max(share, 0.01)
                if inc_cpa > 1.5 * pros_cpa:  # not worth it: move most of it to prospecting
                    sim.act({"type": "shift_budget", "from": li, "to": "meta_prospecting", "amount": 0.8 * sim.budgets[li]})


STRATEGIES = {"equal_split": equal_split, "naive_last_click": naive_last_click, "smart_holdout": smart_holdout}


def evaluate(strategy, split: str, n: int = 6, seeds=(0, 1, 2)) -> float:
    scores = []
    for c in generate_campaigns(n, split, seed=0):
        for s in seeds:
            sim = Simulator(c, seed=s)
            strategy(sim)
            scores.append(score(sim))
    return float(np.mean(scores))


if __name__ == "__main__":
    print(f"{'strategy':<18}" + "".join(f"{s:>12}" for s in ("train", "validation", "test")))
    for name, fn in STRATEGIES.items():
        print(f"{name:<18}" + "".join(f"{evaluate(fn, s):>12.3f}" for s in ("train", "validation", "test")))
