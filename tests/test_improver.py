import json

import pytest

from agents import improver
from agents.models import FakeClient

PB = "# Playbook v1\n\n1. Trust last-click CPA.\n"
NEW = PB + "2. Holdout-test cheap items before scaling.\n"


class Scripted(FakeClient):
    def __init__(self, edit=None, fail_edit=False):
        super().__init__(["{}"])
        self.edit, self.fail_edit, self.roles = edit if edit is not None else NEW, fail_edit, []

    def create(self, **kw):
        sys_ = kw["messages"][0]["content"]
        if "Compress" in sys_:
            self.roles.append("summarizer")
            self.responses = ["short summary"]
        elif "Diagnose the most costly failure pattern" in sys_:
            self.roles.append("diagnose")
            self.responses = [json.dumps({"diagnosis": "retargeting over-credited", "research_question": "q?"})]
        else:
            self.roles.append("edit")
            self.responses = ["nope" if self.fail_edit else json.dumps({"new_playbook": self.edit, "summary": "add holdout rule", "rationale": "r"})]
        return super().create(**kw)


def fake_research(question):
    fake_research.n += 1
    return {"answer": "A", "sources": [{"url": "https://x.org/a", "title": "A"}]}


fake_research.n = 0


def test_propose_edit_passes_sources_through_and_researches_once():
    fake_research.n = 0
    out = improver.propose_edit(PB, [{"score": 0.5}], [], client=Scripted(), research_fn=fake_research)
    assert out["new_playbook"] == NEW and out["sources"] == ["https://x.org/a"] and fake_research.n == 1
    assert [c["role"] for c in out["improver_calls"]] == ["improver", "improver"]


def test_large_context_uses_summarizer():
    c = Scripted()
    improver.propose_edit(PB, [{"x": "y" * 20000}], [], client=c, research_fn=fake_research)
    assert c.roles[0] == "summarizer"


def test_malformed_edit_raises_and_identical_passes_to_loop(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    with pytest.raises(RuntimeError):
        improver.propose_edit(PB, [], [], client=Scripted(fail_edit=True), research_fn=fake_research)
    same = improver.propose_edit(PB, [], [], client=Scripted(edit=PB), research_fn=fake_research)
    assert same["new_playbook"] == PB  # improve.loop records identical/empty edits as 'empty_edit'


THREE = [PB + "2. Hold budget moves until a holdout read exists.\n",
         PB + "2. Shift only a quarter of the worst item's budget per checkpoint.\n",
         PB + "2. Cap any single item at 50% of the period budget.\n"]


class Multi(Scripted):
    def __init__(self, replies, questions=("q1?", "q2?")):
        super().__init__()
        self.replies, self.questions = list(replies), questions

    def create(self, **kw):
        sys_ = kw["messages"][0]["content"]
        if "Diagnose the most costly failure pattern" in sys_:
            self.roles.append("diagnose")
            self.responses = [json.dumps({"diagnosis": "d", "target_rule": "2", "research_questions": list(self.questions)})]
            return FakeClient.create(self, **kw)
        if "THREE alternative" in sys_:
            self.roles.append("edit")
            reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
            self.responses = [json.dumps({"candidates": [{"new_playbook": t, "summary": f"s{i}", "rationale": "r"}
                                                         for i, t in enumerate(reply)]})]
            return FakeClient.create(self, **kw)
        return super().create(**kw)


def test_three_candidates_and_two_research_calls():
    fake_research.n = 0
    c = Multi([THREE])
    out = improver.propose_edit(PB, [{"score": 0.5}], [], client=c, research_fn=fake_research)
    assert fake_research.n == 2 and out["sources"] == ["https://x.org/a"]
    assert out["new_playbook"] == THREE[0] and [a["new_playbook"] for a in out["alternatives"]] == THREE[1:]
    assert out["alternatives"][0]["sources"] == out["sources"]


def test_near_duplicate_candidates_collapse_and_rejected_repeats_are_dropped():
    dup = PB + "2. Hold budget moves until a holdout read exists!\n"
    out = improver.propose_edit(PB, [], [], client=Multi([[THREE[0], dup, THREE[1]]]), research_fn=fake_research)
    assert [out["new_playbook"]] + [a["new_playbook"] for a in out["alternatives"]] == [THREE[0], THREE[1]]
    hist = [{"round": 1, "summary": "x", "accepted": False, "diff": "+2. Hold budget moves until a holdout read exists.\n"}]
    out = improver.propose_edit(PB, [], hist, client=Multi([THREE]), research_fn=fake_research)
    assert out["new_playbook"] == THREE[1] and len(out["alternatives"]) == 1


def test_all_repeats_retry_once_then_raise():
    hist = [{"round": 1, "summary": "x", "accepted": False, "diff": "+2. Hold budget moves until a holdout read exists.\n"}]
    c = Multi([[THREE[0]]])
    with pytest.raises(RuntimeError, match="kept repeating"):
        improver.propose_edit(PB, [], hist, client=c, research_fn=fake_research)
    assert c.roles.count("edit") == 2
    c = Multi([[THREE[0]], [THREE[2]]])  # the retry comes back different
    assert improver.propose_edit(PB, [], hist, client=c, research_fn=fake_research)["new_playbook"] == THREE[2]


def test_explore_draws_a_seeded_direction_skips_diagnosis_and_samples_hot():
    fake_research.n = 0
    seen = []

    class Hot(Multi):
        def create(self, **kw):
            seen.append(kw.get("temperature"))
            return super().create(**kw)

    def run(seed):
        c = Hot([THREE])
        out = improver.propose_edit(PB, [], [], client=c, research_fn=fake_research, explore={"seed": seed})
        return out, c

    out, c = run("s:3")
    assert "diagnose" not in c.roles and fake_research.n == 1 and seen == [improver.EXPLORE_TEMPERATURE]
    assert out["explore"]["direction"] in improver.DIRECTIONS and out["diagnosis"] is None
    assert run("s:3")[0]["explore"] == out["explore"]  # the same seed draws the same direction
    assert {run(f"s:{i}")[0]["explore"]["direction"] for i in range(30)} > {out["explore"]["direction"]}
