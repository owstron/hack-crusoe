"""Write a finished simulator run through the store (the in-memory sim.trace stays; every entry is also persisted)."""
from __future__ import annotations

from typing import Optional

from . import truth
from .db import Store


def persist_sim(store: Store, sim, *, split: str, score: float, playbook_version: Optional[str] = None,
                config: Optional[dict] = None, with_debrief: bool = False, error: Optional[str] = None) -> str:
    """Create a mode='sim' run and write its checkpoints, plan/actions (incl. errors) and score."""
    from sim import SCHEMA_VERSION  # local import: store has no hard dependency on the simulator

    c = sim.campaign
    run_id = store.create_run(
        "sim", SCHEMA_VERSION, playbook_version, {"split": split, "campaign": c.id, **(config or {})},
        max_total_spend=sim.observe()["campaign"]["max_total_spend"], campaign_ref=c.id)
    for e in sim.trace:
        idx = e["checkpoint"]
        if e["kind"] == "step":
            obs = e["observed"]
            store.record_checkpoint(run_id, idx, obs, period=obs["period"], provisional=obs["status"] == "provisional")
        elif e["kind"] in ("plan", "action"):
            payload = dict(e["payload"])
            if e["kind"] == "plan":
                payload["_kind"] = "plan"
            store.record_action(run_id, idx, payload, e["ok"], e["error"])
        else:  # runner error entries
            store.record_action(run_id, idx, {"_kind": "error"}, False, e.get("error"))
    truth.save_score(store, run_id, split, score, sim.debrief() if with_debrief else None)
    store.set_run_status(run_id, "failed" if error else "done")
    return run_id
