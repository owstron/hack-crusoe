"""Final eval: Version 1 vs the final version on TEST campaigns. Run once, at the end.

    result = await final_eval(runner, "playbooks/v1.md", "playbooks/v7.md")

Writes results/final_eval.json (input for the results page) and refuses to overwrite it unless
force=True, so test results are never used to make decisions.
"""
from __future__ import annotations

import json
import os
import time
from typing import Callable

import numpy as np

from . import LINE_ITEMS
from .gate import evaluate_version, paired_stats
from .simulator import best_weekly_incremental

OUT_PATH = "results/final_eval.json"


def _spend_shares(sims) -> dict:
    """Average share of budget per line item actually spent vs the oracle's optimal share."""
    actual = {li: [] for li in LINE_ITEMS}
    optimal = {li: [] for li in LINE_ITEMS}
    for sim in sims:
        total = sum(w["line_items"][li]["spend"] for w in sim.history for li in LINE_ITEMS) or 1
        _, best = best_weekly_incremental(sim.campaign)
        for li in LINE_ITEMS:
            actual[li].append(sum(w["line_items"][li]["spend"] for w in sim.history) / total)
            optimal[li].append(best[li] / sim.campaign.weekly_budget)
    return {li: {"actual": round(float(np.mean(actual[li])), 3), "optimal": round(float(np.mean(optimal[li])), 3)}
            for li in LINE_ITEMS}


def _rotation_rate(sims) -> float:
    rot = sum(1 for s in sims for e in s.trace
              if e["kind"] == "action" and e["ok"] and e["payload"].get("type") == "rotate_creative")
    return round(rot / max(len(sims), 1), 2)


def _summary(scores: dict) -> dict:
    sims = [r["sim"] for r in scores.values()]
    return {
        "mean_score": round(float(np.mean([r["score"] for r in scores.values()])), 4),
        "errors": sum(1 for r in scores.values() if r["error"]),
        "spend_share_vs_optimal": _spend_shares(sims),
        "creative_rotations_per_campaign": _rotation_rate(sims),
        "runs": {k: {kk: v for kk, v in r.items() if kk != "sim"} for k, r in scores.items()},
    }


async def final_eval(runner: Callable, v1, final, out_path: str = OUT_PATH, force: bool = False) -> dict:
    if os.path.exists(out_path) and not force:
        raise RuntimeError(f"{out_path} exists: test set already used. Pass force=True only to fix a crash.")
    t0 = time.time()
    base = await evaluate_version(runner, v1, "test", keep_sims=True)
    cand = await evaluate_version(runner, final, "test", keep_sims=True)
    stats = paired_stats(base, cand)
    result = {
        "split": "test",
        "v1": str(v1), "final": str(final),
        "v1_mean": round(stats["base_mean"], 4), "final_mean": round(stats["cand_mean"], 4),
        "mean_diff": round(stats["mean_diff"], 4), "ci95": [round(x, 4) for x in stats["ci95"]],
        "wins": stats["wins"], "losses": stats["losses"], "n_pairs": stats["n_pairs"],
        "v1_detail": _summary(base), "final_detail": _summary(cand),
        "seconds": round(time.time() - t0, 1),
    }
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(result, f, indent=2)
    return result


if __name__ == "__main__":
    import asyncio

    from .baselines import STRATEGIES

    def runner(version, sim):
        STRATEGIES[version](sim)

    r = asyncio.run(final_eval(runner, "naive_last_click", "smart_holdout",
                               out_path="results/final_eval_baselines.json", force=True))
    print(f"test: v1 {r['v1_mean']:.3f} -> final {r['final_mean']:.3f}  diff {r['mean_diff']:+.3f} "
          f"CI {r['ci95']}  W{r['wins']}/L{r['losses']}")
    for label in ("v1_detail", "final_detail"):
        print(label, {li: v["actual"] for li, v in r[label]["spend_share_vs_optimal"].items()},
              "rotations:", r[label]["creative_rotations_per_campaign"])
    print("optimal  ", {li: v["optimal"] for li, v in r["v1_detail"]["spend_share_vs_optimal"].items()})
