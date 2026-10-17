"""Schema v2 contract tests. Run: .venv/bin/python -m pytest tests (or python tests/test_schema_v2.py)."""
import copy
import pathlib
import re

from sim import LINE_ITEMS, SCHEMA_VERSION, Simulator, generate_campaigns, score
from sim.baselines import STRATEGIES

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _sim(seed=0):
    return Simulator(generate_campaigns(1, "train", seed=0)[0], seed=seed)


def test_observation_shape():
    sim = _sim()
    sim.apply_plan({"allocations": {li: 1 for li in LINE_ITEMS}})
    assert sim.status == "ready"
    assert sim.step() == {"due_at": None}
    obs = sim.observe()
    c = obs["campaign"]
    assert c["schema_version"] == SCHEMA_VERSION == 2
    assert {"period_budget", "n_checkpoints", "max_total_spend", "max_period_spend", "capabilities"} <= set(c)
    assert c["capabilities"]["line_items"]["google_brand"] == {"channel": "google"}
    assert obs["checkpoint"] == 1 and obs["checkpoints_left"] == 3 and obs["status"] == "ready"
    h = obs["history"][0]
    assert h["read_no"] == 1 and h["status"] == "final" and h["delivery_ok"] is True
    assert set(h["period"]) == {"start", "end"}
    row = h["line_items"]["meta_prospecting"]
    assert {"attribution", "attributed_conversions", "cpa", "purchase_value", "roas"} == set(row["reported"])
    assert row["measured"] == {"holdout": None}
    assert "reach" not in row["unavailable"] and "search_impression_share" in row["unavailable"]
    assert "reach" in h["line_items"]["google_brand"]["unavailable"]
    assert row["creative_age_checkpoints"] == 0


def test_done_status():
    sim = _sim()
    sim.apply_plan({"allocations": {li: 1 for li in LINE_ITEMS}})
    while not sim.done:
        sim.step()
    assert sim.status == "done" and sim.observe()["status"] == "done"


def test_capabilities_reject_google_and_holdouts():
    sim = _sim()
    sim._public["capabilities"] = {"line_items": {li: {"channel": "meta"} for li in LINE_ITEMS[:2]},
                                   "holdouts": False, "attribution": {}}
    assert not sim.apply_plan({"allocations": {"google_brand": 1}})["ok"]
    assert not sim.apply_plan({"allocations": {"meta_prospecting": 1}, "holdouts": {"meta_prospecting": 0.2}})["ok"]
    assert sim.apply_plan({"allocations": {"meta_prospecting": 1, "meta_retargeting": 1}})["ok"]
    assert not sim.act({"type": "set_holdout", "line_item": "meta_retargeting", "share": 0.2})["ok"]
    assert not sim.act({"type": "pause", "line_item": "google_brand"})["ok"]


def test_spend_caps():
    c = generate_campaigns(1, "train", seed=0)[0]
    c.max_period_spend = c.period_budget * 0.5
    sim = Simulator(c)
    assert not sim.apply_plan({"allocations": {li: 1 for li in LINE_ITEMS}})["ok"]
    c2 = generate_campaigns(1, "train", seed=0)[0]
    c2.max_total_spend = c2.period_budget * 1.5  # second period would break the lifetime cap
    sim = Simulator(c2)
    assert sim.apply_plan({"allocations": {li: 1 for li in LINE_ITEMS}})["ok"]
    sim.step()
    assert not sim.act({"type": "shift_budget", "from": "google_brand", "to": "meta_prospecting", "amount": 1})["ok"]
    assert not sim.act({"type": "pause", "line_item": "google_brand"})["ok"]


def test_rotate_creative_registry():
    sim = _sim()
    plan = {"allocations": {li: 1 for li in LINE_ITEMS},
            "creatives": {"meta_prospecting": [{"id": "a", "headline": "h", "body": "b", "cta": "Shop"},
                                               {"id": "b", "headline": "h2", "body": "b2", "cta": "Shop"}]}}
    assert sim.apply_plan(plan)["ok"]
    sim.step()
    assert sim.observe()["history"][0]["line_items"]["meta_prospecting"]["creative_age_checkpoints"] == 0
    sim.step()
    assert sim.age["meta_prospecting"] == 2
    assert sim.act({"type": "rotate_creative", "line_item": "meta_prospecting"})["ok"]
    live = {c["id"]: c["status"] for c in sim.observe()["creatives"] if c["line_item"] == "meta_prospecting"}
    assert live == {"a": "retired", "b": "live"} and sim.age["meta_prospecting"] == 0
    before = copy.deepcopy(sim.observe()["creatives"])
    res = sim.act({"type": "rotate_creative", "line_item": "meta_prospecting"})  # nothing approved left
    assert not res["ok"] and sim.observe()["creatives"] == before
    # a line item with no plan creatives and an explicit registry elsewhere has none to rotate to either
    assert not sim.act({"type": "rotate_creative", "line_item": "meta_retargeting"})["ok"]


def test_implicit_creative_pool_matches_v1_behaviour():
    sim = _sim()
    sim.apply_plan({"allocations": {li: 1 for li in LINE_ITEMS}})
    sim.step()
    assert all(sim.act({"type": "rotate_creative", "line_item": li})["ok"] for li in LINE_ITEMS)


def test_baseline_scores_unchanged_from_v1():
    # recorded from the v1 simulator before the migration (sim.baselines evaluate, validation split)
    from sim.baselines import evaluate
    assert round(evaluate(STRATEGIES["naive_last_click"], "validation"), 3) == 0.740
    assert round(evaluate(STRATEGIES["smart_holdout"], "validation"), 3) == 0.917


def test_no_old_contract_fields_in_code():
    pat = re.compile(r"weeks_left|n_weeks|weekly_budget|CHANNEL_OF|creative_age_weeks")
    skip = {"test_schema_v2.py"}
    for d in ("sim", "improve", "report", "agents", "tests", "backends", "store"):
        for f in (ROOT / d).rglob("*.py"):
            if f.name not in skip:
                assert not pat.search(f.read_text()), f


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
