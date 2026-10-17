import pytest

from backends import CampaignBackend, SimulatorBackend
from sim import LINE_ITEMS, Simulator, generate_campaigns, score
from sim.baselines import naive_last_click, smart_holdout


def naive_backend(b):
    b.apply_plan({"allocations": {li: 1 for li in LINE_ITEMS}})
    while not b.done:
        b.advance()
        obs = b.observe()
        last = obs["history"][-1]["line_items"]
        cpas = {li: m["reported"]["cpa"] for li, m in last.items() if m["reported"]["cpa"]}
        if len(cpas) >= 2 and not b.done:
            best, worst = min(cpas, key=cpas.get), max(cpas, key=cpas.get)
            b.act({"type": "shift_budget", "from": worst, "to": best, "amount": 0.5 * obs["budgets"][worst]})


def smart_backend(b):
    suspects = ["meta_retargeting", "google_brand"]
    b.apply_plan({
        "allocations": {"meta_prospecting": 0.45, "meta_retargeting": 0.15, "google_nonbrand": 0.25, "google_brand": 0.15},
        "holdouts": {li: 0.2 for li in suspects},
    })
    while not b.done:
        b.advance()
        if b.done:
            break
        obs = b.observe()
        for li in LINE_ITEMS:
            b.act({"type": "rotate_creative", "line_item": li})
        if obs["checkpoint"] == 2:
            last = obs["history"][-1]["line_items"]
            pros = last["meta_prospecting"]["reported"]["cpa"] or 1e9
            for li in suspects:
                share = last[li]["measured"]["holdout"]["incremental_share_est"]
                b.act({"type": "set_holdout", "line_item": li, "share": 0})
                if (last[li]["reported"]["cpa"] or 1e9) / max(share, 0.01) > 1.5 * pros:
                    b.act({"type": "shift_budget", "from": li, "to": "meta_prospecting", "amount": 0.8 * obs["budgets"][li]})


@pytest.mark.parametrize("direct,via", [(naive_last_click, naive_backend), (smart_holdout, smart_backend)])
def test_baseline_score_matches_direct(direct, via):
    for c in generate_campaigns(2, "train", seed=0):
        for seed in (0, 1):
            sim = Simulator(c, seed=seed)
            direct(sim)
            b = SimulatorBackend(c, seed=seed)
            via(b)
            assert b.done and b.status == "done"
            # obs["budgets"] is rounded to cents (direct baseline reads exact sim.budgets), so allow ~1e-6
            assert score(b.sim) == pytest.approx(score(sim), abs=1e-6)


def test_advance_due_at_none():
    b = SimulatorBackend(generate_campaigns(1, "train", seed=0)[0])
    assert b.status == "ready"
    assert b.advance() == {"due_at": None}


def test_plan_with_creatives():
    c = generate_campaigns(1, "train", seed=0)[0]
    b = SimulatorBackend(c)
    plan = {"allocations": {li: 1 for li in LINE_ITEMS},
            "creatives": {"meta_prospecting": [{"id": "mp-a", "headline": "h", "body": "b", "cta": "Shop"},
                                               {"id": "mp-b", "headline": "h2", "body": "b2", "cta": "Shop"}]}}
    assert b.apply_plan(plan) == {"ok": True, "error": None}
    reg = {x["id"]: x for x in b.observe()["creatives"]}
    assert reg["mp-a"]["status"] == "live" and reg["mp-b"]["status"] == "approved"
    b.advance()
    assert b.act({"type": "rotate_creative", "line_item": "meta_prospecting"})["ok"]


def test_protocol_and_wrap():
    c = generate_campaigns(1, "train", seed=0)[0]
    b = SimulatorBackend(c)
    assert isinstance(b, CampaignBackend)
    sim = Simulator(c)
    assert SimulatorBackend.wrap(sim).sim is sim
