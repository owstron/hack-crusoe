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
        elif "ask ONE web research question" in sys_:
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
