import asyncio

import pytest

from improve import loop, stubs
from store import Store


def three_candidates(playbook, failures, history):
    """The stub improver's next three untried rules as one proposal with two alternatives."""
    tried, out = [dict(h) for h in history], []
    for _ in range(3):
        p = stubs.propose_edit(playbook, failures, tried)
        if p is None:
            break
        out.append(p)
        tried.append({"summary": p["summary"]})
    return {**out[0], "alternatives": out[1:]} if out else None


@pytest.fixture
def start(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "v1.md").write_text(stubs.STUB_V1)
    return "v1.md"


def run(start, db, rounds, **kw):
    return asyncio.run(loop.improve(stubs.runner, three_candidates, start, "multi", rounds, store_path=db, **kw))


def test_each_candidate_is_gated_and_only_the_best_is_accepted(start, tmp_path):
    db = str(tmp_path / "t.db")
    summary = run(start, db, 1)
    with Store(db) as s:
        gates = [e for e in s.session_data("multi")["changelog"] if e["event"] == "gate"]
    assert len(gates) == 3 and {e["candidate"].split("/")[-1] for e in gates} == {"round1_1.md", "round1_2.md", "round1_3.md"}
    accepted = [e for e in gates if e["accepted"]]
    assert len(accepted) == 1 and len(summary["versions"]) == 2  # v1 plus the one winner
    best = max(gates, key=lambda e: e["mean_diff"])
    assert accepted[0] is not None and accepted[0]["candidate"] == best["candidate"]
    for e in gates:  # a passing candidate that lost says so
        if e is not accepted[0] and e["mean_diff"] > 0.01:
            assert "was better" in e["reason"]
    assert all(h["diff"] for h in summary["history"])


def test_resume_after_a_multi_candidate_round_restores_state(start, tmp_path):
    db = str(tmp_path / "t.db")
    first = run(start, db, 1)
    second = run(start, db, 2, resume=True)
    for a, b in zip(first["versions"], second["versions"]):  # the log stores val_mean rounded to 4 places
        assert (a["version"], a["round"]) == (b["version"], b["round"]) and a["val_mean"] == pytest.approx(b["val_mean"], abs=1e-3)
    assert len(second["history"]) > len(first["history"])  # round 2 gated the one rule that was left
    assert second["history"][: len(first["history"])] == first["history"]


def stuck_then_good(playbook, failures, history, explore=None):
    """Proposes the harmful stub rule until asked to explore; an explore round proposes the useful holdout rule."""
    tried = [{"summary": s} for s in ("Rotate creative weekly", "Holdout-test cheap last-click items", "Start prospecting-heavy")]
    if explore:
        p = stubs.propose_edit(playbook, failures, [t for t in tried if t["summary"] != "Rotate creative weekly"])
        return {**p, "explore": {"seed": explore["seed"], "direction": "d"}}
    return stubs.propose_edit(playbook, failures, tried)  # "Scale winners harder": rejected by the gate


def test_stagnation_triggers_explore_and_the_winner_is_rechecked(start, tmp_path):
    db = str(tmp_path / "t.db")
    asyncio.run(loop.improve(stubs.runner, stuck_then_good, start, "x", 3, store_path=db))
    with Store(db) as s:
        gates = [e for e in s.session_data("x")["changelog"] if e["event"] == "gate"]
    assert [bool(e.get("explore")) for e in gates] == [False, False, True]
    assert gates[2]["explore"] == {"seed": "x:3", "direction": "d", "stagnant_rounds": 2}
    assert gates[2]["accepted"] and gates[2]["recheck"]["accepted"]  # passed again on fresh seeds
    assert not gates[0]["accepted"] and "recheck" not in gates[0]


def test_an_explore_winner_that_fails_the_recheck_is_not_accepted(start, tmp_path, monkeypatch):
    real = loop.run_gate

    async def fake(runner, base, cand, **kw):
        out = await real(runner, base, cand, **kw)
        if kw.get("seeds"):  # the fresh-seed recheck disagrees
            out["accepted"], out["reason"] = False, "no improvement on fresh seeds"
        return out

    monkeypatch.setattr(loop, "run_gate", fake)
    db = str(tmp_path / "t.db")
    summary = asyncio.run(loop.improve(stubs.runner, stuck_then_good, start, "y", 3, store_path=db))
    with Store(db) as s:
        last = [e for e in s.session_data("y")["changelog"] if e["event"] == "gate"][-1]
    assert not last["accepted"] and "failed the fresh-seed recheck" in last["reason"]
    assert len(summary["versions"]) == 1
