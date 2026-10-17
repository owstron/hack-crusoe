import pytest

from agents import schemas
from sim import LINE_ITEMS, Simulator, generate_campaigns


@pytest.fixture
def campaign():
    return Simulator(generate_campaigns(1, "train", seed=0)[0]).observe()["campaign"]


def test_plan_ok_and_rejections(campaign):
    plan = schemas.validate_plan({"allocations": {"meta_prospecting": "0.5", "google_brand": 1},
                                  "holdouts": {"meta_retargeting": 0.2}, "hypotheses": {"meta_retargeting": "x"}}, campaign)
    assert plan["allocations"]["meta_prospecting"] == 0.5 and plan["holdouts"] == {"meta_retargeting": 0.2}
    for bad in ({}, [], {"allocations": {"tiktok": 1}}, {"allocations": {"google_brand": 0}},
                {"allocations": {"google_brand": 1}, "holdouts": {"google_brand": 0.9}},
                {"allocations": {"google_brand": "abc"}}, {"allocations": {"google_brand": -1}}):
        with pytest.raises(ValueError):
            schemas.validate_plan(bad, campaign)


def test_meta_only_capabilities_reject_google_and_holdouts(campaign):
    campaign["capabilities"] = {"line_items": {li: {"channel": "meta"} for li in LINE_ITEMS[:2]},
                                "holdouts": False, "attribution": {}}
    with pytest.raises(ValueError):
        schemas.validate_plan({"allocations": {"google_brand": 1}}, campaign)
    with pytest.raises(ValueError):
        schemas.validate_plan({"allocations": {"meta_prospecting": 1}, "holdouts": {"meta_prospecting": 0.2}}, campaign)
    valid, rejected = schemas.parse_actions([{"type": "set_holdout", "line_item": "meta_prospecting", "share": 0.2}], campaign)
    assert not valid and rejected


def test_parse_actions_filters_and_restricts(campaign):
    meta = ["meta_prospecting", "meta_retargeting"]
    raw = {"actions": [
        {"type": "shift_budget", "from": "meta_retargeting", "to": "google_brand", "amount": "100", "junk": 1},
        {"type": "shift_budget", "from": "google_brand", "to": "meta_prospecting", "amount": 5},  # not meta's
        {"type": "pause", "line_item": "nope"}, {"type": "launch_rocket"}, "text", {"type": "shift_budget"},
        {"type": "set_holdout", "line_item": "meta_retargeting", "share": 0.9},
        {"type": "rotate_creative", "line_item": "meta_prospecting"},
    ]}
    valid, rejected = schemas.parse_actions(raw, campaign, meta)
    assert valid == [{"type": "shift_budget", "from": "meta_retargeting", "to": "google_brand", "amount": 100.0},
                     {"type": "rotate_creative", "line_item": "meta_prospecting"}]
    assert len(rejected) == 6
    with pytest.raises(ValueError):
        schemas.parse_actions("not a list", campaign)


def test_verifier_schemas():
    assert schemas.validate_done_definition({"criteria": ["a"]})["criteria"] == ["a"]
    with pytest.raises(ValueError):
        schemas.validate_done_definition({"criteria": []})
    assert schemas.validate_verdict({"verdict": "pass", "evidence": ["x"]})["verdict"] == "pass"
    with pytest.raises(ValueError):
        schemas.validate_verdict({"verdict": "maybe"})
