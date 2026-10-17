"""Gate: decides whether candidate Version N+1 replaces Version N, on VALIDATION campaigns only.

Plug-in point for the agent side is one callable:
    runner(version, sim) -> None    # plays the whole campaign on `sim`; may be sync or async
`version` is opaque here (e.g. a playbook path). Both versions run on the same campaigns and the
same sim seeds, so each (campaign, seed) gives one paired score difference.

    base = await evaluate_version(runner, "playbooks/v1.md", "validation")
    result = await run_gate(runner, "playbooks/v1.md", "playbooks/v2.md", base_scores=base)
    result["accepted"]  -> bool; the whole dict goes into the change log
"""
from __future__ import annotations

import asyncio
import inspect
import time
import traceback
from typing import Callable, Dict, Optional, Sequence

import numpy as np

from . import Simulator, generate_campaigns, score

N_CAMPAIGNS = 6
SEEDS = (0, 1, 2)
CAMPAIGN_SEED = 0  # campaign generation seed; fixed so every version sees the same campaigns
MARGIN = 0.01      # candidate must beat base by more than this (mean paired score difference)
CONCURRENCY = 16

# two-sided 95% t critical values by degrees of freedom (no scipy dependency)
_T95 = {1: 12.71, 2: 4.30, 3: 3.18, 4: 2.78, 5: 2.57, 6: 2.45, 7: 2.36, 8: 2.31, 9: 2.26, 10: 2.23,
        12: 2.18, 15: 2.13, 17: 2.11, 20: 2.09, 25: 2.06, 30: 2.04}


def _t95(df: int) -> float:
    if df >= 30:
        return 1.96 if df > 60 else 2.0
    return _T95[max(k for k in _T95 if k <= df)]


async def _play(runner: Callable, version, sim: Simulator) -> Optional[str]:
    """Run the agent; on any crash, finish remaining weeks on autopilot so the pair still scores."""
    error = None
    try:
        if inspect.iscoroutinefunction(runner):
            await runner(version, sim)
        else:
            await asyncio.to_thread(runner, version, sim)
    except Exception as e:  # agent/model failures must not kill the gate
        error = f"{type(e).__name__}: {e}"
        sim.trace.append({"week": sim.week, "kind": "error", "error": traceback.format_exc()})
    while not sim.done:  # agent returned early or crashed: keep current budgets
        sim.step()
    return error


async def evaluate_version(runner: Callable, version, split: str, n: int = N_CAMPAIGNS,
                           seeds: Sequence[int] = SEEDS, keep_sims: bool = False) -> Dict[str, dict]:
    """Score `version` on every (campaign, seed) of a split. Returns {"<campaign_id>/s<seed>": {...}}."""
    sem = asyncio.Semaphore(CONCURRENCY)  # created inside the running loop

    async def one(campaign, seed):
        async with sem:
            sim = Simulator(campaign, seed=seed)
            t0 = time.time()
            error = await _play(runner, version, sim)
            row = {"campaign": campaign.id, "seed": seed, "score": score(sim), "error": error,
                   "seconds": round(time.time() - t0, 1)}
            if keep_sims:
                row["sim"] = sim
            return f"{campaign.id}/s{seed}", row

    jobs = [one(c, s) for c in generate_campaigns(n, split, CAMPAIGN_SEED) for s in seeds]
    return dict(await asyncio.gather(*jobs))


def paired_stats(base: Dict[str, dict], cand: Dict[str, dict]) -> dict:
    keys = sorted(set(base) & set(cand))
    d = np.array([cand[k]["score"] - base[k]["score"] for k in keys])
    n = len(d)
    mean = float(d.mean()) if n else 0.0
    half = _t95(n - 1) * float(d.std(ddof=1)) / np.sqrt(n) if n > 1 else float("inf")
    return {
        "n_pairs": n,
        "base_mean": float(np.mean([base[k]["score"] for k in keys])) if n else 0.0,
        "cand_mean": float(np.mean([cand[k]["score"] for k in keys])) if n else 0.0,
        "mean_diff": mean,
        "ci95": [float(mean - half), float(mean + half)],
        "wins": int((d > 1e-9).sum()), "losses": int((d < -1e-9).sum()),
        "base_errors": sum(1 for k in keys if base[k]["error"]),
        "cand_errors": sum(1 for k in keys if cand[k]["error"]),
    }


def decide(stats: dict, margin: float = MARGIN, require_ci: bool = True) -> tuple:
    if stats["n_pairs"] == 0:
        return False, "no paired runs"
    if stats["cand_errors"] > stats["n_pairs"] // 4:
        return False, f"candidate crashed on {stats['cand_errors']}/{stats['n_pairs']} runs"
    if stats["mean_diff"] <= margin:
        return False, f"mean improvement {stats['mean_diff']:+.3f} not above margin {margin}"
    if require_ci and stats["ci95"][0] <= 0:
        return False, f"improvement {stats['mean_diff']:+.3f} but 95% CI {stats['ci95'][0]:+.3f} includes 0"
    return True, f"mean improvement {stats['mean_diff']:+.3f} (95% CI {stats['ci95'][0]:+.3f} to {stats['ci95'][1]:+.3f})"


async def run_gate(runner: Callable, base_version, cand_version, base_scores: Optional[Dict[str, dict]] = None,
                   margin: float = MARGIN, require_ci: bool = True) -> dict:
    """Paired comparison on VALIDATION. Pass base_scores (from a previous evaluate_version on the
    current accepted version) to skip rerunning it; the campaigns and seeds are identical."""
    t0 = time.time()
    if base_scores is None:
        base_scores, cand_scores = await asyncio.gather(
            evaluate_version(runner, base_version, "validation"),
            evaluate_version(runner, cand_version, "validation"))
    else:
        cand_scores = await evaluate_version(runner, cand_version, "validation")
    stats = paired_stats(base_scores, cand_scores)
    accepted, reason = decide(stats, margin, require_ci)
    return {
        "base_version": str(base_version), "cand_version": str(cand_version),
        "accepted": accepted, "reason": reason, "margin": margin, **stats,
        "seconds": round(time.time() - t0, 1),
        "base_scores": base_scores, "cand_scores": cand_scores,  # reuse cand_scores as next base if accepted
    }


if __name__ == "__main__":
    from .baselines import STRATEGIES

    def runner(version, sim):
        STRATEGIES[version](sim)

    async def main():
        for base, cand in [("naive_last_click", "smart_holdout"), ("smart_holdout", "naive_last_click"),
                           ("naive_last_click", "naive_last_click"), ("naive_last_click", "equal_split")]:
            r = await run_gate(runner, base, cand)
            print(f"{base:>17} -> {cand:<17} accepted={r['accepted']!s:<5} {r['reason']}  "
                  f"(W{r['wins']}/L{r['losses']}, {r['seconds']}s)")

    asyncio.run(main())
