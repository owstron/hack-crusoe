import json
import pathlib

import pytest

import agents.runner as runner
from agents.models import FakeClient
from backends.simulator import SimulatorBackend
from sim import LINE_ITEMS, Simulator, generate_campaigns, score
from sim.baselines import equal_split
from store import Store, reset_db

PLAN = {"allocations": {"meta_prospecting": 0.45, "meta_retargeting": 0.15, "google_nonbrand": 0.25, "google_brand": 0.15},
        "holdouts": {"meta_retargeting": 0.2}, "hypotheses": {"meta_retargeting": "cheap CPA may be over-credited"}}


class Scripted(FakeClient):
    """Role-aware fake: picks a reply from the system prompt."""

    def __init__(self, bad=False):
        super().__init__(["{}"])
        self.bad = bad
        self.prompts = []

    def create(self, **kw):
        sys_, user = kw["messages"][0]["content"], kw["messages"][1]["content"]
        self.prompts.append(sys_ + user)
        if self.bad:
            self.responses = ["I cannot help with that."]
        elif "Planner" in sys_:
            self.responses = [json.dumps(PLAN)]
        elif "channel agent" in sys_:
            acts = [{"type": "rotate_creative", "line_item": "meta_prospecting"},
                    {"type": "pause", "line_item": "google_brand"}, {"type": "bogus"}]
            self.responses = ["```json\n" + json.dumps({"actions": acts}) + "\n```"]
        elif "Before the campaign starts" in sys_:
            self.responses = [json.dumps({"criteria": ["use holdouts"]})]
        else:
            self.responses = [json.dumps({"verdict": "pass", "summary": "ok", "evidence": []})]
        return super().create(**kw)


@pytest.fixture
def playbook(tmp_path):
    p = tmp_path / "v1.md"
    p.write_text("# Playbook\n1. test it\n")
    return str(p)


def _sim():
    return Simulator(generate_campaigns(1, "train", seed=0)[0], seed=0)


def test_run_completes_and_records_trace(playbook):
    sim, client = _sim(), Scripted()
    runner.run(playbook, sim, client=client)
    assert sim.done
    kinds = [e["kind"] for e in sim.trace]
    assert kinds.count("model_call") == 2 + 2 * 3 + 1  # done-def, planner, 2 channels x 3 checkpoints, verdict
    assert kinds.count("rejected") == 3 * 4  # per checkpoint: each agent rejects the bogus action and the other channel's item
    assert {e["payload"]["role"] for e in sim.trace if e["kind"] == "model_call"} == {"planner", "channel", "verifier"}
    plan = next(e for e in sim.trace if e["kind"] == "plan")
    assert plan["ok"] and plan["payload"]["holdouts"] == {"meta_retargeting": 0.2}
    assert 0 < score(sim) <= 1


def test_no_hidden_truth_in_prompts(playbook):
    sim, client = _sim(), Scripted()
    runner.run(playbook, sim, client=client)
    blob = "\n".join(client.prompts)
    for t in sim.campaign.truth.values():
        for key in ("r", "A_max", "attr_cpa", "decay"):
            assert repr(t[key]) not in blob
    assert "debrief" not in blob and "true_incrementality" not in blob


def test_garbage_model_falls_back_to_equal_split(playbook):
    sim = _sim()
    runner.run(playbook, sim, client=Scripted(bad=True))
    ref = _sim()
    equal_split(ref)
    assert score(sim) == pytest.approx(score(ref))
    calls = [e for e in sim.trace if e["kind"] == "model_call"]
    assert calls and all(e["payload"]["fallback_used"] for e in calls)


def test_resume_from_saved_state_matches_in_process_run(playbook, tmp_path):
    ref = _sim()
    runner.run(playbook, ref, client=Scripted())
    db = str(tmp_path / "r.db")
    sim = _sim()
    backend = SimulatorBackend.wrap(sim)
    with Store(db) as st:
        rid = st.create_run("sim", 2, playbook)
    state = runner.new_state()
    while state["stage"] != "finished":  # each call: fresh Store + state loaded from the database only
        with Store(db) as st:
            state = st.load_run_state(rid)["state"] or runner.new_state()
            state = runner.run_checkpoint(playbook, backend, state, client=Scripted(), store=st, run_id=rid)
    assert score(sim) == pytest.approx(score(ref))
    with Store(db) as st:
        s = st.load_run_state(rid)
        assert s["status"] == "done" and s["state"]["stage"] == "finished" and s["state"]["verdict"]["verdict"] == "pass"
        assert s["last_checkpoint"] == 4
        trace = st.get_trace(rid)
        assert [e["event_id"] for e in trace] == sorted(e["event_id"] for e in trace)
        assert {e["role"] for e in trace if e["kind"] == "model_call"} == {"planner", "channel", "verifier"}
    reset_db(db)


def test_awaiting_data_touches_nothing(playbook):
    class Waiting(SimulatorBackend):
        @property
        def status(self):
            return "awaiting_data"
    b = Waiting.wrap(_sim())
    state = runner.new_state()
    assert runner.run_checkpoint(playbook, b, state, client=Scripted()) == state
    assert b.sim.trace == []


def test_gate_plugin_signature(playbook, tmp_path, monkeypatch):
    import asyncio
    from sim.gate import evaluate_version
    monkeypatch.setattr(runner, "_client", Scripted())
    db = str(tmp_path / "g.db")
    res = asyncio.run(evaluate_version(runner.run, playbook, "train", n=1, seeds=(0,), store_path=db))
    (row,) = res.values()
    assert row["error"] is None
    with Store(db) as st:
        n = st.conn.execute("SELECT COUNT(*) FROM model_calls WHERE run_id=?", (row["run_id"],)).fetchone()[0]
        assert n == 9
    reset_db(db)
