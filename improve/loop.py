"""Improvement loop: train failures -> improver proposes one edit -> gate on validation -> keep or revert.

Plug-ins (import strings "module:function", sync or async):
    runner(version, sim) -> None
        Plays a whole campaign. `version` is a playbook file path.
    propose_edit(playbook, failures, history) -> dict | None
        playbook: current playbook text. failures: worst train runs (see summarize_run).
        history: earlier proposals [{round, summary, accepted, mean_diff, reason}].
        Returns {"new_playbook": str, "summary": str, "rationale": str, "sources": [str]}, or None to stop.

Each run lives in runs/<name>/: playbooks/v1.md, v2.md, ... (accepted versions), candidates/roundN.md.
The change log, playbook versions, gate results and summary are written to the run store (store/adswarm.db),
which is the source of record; report/build_page.py renders runs/<name>/index.html from it.
"""
from __future__ import annotations

import asyncio
import difflib
import importlib
import inspect
import json
import os
import shutil
import time
import traceback
from typing import Callable, List, Optional

import numpy as np

from sim import LINE_ITEMS
from sim.gate import MARGIN, evaluate_version, run_gate
from store import DEFAULT_PATH, Store, truth

N_FAILURES = 3
TRAIN_SEEDS = (0,)  # one seed per train campaign is enough to find failures


def load(spec: str) -> Callable:
    module, fn = spec.split(":")
    return getattr(importlib.import_module(module), fn)


async def _call(fn: Callable, *args):
    if inspect.iscoroutinefunction(fn):
        return await fn(*args)
    return await asyncio.to_thread(fn, *args)


def summarize_run(row: dict, store_path: Optional[str] = None) -> dict:
    """What the improver sees for one train run: score, hidden truth, observed metrics, agent actions."""
    sim = row["sim"]
    debrief = None
    if store_path and row.get("run_id"):  # train-split hidden truth, through the store's privileged accessor
        with Store(store_path) as st:
            debrief = truth.train_debrief(st, row["run_id"])
    debrief = debrief or sim.debrief()
    checkpoints = []
    for w in sim.history:
        checkpoints.append({"checkpoint": w["checkpoint"], "period": w["period"], "status": w["status"], "line_items": {li: {
            "spend": m["spend"], "attributed_conversions": m["reported"]["attributed_conversions"],
            "cpa": m["reported"]["cpa"],
            "ctr": m.get("ctr"), "frequency": m.get("frequency"), "roas": m["reported"]["roas"],
            **({"impression_share": m["search_impression_share"],
                "budget_lost_impression_share": m["search_budget_lost_impression_share"]}
               if m.get("search_impression_share") is not None else {}),
            "creative_age_checkpoints": m["creative_age_checkpoints"],
            **({"holdout_incremental_share": m["measured"]["holdout"]["incremental_share_est"],
                "holdout_ci95": m["measured"]["holdout"]["ci95"]} if m["measured"]["holdout"] else {}),
        } for li, m in w["line_items"].items()}})
    events = []
    for e in sim.trace:
        if e["kind"] == "step":
            continue
        e = {k: v for k, v in e.items() if k != "observed"}
        if isinstance(e.get("error"), str):
            e["error"] = e["error"][-500:]
        events.append(e)
    return {
        "campaign": row["campaign"], "seed": row["seed"], "score": round(row["score"], 3),
        "agent_error": row["error"],
        "truth": debrief["line_items"],
        "achieved_vs_best": [debrief["achieved_incremental"], debrief["best_possible_incremental"]],
        "checkpoints": checkpoints,
        "events": events,  # plan, actions (with ok/error), and anything the runner appends to sim.trace
    }


def worst_runs(scores: dict, k: int = N_FAILURES, store_path: Optional[str] = None) -> List[dict]:
    rows = sorted(scores.values(), key=lambda r: r["score"])
    return [summarize_run(r, store_path) for r in rows[:k]]


def _diff(old: str, new: str, old_name: str, new_name: str) -> str:
    return "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True), old_name, new_name))


def _strip(scores: dict) -> dict:
    return {k: {"score": round(r["score"], 4), "error": r["error"]} for k, r in scores.items()}


async def improve(runner: Callable, improver: Callable, start: str, name: str, rounds: int,
                  margin: float = MARGIN, overwrite: bool = False, store_path: str = DEFAULT_PATH) -> dict:
    root = os.path.join("runs", name)
    if os.path.exists(root):
        if not overwrite:
            raise RuntimeError(f"{root} exists; pick a new --name or pass --overwrite")
        shutil.rmtree(root)
    os.makedirs(os.path.join(root, "playbooks"))
    os.makedirs(os.path.join(root, "candidates"))
    store = Store(store_path)
    store.start_session(name, overwrite=overwrite)

    def log(entry: dict):
        entry["time"] = time.strftime("%H:%M:%S")
        store.record_improvement(name, entry)
        print(json.dumps({k: v for k, v in entry.items() if k not in ("diff", "rationale", "val_scores")})[:300], flush=True)

    current = os.path.join(root, "playbooks", "v1.md")
    shutil.copy(start, current)
    store.save_playbook(name, current, open(current).read())
    t0 = time.time()
    base_scores = await evaluate_version(runner, current, "validation", store_path=store_path)
    base_mean = float(np.mean([r["score"] for r in base_scores.values()]))
    versions = [{"version": current, "round": 0, "val_mean": base_mean}]
    log({"round": 0, "event": "baseline", "version": current, "val_mean": round(base_mean, 4),
         "val_scores": _strip(base_scores), "seconds": round(time.time() - t0, 1)})

    history: List[dict] = []
    for rnd in range(1, rounds + 1):
        t0 = time.time()
        playbook = open(current).read()
        train = await evaluate_version(runner, current, "train", seeds=TRAIN_SEEDS, keep_sims=True, store_path=store_path)
        train_mean = float(np.mean([r["score"] for r in train.values()]))
        try:
            proposal = await _call(improver, playbook, worst_runs(train, store_path=store_path), history)
        except Exception as e:
            log({"round": rnd, "event": "improver_error", "error": f"{type(e).__name__}: {e}",
                 "traceback": traceback.format_exc()[-1500:]})
            continue
        if not proposal:
            log({"round": rnd, "event": "no_proposal"})
            break
        new_text = proposal.get("new_playbook") or ""
        if not new_text.strip() or new_text.strip() == playbook.strip():
            log({"round": rnd, "event": "empty_edit", "summary": proposal.get("summary")})
            history.append({"round": rnd, "summary": proposal.get("summary"), "accepted": False,
                            "mean_diff": 0.0, "reason": "edit was empty or identical"})
            continue

        cand = os.path.join(root, "candidates", f"round{rnd}.md")
        with open(cand, "w") as f:
            f.write(new_text)
        gate = await run_gate(runner, current, cand, base_scores=base_scores, margin=margin, store_path=store_path)
        entry = {
            "round": rnd, "event": "gate", "base_version": current, "candidate": cand,
            "summary": proposal.get("summary"), "rationale": proposal.get("rationale"),
            "sources": proposal.get("sources") or [],
            "diff": _diff(playbook, new_text, os.path.basename(current), f"round{rnd}.md"),
            "train_mean": round(train_mean, 4),
            **{k: gate[k] for k in ("accepted", "reason", "base_mean", "cand_mean", "mean_diff", "ci95",
                                    "wins", "losses", "n_pairs", "cand_errors")},
            "val_scores": _strip(gate["cand_scores"]),
        }
        if gate["accepted"]:
            parent = current
            current = os.path.join(root, "playbooks", f"v{len(versions) + 1}.md")
            shutil.copy(cand, current)
            store.save_playbook(name, current, new_text, parent=parent, diff=entry["diff"],
                                gate_result={k: v for k, v in entry.items() if k not in ("diff", "val_scores")},
                                research_urls=entry["sources"])
            base_scores = gate["cand_scores"]
            versions.append({"version": current, "round": rnd, "val_mean": gate["cand_mean"]})
            entry["new_version"] = current
        entry["seconds"] = round(time.time() - t0, 1)
        log(entry)
        history.append({"round": rnd, "summary": proposal.get("summary"), "accepted": gate["accepted"],
                        "mean_diff": round(gate["mean_diff"], 4), "reason": gate["reason"]})

    summary = {"name": name, "start": start, "final_version": current, "versions": versions,
               "rounds": rounds, "history": history}
    store.finish_session(name, summary)
    store.close()
    return summary
