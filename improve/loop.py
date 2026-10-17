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
from sim import gate as gate_mod
from sim.gate import MARGIN, evaluate_version, run_gate
from store import DEFAULT_PATH, Store, truth

N_FAILURES = 3
TRAIN_SEEDS = (0,)  # one seed per train campaign is enough to find failures
N_TRAIN = 6         # train campaigns per round
BUDGET_MOVES = ("shift_budget", "pause")  # actions that move money
IDLE_RATIO = 0.5  # a candidate whose agents make under this share of the base's budget moves is flagged as idle
STAGNATION_ROUNDS = 2  # rounds in a row without an accepted edit before the improver is asked to explore
RECHECK_SEED_BASE = 100  # an explore winner is re-gated on sim seeds RECHECK_SEED_BASE.. (fresh noise)


def load(spec: str) -> Callable:
    module, fn = spec.split(":")
    return getattr(importlib.import_module(module), fn)


async def _call(fn: Callable, *args, **kwargs):
    if inspect.iscoroutinefunction(fn):
        return await fn(*args, **kwargs)
    return await asyncio.to_thread(lambda: fn(*args, **kwargs))


def _accepts(fn: Callable, name: str) -> bool:
    params = inspect.signature(fn).parameters
    return name in params or any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


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
        if e["kind"] == "model_call":  # keep what the model decided, not the full prompts
            e["payload"] = {k: e["payload"].get(k) for k in ("role", "parsed_json", "fallback_used", "error")}
        if isinstance(e.get("error"), str):
            e["error"] = e["error"][-500:]
        events.append(e)
    moves = sum(1 for e in sim.trace if e["kind"] == "action" and e.get("ok")
                and (e.get("payload") or {}).get("type") in BUDGET_MOVES)
    return {
        "campaign": row["campaign"], "seed": row["seed"], "score": round(row["score"], 3),
        "agent_error": row["error"],
        "budget_moves": moves,  # 0 means the agents sat idle: the playbook may be too vague or too cautious to act on
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


def _restore(changelog: List[dict]) -> dict:
    """Rebuild the loop state from a session's change log (what `improve` logged before it stopped)."""
    base = next((e for e in changelog if e.get("event") == "baseline"), None)
    if base is None:
        raise RuntimeError("nothing to resume: the session has no baseline entry")
    current, base_scores = base["version"], base["val_scores"]
    versions = [{"version": current, "round": 0, "val_mean": base["val_mean"]}]
    history: List[dict] = []
    for e in changelog:
        if e.get("event") == "gate":
            history.append({"round": e["round"], "summary": e.get("summary"), "accepted": e["accepted"],
                            "mean_diff": round(e["mean_diff"], 4), "reason": e["reason"], "diff": e.get("diff")})
            if e["accepted"]:
                current, base_scores = e["new_version"], e["val_scores"]
                versions.append({"version": current, "round": e["round"], "val_mean": e["cand_mean"]})
        elif e.get("event") == "empty_edit":
            history.append({"round": e["round"], "summary": e.get("summary"), "accepted": False,
                            "mean_diff": 0.0, "reason": "edit was empty or identical"})
    return {"current": current, "base_scores": base_scores, "versions": versions, "history": history,
            "last_round": max(e.get("round", 0) for e in changelog)}


async def improve(runner: Callable, improver: Callable, start: str, name: str, rounds: int,
                  margin: float = MARGIN, overwrite: bool = False, store_path: str = DEFAULT_PATH,
                  resume: bool = False) -> dict:
    """With resume=True, continue an earlier session from its last logged round. A round that was cut off
    before it logged anything is run again from its start."""
    root = os.path.join("runs", name)
    store = Store(store_path)

    def log(entry: dict):
        entry["time"] = time.strftime("%H:%M:%S")
        store.record_improvement(name, entry)
        print(json.dumps({k: v for k, v in entry.items() if k not in ("diff", "rationale", "val_scores")})[:300], flush=True)

    if resume:
        data = store.session_data(name)
        if data is None or not os.path.isdir(root):
            raise RuntimeError(f"nothing to resume under {root} / session {name!r}")
        st = _restore(data["changelog"])
        current, base_scores, versions, history = st["current"], st["base_scores"], st["versions"], st["history"]
        first_round = st["last_round"] + 1
        print(f"resuming {name} at round {first_round} from {current}", flush=True)
    else:
        if os.path.exists(root):
            if not overwrite:
                raise RuntimeError(f"{root} exists; pick a new --name or pass --overwrite")
            shutil.rmtree(root)
        os.makedirs(os.path.join(root, "playbooks"))
        os.makedirs(os.path.join(root, "candidates"))
        store.start_session(name, overwrite=overwrite)
        current = os.path.join(root, "playbooks", "v1.md")
        shutil.copy(start, current)
        store.save_playbook(name, current, open(current).read())
        t0 = time.time()
        base_scores = await evaluate_version(runner, current, "validation", store_path=store_path)
        base_mean = float(np.mean([r["score"] for r in base_scores.values()]))
        versions = [{"version": current, "round": 0, "val_mean": base_mean}]
        log({"round": 0, "event": "baseline", "version": current, "val_mean": round(base_mean, 4),
             "val_scores": _strip(base_scores), "seconds": round(time.time() - t0, 1)})
        history = []
        first_round = 1

    for rnd in range(first_round, rounds + 1):
        t0 = time.time()
        playbook = open(current).read()
        train = await evaluate_version(runner, current, "train", n=N_TRAIN, seeds=TRAIN_SEEDS, keep_sims=True,
                                     store_path=store_path)
        train_mean = float(np.mean([r["score"] for r in train.values()]))
        stagnant = (rnd - 1) - versions[-1]["round"]  # rounds since the last accepted edit
        explore = {"seed": f"{name}:{rnd}"} if stagnant >= STAGNATION_ROUNDS and _accepts(improver, "explore") else None
        if explore:
            print(f"  round {rnd}: {stagnant} rounds without an accepted edit, exploring (seed {explore['seed']})", flush=True)
        try:
            proposal = await _call(improver, playbook, worst_runs(train, store_path=store_path), history,
                                   **({"explore": explore} if explore else {}))
        except Exception as e:
            log({"round": rnd, "event": "improver_error", "error": f"{type(e).__name__}: {e}",
                 "traceback": traceback.format_exc()[-1500:]})
            continue
        if not proposal:
            log({"round": rnd, "event": "no_proposal"})
            break
        # One proposal may carry alternatives ("alternatives": more edits of the same shape). Each is gated against the
        # same baseline scores; at most one (the best passing) is accepted per round.
        proposals = [proposal] + list(proposal.get("alternatives") or [])
        usable = []
        for i, prop in enumerate(proposals, 1):
            new_text = prop.get("new_playbook") or ""
            if not new_text.strip() or new_text.strip() == playbook.strip():
                log({"round": rnd, "event": "empty_edit", "summary": prop.get("summary")})
                history.append({"round": rnd, "summary": prop.get("summary"), "accepted": False,
                                "mean_diff": 0.0, "reason": "edit was empty or identical"})
                continue
            tag = f"round{rnd}" if len(proposals) == 1 else f"round{rnd}_{i}"
            cand = os.path.join(root, "candidates", f"{tag}.md")
            with open(cand, "w") as f:
                f.write(new_text)
            usable.append((tag, cand, prop, new_text))
        if not usable:
            continue
        gates = await asyncio.gather(*[run_gate(runner, current, cand, base_scores=base_scores, margin=margin,
                                                store_path=store_path) for _, cand, _, _ in usable])
        passing = [k for k, g in enumerate(gates) if g["accepted"]]
        winner = max(passing, key=lambda k: gates[k]["mean_diff"]) if passing else None
        best, recheck = winner, None
        if explore and winner is not None:
            # Exploration tries many random ideas against the same validation campaigns, so a winner must also pass
            # the gate again on fresh sim seeds before it is accepted.
            seeds = tuple(range(RECHECK_SEED_BASE, RECHECK_SEED_BASE + len(gate_mod.SEEDS)))
            recheck = await run_gate(runner, current, usable[winner][1], margin=margin, store_path=store_path, seeds=seeds)
            if not recheck["accepted"]:
                best = None
        chosen = None
        base_moves = store.budget_moves_per_run(current, "validation")
        for k, ((tag, cand, prop, new_text), gate) in enumerate(zip(usable, gates)):
            accepted = k == best
            reason = gate["reason"]
            cand_moves = store.budget_moves_per_run(cand, "validation")
            if base_moves and cand_moves is not None and cand_moves < IDLE_RATIO * base_moves:
                reason += (f" [idle: agents made {cand_moves:.1f} budget moves per run vs {base_moves:.1f} for the base;"
                           " the rules were probably too vague or cautious to act on]")
            if k == winner and recheck is not None and not accepted:
                reason = f"passed the gate ({gate['mean_diff']:+.3f}) but failed the fresh-seed recheck: {recheck['reason']}"
            elif gate["accepted"] and not accepted:
                reason = f"passed the gate ({gate['mean_diff']:+.3f}) but {usable[winner][0]} was better"
            entry = {
                "round": rnd, "event": "gate", "base_version": current, "candidate": cand,
                "summary": prop.get("summary"), "rationale": prop.get("rationale"),
                "sources": prop.get("sources") or proposal.get("sources") or [],
                **({"improver_calls": proposal["improver_calls"]} if proposal.get("improver_calls") else {}),
                **({"diagnosis": proposal["diagnosis"]} if proposal.get("diagnosis") else {}),
                **({"explore": {**proposal["explore"], "stagnant_rounds": stagnant}} if proposal.get("explore") else {}),
                **({"recheck": {key: recheck[key] for key in ("accepted", "reason", "base_mean", "cand_mean", "mean_diff",
                                                              "ci95", "wins", "losses", "n_pairs")}}
                   if recheck is not None and k == winner else {}),
                "diff": _diff(playbook, new_text, os.path.basename(current), f"{tag}.md"),
                "train_mean": round(train_mean, 4),
                **{key: gate[key] for key in ("base_mean", "cand_mean", "mean_diff", "ci95", "wins", "losses",
                                              "n_pairs", "cand_errors")},
                "accepted": accepted, "reason": reason,
                "budget_moves_per_run": {"base": base_moves, "candidate": cand_moves},
                "val_scores": _strip(gate["cand_scores"]),
            }
            if accepted:
                parent = current
                chosen = (tag, gate)
                current = os.path.join(root, "playbooks", f"v{len(versions) + 1}.md")
                shutil.copy(cand, current)
                store.save_playbook(name, current, new_text, parent=parent, diff=entry["diff"],
                                    gate_result={key: v for key, v in entry.items() if key not in ("diff", "val_scores")},
                                    research_urls=entry["sources"])
                base_scores = gate["cand_scores"]
                versions.append({"version": current, "round": rnd, "val_mean": gate["cand_mean"]})
                entry["new_version"] = current
            entry["seconds"] = round(time.time() - t0, 1)
            log(entry)
            history.append({"round": rnd, "summary": prop.get("summary"), "accepted": accepted,
                            "mean_diff": round(gate["mean_diff"], 4), "reason": reason, "diff": entry["diff"]})

    summary = {"name": name, "start": start, "final_version": current, "versions": versions,
               "rounds": rounds, "history": history}
    store.finish_session(name, summary)
    store.close()
    return summary
