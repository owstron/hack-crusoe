"""Continuous mode: `ad_swarm.py live run` keeps going until stopped.

Two loops share one process:
  campaigns   create -> approve -> activate -> checkpoints until done -> report -> the next campaign, forever.
              Each campaign starts with the newest playbook the improvement loop has accepted.
  improvement one `improve.loop` round at a time in the simulator (diagnose, edit, gate), forever. It only
              publishes a playbook once the gate has accepted it, so the campaigns never see a half-made edit.
Stop with Ctrl-C / SIGTERM or `touch runs/STOP`. The current checkpoint finishes first; an improvement round that is
cut off is simply run again on the next start (`improve` resumes from its last logged round).
Dry-run only: nothing here sends a write to Meta.
"""
from __future__ import annotations

import asyncio
import os
import signal
import threading
import time
from typing import Callable, Optional

from store import Store

STOP_FILE = os.path.join("runs", "STOP")


class Stopper:
    def __init__(self):
        self.event = threading.Event()

    def install(self) -> None:
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: self.set("signal received"))

    def set(self, why: str) -> None:
        if not self.event.is_set():
            print(f"\nstopping ({why}): finishing the current step...", flush=True)
        self.event.set()

    @property
    def stopped(self) -> bool:
        if not self.event.is_set() and os.path.exists(STOP_FILE):
            self.set(f"{STOP_FILE} exists")
        return self.event.is_set()

    def sleep(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while not self.stopped and time.monotonic() < end:
            time.sleep(min(0.5, max(0.0, end - time.monotonic())))


def _say(tag: str, text: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {tag:<9} {text}", flush=True)


class Improver(threading.Thread):
    """Runs improvement rounds in the background and publishes the newest accepted playbook."""

    def __init__(self, name: str, start: str, db: str, stopper: Stopper,
                 runner_spec: str = "agents.runner:run", improver_spec: str = "agents.improver:propose_edit",
                 pause_s: float = 5.0):
        super().__init__(daemon=True, name="improver")
        self.name_, self.start_pb, self.db, self.stopper, self.pause_s = name, start, db, stopper, pause_s
        self.runner_spec, self.improver_spec = runner_spec, improver_spec
        self.playbook = start
        self.rounds_done = 0
        self._lock = threading.Lock()

    def current(self) -> str:
        with self._lock:
            return self.playbook

    def _sync(self) -> None:
        """Pick the newest accepted version out of the session's change log."""
        from improve.loop import _restore
        with Store(self.db) as st:
            data = st.session_data(self.name_)
        if data and any(e.get("event") == "baseline" for e in data["changelog"]):
            s = _restore(data["changelog"])
            with self._lock:
                self.rounds_done, newer = s["last_round"], s["current"] != self.playbook
                self.playbook = s["current"] if os.path.exists(s["current"]) else self.playbook
            if newer:
                _say("improver", f"new playbook published: {self.playbook}")

    def run(self) -> None:
        os.environ.setdefault("GATE_CONCURRENCY", "4")  # leave model capacity for the live agents
        from improve.loop import improve, load
        runner, improver = load(self.runner_spec), load(self.improver_spec)
        try:
            self._sync()
        except Exception as e:  # a fresh session has nothing to restore yet
            _say("improver", f"fresh session ({type(e).__name__})")
        while not self.stopper.stopped:
            rnd = self.rounds_done + 1
            _say("improver", f"round {rnd}: " + ("establishing the baseline first" if rnd == 1 else "diagnose, edit, gate"))
            try:
                resume = os.path.isdir(os.path.join("runs", self.name_))
                asyncio.run(improve(runner, improver, self.start_pb, self.name_, rnd, store_path=self.db, resume=resume))
                self._sync()
            except Exception as e:
                _say("improver", f"round {rnd} failed: {type(e).__name__}: {e}")
            self.stopper.sleep(self.pause_s)


def run_campaign(a, store: Store, playbook: str, stopper: Stopper, cycle: int, api_factory: Callable,
                 cfg: dict) -> Optional[dict]:
    """One whole campaign. Returns a summary, or None if stopped before it finished."""
    from agents import runner
    from backends import MetaBackend
    from report.live_report import build
    import ad_swarm
    api = api_factory(True)
    backend = MetaBackend.create(store, api, cfg, playbook_version=playbook)
    run_id = backend.run_id
    _say("campaign", f"cycle {cycle}: {run_id} with {playbook}")
    state = runner.run_checkpoint(playbook, backend, runner.new_state(), store=store, run_id=run_id,
                                  want_creatives=True, echo=lambda t: _say("agent", t))
    n = 0
    for c in store.list_creatives(run_id, "pending"):  # dry run only: a human approval gate is not meaningful here
        store.set_creative_status(run_id, c["line_item"], c["creative_id"], "approved")
        n += 1
    _say("campaign", f"auto-approved {n} creatives (dry run); sync {backend.sync_creatives()}")
    res = backend.activate()
    if not res["ok"]:
        _say("campaign", f"activate failed: {res['error']}")
        return {"run_id": run_id, "error": res["error"]}
    seen = 0
    while not stopper.stopped:
        if backend.done:
            break
        if backend.status == "awaiting_data":
            stopper.sleep(a.every)
            continue
        state = runner.run_checkpoint(playbook, backend, state, store=store, run_id=run_id, want_creatives=True,
                                      echo=lambda t: _say("agent", t))
        seen = ad_swarm._print_readings(backend, seen)
    if not backend.done:
        _say("campaign", f"{run_id} left unfinished; resume it with `live watch --run-id {run_id}`")
        return None
    spend = sum(sum(m["spend"] for m in h["line_items"].values()) for h in backend.s["history"] if h["read_no"] == 1)
    verdict = (state.get("verdict") or {}).get("verdict")
    _say("campaign", f"cycle {cycle} done: spend ${spend:.2f}, verdict {verdict}, report {build(run_id, a.db)}")
    return {"run_id": run_id, "spend": spend, "verdict": verdict}


def supervise(a, cfg: dict, api_factory: Callable) -> int:
    stopper = Stopper()
    stopper.install()
    if os.path.exists(STOP_FILE):
        os.remove(STOP_FILE)  # a leftover stop request from the last run
    improver = None
    if a.improve_session:
        improver = Improver(a.improve_session, a.playbook, a.db, stopper)
        improver.start()
    cycle, results = 0, []
    while not stopper.stopped and (a.max_cycles is None or cycle < a.max_cycles):
        cycle += 1
        playbook = improver.current() if improver else a.playbook
        with Store(a.db) as store:
            out = run_campaign(a, store, playbook, stopper, cycle, api_factory, cfg)
        if out is None:
            break
        results.append(out)
        stopper.sleep(2)
    _say("stopped", f"{len(results)} campaign(s) finished"
         + (f"; improvement rounds logged: {improver.rounds_done}; newest playbook {improver.current()}" if improver else ""))
    return 0
