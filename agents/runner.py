"""Runner: one resumable unit (run_checkpoint) and the loop the simulator gate plugs into (run).

All agent logic lives in run_checkpoint, so simulator and live behavior cannot diverge. A call handles exactly one
checkpoint of a CampaignBackend:
  checkpoint 0   : Planner -> Verifier (definition of done) -> apply_plan -> advance
  checkpoint 1..n-1: Channel agents act on the latest observation -> advance
  campaign done  : Verifier interprets the final evidence; stage becomes "finished"
State is a small JSON-serializable dict (see new_state) so a fresh process can resume from the run store.
Model failures are recorded and the campaign continues on its current allocation.
"""
from __future__ import annotations

import copy
import threading
import traceback
from typing import Callable, Optional

from backends.simulator import SimulatorBackend

from . import channel as channel_agent
from . import planner as planner_agent
from . import verifier as verifier_agent
from .planner import default_plan

_client = None
_client_lock = threading.Lock()


def _default_client():
    global _client
    with _client_lock:
        if _client is None:
            from .models import make_client
            _client = make_client()
        return _client


def new_state() -> dict:
    return {"stage": "start", "plan_applied": False, "done_def": None, "verdict": None, "due_at": None, "errors": 0}


def _read_playbook(version: str) -> str:
    with open(version) as f:
        return f.read()


class _Log:
    """Routes trace records to the run store (if given) and to the simulator's in-memory trace (if any)."""

    def __init__(self, backend, store, run_id):
        self.backend, self.store, self.run_id = backend, store, run_id
        self.sim = getattr(backend, "sim", None)
        self.idx = 0

    def model_call(self, rec: dict) -> None:
        if self.store is not None:
            self.store.record_model_call({"run_id": self.run_id, **rec})
        elif self.sim is not None:
            self.sim.trace.append({"checkpoint": rec.get("checkpoint_idx", self.idx), "kind": "model_call",
                                   "payload": rec, "ok": rec.get("error") is None, "error": rec.get("error")})

    def rejected(self, action, error: str) -> None:
        if self.store is not None:
            self.store.record_action(self.run_id, self.idx, {"_kind": "rejected", "action": action}, False, error)
        elif self.sim is not None:
            self.sim.trace.append({"checkpoint": self.idx, "kind": "rejected", "payload": {"action": action},
                                   "ok": False, "error": error})

    def action(self, action: dict, result: dict) -> None:
        if self.store is not None:  # a simulator backend logs actions in its own trace; live backends rely on the store
            self.store.record_action(self.run_id, self.idx, action, result["ok"], result["error"])

    def error(self, text: str) -> None:
        if self.store is not None:
            self.store.record_action(self.run_id, self.idx, {"_kind": "error"}, False, text[-2000:])
        elif self.sim is not None:
            self.sim.trace.append({"checkpoint": self.idx, "kind": "error", "error": text})


def run_checkpoint(version: str, backend, state: dict, *, client=None, store=None, run_id: Optional[str] = None,
                   want_creatives: bool = False) -> dict:
    """Handle exactly one checkpoint and return the updated state (including `due_at`)."""
    state = copy.deepcopy(state)
    if state["stage"] == "finished":
        return state
    if backend.status == "awaiting_data":  # live: not due yet; touch nothing
        return state
    client = client or _default_client()
    playbook = _read_playbook(version)
    obs = backend.observe()
    campaign = obs["campaign"]
    log = _Log(backend, store, run_id)
    log.idx = idx = obs["checkpoint"]

    def recorder(ctx_read_no: int = 0):
        return lambda rec: log.model_call({"checkpoint_idx": idx, "read_no": ctx_read_no, **rec})

    if backend.done:
        if state["verdict"] is None:
            try:
                state["verdict"] = verifier_agent.final_verdict(
                    state["done_def"] or verifier_agent.DEFAULT_CRITERIA, obs, client=client, recorder=recorder(1)).parsed
            except Exception:
                state["errors"] += 1
                log.error(traceback.format_exc())
        state["stage"] = "finished"
        if store is not None:
            store.save_state(run_id, state, None)
            store.set_run_status(run_id, "done")
        return state

    if not state["plan_applied"]:
        _plan(playbook, campaign, backend, state, client, recorder(0), log, want_creatives)
    else:
        _act(playbook, backend, client, recorder(1), log, state)
    state["stage"] = "running"
    state["due_at"] = backend.advance()["due_at"]
    if store is not None:
        if not getattr(backend, "records_checkpoints", False):  # a live backend stores its own readings (with raw data)
            new = backend.observe()
            if new["history"]:
                h = new["history"][-1]
                store.record_checkpoint(run_id, new["checkpoint"], h, period=h["period"], provisional=h["status"] == "provisional")
        store.save_state(run_id, state, state["due_at"])
    return state


def _plan(playbook, campaign, backend, state, client, rec, log, want_creatives) -> None:
    try:
        state["done_def"] = verifier_agent.define_done(playbook, campaign, client=client, recorder=rec).parsed
    except Exception:
        state["errors"] += 1
        log.error(traceback.format_exc())
    try:
        plan = planner_agent.plan(playbook, campaign, client=client, recorder=rec, want_creatives=want_creatives).parsed
    except Exception:
        state["errors"] += 1
        log.error(traceback.format_exc())
        plan = default_plan(campaign)
    result = backend.apply_plan(plan)
    log.action({"_kind": "plan", **plan}, result)
    if not result["ok"]:  # never start a campaign without a plan: fall back to the equal split
        state["errors"] += 1
        log.error(f"plan rejected by backend: {result['error']}")
        fallback = default_plan(campaign)
        log.action({"_kind": "plan", **fallback}, backend.apply_plan(fallback))
    state["plan_applied"] = True


def _act(playbook, backend, client, rec, log, state) -> None:
    for channel, items in channel_agent.channel_groups(backend.observe()["campaign"]).items():
        obs = backend.observe()  # fresh: an earlier agent's actions may have changed budgets
        try:
            valid, rejected, _ = channel_agent.decide(channel, items, playbook, obs, client=client, recorder=rec)
        except Exception:
            state["errors"] += 1
            log.error(traceback.format_exc())
            continue
        for r in rejected:
            log.rejected(r["action"], r["error"])
        for action in valid:
            log.action(action, backend.act(action))


def run(version: str, sim, client=None) -> None:
    """Gate/improvement plug-in signature: play a whole campaign on a simulator with the playbook at `version`."""
    backend = SimulatorBackend.wrap(sim)
    state = new_state()
    for _ in range(backend.sim.campaign.public()["n_checkpoints"] + 3):  # bounded: each call advances or finishes
        state = run_checkpoint(version, backend, state, client=client)
        if state["stage"] == "finished":
            return
    raise RuntimeError("runner did not finish the campaign")
