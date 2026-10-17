"""Run store tests: resume in a fresh process, mocked live run, report from DB alone, hidden-truth isolation."""
import ast
import json
import pathlib
import subprocess
import sys

import pytest

from sim import LINE_ITEMS, Simulator, generate_campaigns, score
from store import Store, reset_db, truth
from store.sim_io import persist_sim

ROOT = pathlib.Path(__file__).resolve().parent.parent


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "t.db")
    yield path
    reset_db(path)


def _played_sim(split="train"):
    sim = Simulator(generate_campaigns(1, split, seed=0)[0], seed=0)
    sim.apply_plan({"allocations": {li: 1 for li in LINE_ITEMS}})
    sim.step()
    sim.act({"type": "pause", "line_item": "google_brand"})
    while not sim.done:
        sim.step()
    return sim


def test_simulated_run_rows(db):
    sim = _played_sim()
    with Store(db) as st:
        rid = persist_sim(st, sim, split="train", score=score(sim), playbook_version="v1", with_debrief=True)
        assert len(st.conn.execute("SELECT * FROM checkpoints WHERE run_id=?", (rid,)).fetchall()) == 4
        trace = st.get_trace(rid)
        assert [e["kind"] for e in trace] == ["action", "action"]
        assert [e["event_id"] for e in trace] == sorted(e["event_id"] for e in trace)
        assert trace[0]["action"]["_kind"] == "plan" and trace[1]["action"]["type"] == "pause"
        assert st.get_score(rid, "train") == pytest.approx(score(sim))
        assert truth.train_debrief(st, rid)["line_items"]


def test_test_split_debrief_never_stored(db):
    sim = _played_sim("test")
    with Store(db) as st:
        rid = persist_sim(st, sim, split="test", score=score(sim), with_debrief=True)
        assert truth.train_debrief(st, rid) is None
        assert st.conn.execute("SELECT debrief_json FROM scores WHERE run_id=?", (rid,)).fetchone()[0] is None


def test_mocked_live_run_resumes_in_fresh_process(db):
    with Store(db) as st:
        rid = st.create_run("live", 2, "v1", {"interval_days": 7}, max_total_spend=5000, due_at="2026-02-01T00:00:00")
        st.record_model_call({"run_id": rid, "role": "planner", "model": "m", "temperature": 0, "prompt": [{"role": "user"}],
                              "raw_response": "{}", "parsed_json": {"a": 1}, "error": None, "fallback_used": False})
        st.record_checkpoint(rid, 1, {"x": 1}, raw={"meta": "raw"}, period={"start": "a", "end": "b"}, provisional=True)
        st.record_checkpoint(rid, 1, {"x": 2}, read_no=2, period={"start": "a", "end": "b"})  # re-read keeps both
        e1 = st.record_action(rid, 1, {"type": "pause", "line_item": "meta_retargeting"}, False, "unsupported", read_no=2)
        e2 = st.record_model_call({"run_id": rid, "role": "channel", "checkpoint_idx": 1, "read_no": 2})
        assert e2 == e1 + 1
        st.save_creative(rid, "meta_prospecting", "c1", {"headline": "h"})
        st.set_creative_status(rid, "meta_prospecting", "c1", "approved")
        st.record_external_id(rid, "campaign", "123")
        st.save_state(rid, {"plan_applied": True, "next_checkpoint": 2}, due_at="2026-02-08T00:00:00")
    code = ("import json,sys\nfrom store import Store\n"
            "st = Store(sys.argv[1]); s = st.load_run_state(sys.argv[2])\n"
            "s['approved'] = [c['creative_id'] for c in st.list_creatives(sys.argv[2], 'approved')]\n"
            "s['ids'] = st.external_ids(sys.argv[2])\nprint(json.dumps(s))")
    out = subprocess.run([sys.executable, "-c", code, db, rid], capture_output=True, text=True, cwd=ROOT, check=True)
    s = json.loads(out.stdout)
    assert s["state"] == {"plan_applied": True, "next_checkpoint": 2} and s["due_at"] == "2026-02-08T00:00:00"
    assert s["last_checkpoint"] == 1 and s["last_read_no"] == 2
    assert s["approved"] == ["c1"] and s["ids"][0]["meta_id"] == "123"
    with Store(db) as st:
        assert st.get_checkpoint(rid, 1, 1)["provisional"] and st.get_checkpoint(rid, 1, 1)["observation"] == {"x": 1}


def test_report_builds_from_db_alone(db, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with Store(db) as st:
        st.start_session("s")
        st.record_improvement("s", {"round": 0, "event": "baseline", "version": "v1", "val_mean": 0.7})
        st.finish_session("s", {"final_version": "v1"})
    from report.build_page import build
    out = build("s", db_path=db)
    assert "baseline" in pathlib.Path(out).read_text()


def test_agent_code_cannot_reach_debriefs():
    for d in ("agents", "backends"):
        for f in (ROOT / d).rglob("*.py"):
            src = f.read_text()
            assert "debrief_json" not in src, f
            for node in ast.walk(ast.parse(src)):
                mods = []
                if isinstance(node, ast.Import):
                    mods = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    mods = [node.module or ""] + [f"{node.module}.{a.name}" for a in node.names]
                assert not any(m.startswith("store.truth") or m == "store.sim_io" for m in mods), (f, mods)


def test_agent_facing_store_functions_never_return_debriefs(db):
    sim = _played_sim()
    with Store(db) as st:
        rid = persist_sim(st, sim, split="train", score=score(sim), with_debrief=True)
        blob = json.dumps([st.load_run_state(rid), st.get_trace(rid), st.list_runs(),
                           st.get_checkpoint(rid, 1), st.list_creatives(rid)], default=str)
        assert "true_incrementality" not in blob and "debrief" not in blob
