import datetime as dt

import pytest

import ad_swarm
import agents.runner as runner
import backends.meta as meta
from store import Store
from test_meta_backend import Scripted

CREATE = ["live", "create", "--dry-run", "--start-date", "2026-03-02", "--total-budget", "400",
          "--min-impressions", "0", "--min-conversions", "0"]


class Clock:
    now = dt.datetime(2026, 3, 2)

    def __call__(self):
        return self.now


@pytest.fixture
def env(tmp_path, monkeypatch):
    clock = Clock()
    monkeypatch.setattr(meta, "_utcnow", clock)
    monkeypatch.setattr(runner, "_default_client", lambda: Scripted())
    pb = tmp_path / "v1.md"
    pb.write_text("# Playbook\n1. test it\n")
    db = str(tmp_path / "t.db")
    return clock, str(pb), db


def cli(db, *args):
    return ad_swarm.main([*args[:2], *args[2:], "--db", db])


def run_id(db):
    with Store(db) as s:
        return s.list_runs("live")[0]["run_id"]


def test_create_is_a_paused_dry_run_and_prints_the_context_first(env, capsys):
    clock, pb, db = env
    assert cli(db, *CREATE, "--playbook", pb) == 0
    out = capsys.readouterr().out
    assert out.index("api write") < out.index("run live-")  # the banner comes before anything happens
    assert "DRY RUN" in out and "no (dry run)" in out and "planner=" in out and pb in out
    assert "1 creative(s) pending human approval" in out and "PAUSED" in out
    with Store(db) as s:
        assert s.load_run_state(run_id(db))["config"]["dry_run"] is True


def test_checkpoint_refuses_early_then_runs_when_due(env, capsys):
    clock, pb, db = env
    cli(db, *CREATE, "--playbook", pb)
    rid = run_id(db)
    capsys.readouterr()
    with Store(db) as s:
        before = s.conn.execute("SELECT count(*) FROM actions").fetchone()[0]
    assert cli(db, "live", "checkpoint", "--run-id", rid) == 0
    assert "not due until 2026-03-16T00:00:00" in capsys.readouterr().out
    with Store(db) as s:
        assert s.conn.execute("SELECT count(*) FROM actions").fetchone()[0] == before
        assert s.conn.execute("SELECT count(*) FROM checkpoints").fetchone()[0] == 0
    clock.now = dt.datetime(2026, 3, 16)
    assert cli(db, "live", "checkpoint", "--run-id", rid) == 0
    assert "stage running" in capsys.readouterr().out
    with Store(db) as s:
        assert s.conn.execute("SELECT count(*) FROM checkpoints").fetchone()[0] == 1


def test_approve_then_activate_needs_yes(env, capsys):
    clock, pb, db = env
    cli(db, *CREATE, "--playbook", pb)
    rid = run_id(db)
    assert cli(db, "live", "activate", "--run-id", rid) == 2  # no --yes
    assert cli(db, "live", "activate", "--run-id", rid, "--yes") == 1  # no approved creative attached
    assert cli(db, "live", "approve", "--run-id", rid) == 2  # must name creatives
    assert cli(db, "live", "approve", "--run-id", rid, "--creative", "nope") == 2
    assert cli(db, "live", "approve", "--run-id", rid, "--list") == 0
    assert "[pending " in capsys.readouterr().out
    with Store(db) as s:
        assert s.list_creatives(rid, "approved") == []
    assert cli(db, "live", "approve", "--run-id", rid, "--all") == 0
    assert cli(db, "live", "activate", "--run-id", rid, "--yes") == 0
    with Store(db) as s:
        assert s.load_run_state(rid)["status"] == "active"


def test_observe_is_read_only(env, capsys):
    clock, pb, db = env
    cli(db, *CREATE, "--playbook", pb)
    rid = run_id(db)
    capsys.readouterr()
    assert cli(db, "live", "observe", "--run-id", rid) == 0
    out = capsys.readouterr().out
    assert "status awaiting_data" in out and "no (reads Insights" in out


def test_errors_are_clean(env, capsys):
    clock, pb, db = env
    assert cli(db, "live", "observe", "--run-id", "nope") == 2
    assert "unknown run" in capsys.readouterr().err
    assert cli(db, "simulate", "run", "--split", "test") == 2
    assert "test split" in capsys.readouterr().err


def test_live_without_credentials_is_refused_before_any_work(env, monkeypatch, capsys):
    clock, pb, db = env
    for k in ("META_ACCESS_TOKEN", "META_AD_ACCOUNT_ID", "META_PAGE_ID"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(ad_swarm, "load_dotenv", lambda *a, **k: None)
    assert cli(db, "live", "create", "--playbook", pb) == 2
    assert "META_ACCESS_TOKEN" in capsys.readouterr().err
    with Store(db) as s:
        assert s.list_runs() == []


def test_simulate_run_and_improve_use_the_stub_path(env, capsys, tmp_path, monkeypatch):
    clock, pb, db = env
    monkeypatch.chdir(tmp_path)
    assert ad_swarm.main(["simulate", "improve", "--name", "cli", "--rounds", "1", "--db", db]) == 0
    out = capsys.readouterr().out
    assert "simulator (local" in out and "final:" in out
    assert ad_swarm.main(["simulate", "report", "--name", "cli", "--db", db]) == 0
    assert (tmp_path / "runs" / "cli" / "index.html").exists()
