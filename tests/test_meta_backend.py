import datetime as dt
import json

import pytest

import agents.runner as runner
from agents.models import FakeClient
from backends import CampaignBackend, MetaAPI, MetaBackend, MetaError
from store import Store

START = dt.datetime(2026, 3, 2)
CR = {"id": "c1", "headline": "Stop scrolling", "body": "Real results.", "cta": "SHOP_NOW"}
PLAN = {"allocations": {"meta_prospecting": 0.6, "meta_retargeting": 0.4}, "creatives": {"meta_prospecting": [CR]}}
# thresholds off so the dry-run data is final unless a test turns them on
CFG = {"product": "mugs", "total_budget": 400.0, "interval_days": 7, "read_delay_days": 7, "attribution_window_days": 7,
       "start_date": "2026-03-02", "min_impressions": 0, "min_conversions": 0}


class Clock:
    def __init__(self, now=START):
        self.now = now

    def __call__(self):
        return self.now

    def to(self, iso):
        self.now = dt.datetime.fromisoformat(iso)


@pytest.fixture
def store(tmp_path):
    with Store(str(tmp_path / "t.db")) as s:
        yield s


def make(store, cfg=None, api=None, clock=None):
    clock = clock or Clock()
    api = api or MetaAPI(account_id="act_1", page_id="p1", dry_run=True)
    return MetaBackend.create(store, api, {**CFG, **(cfg or {})}, clock=clock), api, clock


def test_protocol_and_initial_state(store):
    b, _, _ = make(store)
    assert isinstance(b, CampaignBackend)
    assert b.status == "ready" and not b.done
    obs = b.observe()
    assert obs["campaign"]["capabilities"]["holdouts"] is False
    assert set(obs["campaign"]["line_items"]) == {"meta_prospecting", "meta_retargeting"}
    assert obs["history"] == [] and obs["checkpoint"] == 0


def test_dry_run_apply_plan_creates_paused_objects_only_and_no_network(store):
    b, api, _ = make(store)
    assert b.apply_plan(PLAN) == {"ok": True, "error": None}
    assert api._session is None  # never touched the network layer
    assert all(w["data"].get("status", "PAUSED") == "PAUSED" for w in api.writes)
    kinds = [w["path"].rsplit("/", 1)[-1] for w in api.writes]
    assert kinds == ["campaigns", "adsets", "adsets"]  # the creative is pending: no ad yet
    ids = store.external_ids(b.run_id)
    assert {(i["kind"], i["line_item"]) for i in ids} == {("campaign", ""), ("adset", "meta_prospecting"), ("adset", "meta_retargeting")}
    assert [c["status"] for c in store.list_creatives(b.run_id)] == ["pending"]
    assert b.apply_plan(PLAN)["ok"] is False  # not twice


def test_unapproved_creatives_never_reach_meta(store):
    b, api, _ = make(store)
    b.apply_plan(PLAN)
    n = len(api.writes)
    assert b.sync_creatives()["ok"] and len(api.writes) == n
    store.set_creative_status(b.run_id, "meta_prospecting", "c1", "approved")
    assert b.sync_creatives()["ok"]
    new = [w["path"].rsplit("/", 1)[-1] for w in api.writes[n:]]
    assert new == ["adcreatives", "ads"] and api.writes[-1]["data"]["status"] == "PAUSED"
    assert store.list_creatives(b.run_id)[0]["status"] == "live"
    assert b.sync_creatives()["ok"] and len(api.writes) == n + 2  # idempotent


def test_rotate_without_approved_creative_changes_nothing(store):
    b, api, _ = make(store)
    b.apply_plan(PLAN)
    b.advance()
    n = len(api.writes)
    r = b.act({"type": "rotate_creative", "line_item": "meta_prospecting"})
    assert r["ok"] is False and "approved" in r["error"] and len(api.writes) == n


def test_rotate_retires_the_live_ad_and_attaches_the_approved_one(store):
    b, api, _ = make(store)
    b.apply_plan({**PLAN, "creatives": {"meta_prospecting": [CR, {**CR, "id": "c2", "headline": "h2"}]}})
    store.set_creative_status(b.run_id, "meta_prospecting", "c1", "approved")
    b.sync_creatives()
    b.advance()
    store.set_creative_status(b.run_id, "meta_prospecting", "c2", "approved")
    assert b.act({"type": "rotate_creative", "line_item": "meta_prospecting"})["ok"]
    assert {c["creative_id"]: c["status"] for c in store.list_creatives(b.run_id)} == {"c1": "retired", "c2": "live"}
    assert any(w["data"] == {"status": "PAUSED"} for w in api.writes)  # the old ad was paused
    reg = {c["id"]: c for c in b.observe()["creatives"]}
    assert reg["c2"]["age_checkpoints"] == 0


def test_google_and_holdout_rejected(store):
    b, _, _ = make(store)
    assert b.apply_plan({"allocations": {"google_brand": 1}})["ok"] is False
    assert b.apply_plan({"allocations": {"meta_prospecting": 1}, "holdouts": {"meta_prospecting": 0.2}})["ok"] is False
    b.apply_plan(PLAN)
    b.advance()
    assert b.act({"type": "pause", "line_item": "google_brand"})["ok"] is False
    assert b.act({"type": "set_holdout", "line_item": "meta_prospecting", "share": 0.2})["ok"] is False


def test_time_gates_observation(store):
    b, _, clock = make(store)
    b.apply_plan(PLAN)
    assert b.advance()["due_at"] == "2026-03-16T00:00:00"  # period end 03-08, +7 day read delay
    assert b.status == "awaiting_data"
    assert b.observe()["history"] == []
    clock.to("2026-03-16T00:00:00")
    assert b.status == "ready"
    obs = b.observe()
    h = obs["history"][0]
    assert h["status"] == "final" and h["read_no"] == 1 and h["period"] == {"start": "2026-03-02", "end": "2026-03-08"}
    assert h["line_items"]["meta_prospecting"]["reported"]["attribution"] == "7d_click_1d_view"
    assert h["line_items"]["meta_retargeting"]["measured"] == {"holdout": None}
    assert "measured.holdout" in h["line_items"]["meta_retargeting"]["unavailable"]
    cp = store.get_checkpoint(b.run_id, 1)
    assert cp["raw"]["rows"] and cp["observation"]["checkpoint"] == 1


def test_provisional_blocks_budget_moves_then_reread_keeps_both_readings(store):
    b, _, clock = make(store, {"read_delay_days": 0})
    b.apply_plan(PLAN)
    b.advance()
    clock.to("2026-03-09T00:00:00")  # period ended, the 7-day window is still open
    obs = b.observe()
    assert obs["history"][-1]["status"] == "provisional"
    move = {"type": "shift_budget", "from": "meta_retargeting", "to": "meta_prospecting", "amount": 5}
    r = b.act(move)
    assert r["ok"] is False and "provisional" in r["error"]
    clock.to("2026-03-16T00:00:00")  # the window for period 1 has closed
    obs = b.observe()
    assert [(h["checkpoint"], h["read_no"], h["status"]) for h in obs["history"]] == [(1, 1, "provisional"), (1, 2, "final")]
    assert store.get_checkpoint(b.run_id, 1, 1)["provisional"] and not store.get_checkpoint(b.run_id, 1, 2)["provisional"]
    assert b.act(move)["ok"]  # final data: budget moves are allowed again


def test_thin_data_is_provisional_even_after_window_closes(store):
    b, _, clock = make(store, {"min_conversions": 10_000})
    b.apply_plan(PLAN)
    b.advance()
    clock.to("2026-03-16T00:00:00")
    assert b.observe()["history"][-1]["status"] == "provisional"
    assert b.act({"type": "pause", "line_item": "meta_retargeting"})["ok"] is False


def test_shift_and_pause_update_budgets_and_meta(store):
    b, api, clock = make(store)
    b.apply_plan(PLAN)
    b.advance()
    clock.to("2026-03-16T00:00:00")
    b.observe()
    n = len(api.writes)
    assert b.act({"type": "shift_budget", "from": "meta_retargeting", "to": "meta_prospecting", "amount": 20})["ok"]
    assert len(api.writes) == n + 2
    assert b.observe()["budgets"] == {"meta_prospecting": 80.0, "meta_retargeting": 20.0}
    assert b.act({"type": "pause", "line_item": "meta_retargeting"})["ok"]
    assert b.observe()["budgets"] == {"meta_prospecting": 100.0, "meta_retargeting": 0.0}
    assert b.act({"type": "pause", "line_item": "meta_prospecting"})["error"].startswith("cannot pause the last")


def test_spend_caps(store):
    b, _, _ = make(store, {"max_period_spend": 50.0})
    r = b.apply_plan(PLAN)
    assert r["ok"] is False and "max_period_spend" in r["error"]
    b, _, clock = make(store, {"max_total_spend": 150.0})
    assert b.apply_plan(PLAN)["ok"]  # 100 <= 150
    b.advance()
    clock.to("2026-03-16T00:00:00")
    b.observe()  # period 1's spend (about 100) is now persisted
    r = b.act({"type": "shift_budget", "from": "meta_retargeting", "to": "meta_prospecting", "amount": 5})
    assert r["ok"] is False and "max_total_spend" in r["error"]


def test_state_survives_a_fresh_process(store):
    b, _, clock = make(store)
    b.apply_plan(PLAN)
    b.advance()
    clock.to("2026-03-16T00:00:00")
    b.observe()
    b2 = MetaBackend.load(store, b.run_id, MetaAPI(account_id="act_1", dry_run=True), clock=clock)
    assert b2.s == b.s and b2.status == b.status == "ready"
    assert b2.observe()["history"] == b.observe()["history"]
    assert len(store.external_ids(b.run_id, "adset")) == 2


def test_failed_apply_plan_resumes_without_duplicates(store):
    class Flaky(MetaAPI):
        calls = 0

        def create_adset(self, *a, **k):
            Flaky.calls += 1
            if Flaky.calls == 1:
                raise MetaError("boom")
            return super().create_adset(*a, **k)

    b, _, _ = make(store, api=Flaky(account_id="act_1", dry_run=True))
    assert b.apply_plan(PLAN)["ok"] is False and not b.s["plan_applied"]
    assert b.apply_plan(PLAN)["ok"] is True
    assert len(store.external_ids(b.run_id, "campaign")) == 1 and len(store.external_ids(b.run_id, "adset")) == 2


def test_activation_guards(store):
    b, api, clock = make(store)
    assert b.activate()["ok"] is False
    b.apply_plan(PLAN)
    b.advance()
    assert "approved creative" in b.activate()["error"]
    store.set_creative_status(b.run_id, "meta_prospecting", "c1", "approved")
    b.sync_creatives()
    n = len(api.writes)
    clock.to("2026-03-03T00:00:00")
    assert b.activate()["ok"]
    assert {w["data"]["status"] for w in api.writes[n:]} == {"ACTIVE"}
    assert b.due_at == "2026-03-17T00:00:00"  # the schedule restarts from the activation date
    assert b.activate()["ok"] is False


def test_dry_run_matches_live_shapes(store):
    class Resp:
        status_code = 200

        def __init__(self, body):
            self.body, self.text = body, json.dumps(body)

        def json(self):
            return self.body

    class Session:  # a canned Graph API
        def __init__(self):
            self.n = 0

        def post(self, url, data=None, timeout=None):
            self.n += 1
            return Resp({"id": f"live_{self.n}", "report_run_id": f"rep_{self.n}"})

        def get(self, url, params=None, timeout=None):
            if url.endswith("/insights"):
                return Resp({"data": [{"adset_id": "live_2", "spend": "50", "impressions": "9000", "clicks": "100",
                                       "ctr": "1.1", "actions": [{"action_type": "purchase", "value": "3"}],
                                       "action_values": [{"action_type": "purchase", "value": "150"}]}]})
            return Resp({"async_status": "Job Completed"})

    def run(api):
        b, _, clock = make(store, api=api)
        b.apply_plan(PLAN)
        b.advance()
        clock.to("2026-03-16T00:00:00")
        return b.observe()

    live = run(MetaAPI(token="t", account_id="act_1", page_id="p", session=Session(), sleep=lambda s: None))
    dry = run(MetaAPI(account_id="act_1", dry_run=True))
    assert set(live) == set(dry) and set(live["history"][0]) == set(dry["history"][0])
    assert set(live["history"][0]["line_items"]["meta_prospecting"]) == set(dry["history"][0]["line_items"]["meta_prospecting"])
    assert live["history"][0]["line_items"]["meta_prospecting"]["reported"]["attributed_conversions"] == 3


# ---- the runner drives it exactly as it drives the simulator ----
class Scripted(FakeClient):
    def __init__(self):
        super().__init__(["{}"])

    def create(self, **kw):
        sys_ = kw["messages"][0]["content"]
        if "Planner" in sys_:
            self.responses = [json.dumps(PLAN)]
        elif "channel agent" in sys_:
            acts = [{"type": "shift_budget", "from": "meta_retargeting", "to": "meta_prospecting", "amount": 5}]
            self.responses = [json.dumps({"actions": acts})]
        elif "Before the campaign starts" in sys_:
            self.responses = [json.dumps({"criteria": ["x"]})]
        else:
            self.responses = [json.dumps({"verdict": "pass", "summary": "ok", "evidence": []})]
        return super().create(**kw)


def _actions(store, run_id):
    return store.conn.execute("SELECT count(*) FROM actions WHERE run_id = ?", (run_id,)).fetchone()[0]


def test_runner_drives_a_full_dry_run_across_separate_loads(store, tmp_path):
    pb = tmp_path / "v1.md"
    pb.write_text("# Playbook\n1. test it\n")
    b, _, clock = make(store)
    run_id, client = b.run_id, Scripted()
    state, checked_early = runner.new_state(), False
    for _ in range(12):
        b = MetaBackend.load(store, run_id, MetaAPI(account_id="act_1", dry_run=True), clock=clock)  # a fresh process
        if b.status == "awaiting_data":
            if not checked_early:  # too early: nothing is read, written or recorded
                before = _actions(store, run_id)
                assert runner.run_checkpoint(str(pb), b, state, client=client, store=store, run_id=run_id) == state
                assert _actions(store, run_id) == before
                checked_early = True
            clock.to(state["due_at"])
            continue
        state = runner.run_checkpoint(str(pb), b, state, client=client, store=store, run_id=run_id)
        if state["stage"] == "finished":
            break
    assert state["stage"] == "finished" and checked_early
    assert store.conn.execute("SELECT count(*) FROM checkpoints WHERE run_id = ?", (run_id,)).fetchone()[0] == 4
    assert store.load_run_state(run_id)["status"] == "done"
    acts = [a for a in store.get_trace(run_id) if a["kind"] == "action"]
    assert acts[0]["action"]["_kind"] == "plan" and acts[0]["ok"]
    assert any(a["ok"] and a["action"].get("type") == "shift_budget" for a in acts)


def test_compressed_time_schedule_follows_real_seconds(store, monkeypatch):
    import backends.meta as meta
    real = Clock(dt.datetime(2026, 3, 2, 9, 0, 0))
    monkeypatch.setattr(meta, "_utcnow", real)
    api = MetaAPI(account_id="act_1", page_id="p1", dry_run=True)
    b = MetaBackend.create(store, api, {**CFG, "start_date": None, "interval_days": 1, "read_delay_days": 1,
                                        "attribution_window_days": 1, "seconds_per_day": 10.0})
    b.apply_plan(PLAN)
    b.s["activated"] = True
    b.cfg["clock_anchor"] = real().isoformat()  # as activate() does
    b.s["due_at"] = b._data_due(1).isoformat()
    assert b.status == "ready"  # checkpoint 0 -> 1 has no wait
    b.s["checkpoint"] = 1
    assert b.status == "awaiting_data"  # period 1 is day 1, data due at the end of day 2
    real.now += dt.timedelta(seconds=19)
    assert b.status == "awaiting_data"
    real.now += dt.timedelta(seconds=2)
    assert b.status == "ready"
    assert MetaBackend.load(store, b.run_id, api).status == "ready"  # a fresh process derives the same clock
