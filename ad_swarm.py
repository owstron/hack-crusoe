#!/usr/bin/env python3
"""Unified CLI: the same agents run on the simulator or on Meta, selected by the command, not by editing code.

  ad_swarm.py simulate run     --playbook playbooks/v1.md [--split train|validation] [--seed 0]
  ad_swarm.py simulate improve --name main --rounds 6 --runner agents.runner:run --improver agents.improver:propose_edit
  ad_swarm.py simulate report  --name main

  ad_swarm.py live create     --playbook playbooks/v1.md [--dry-run] [--total-budget 400 ...]   (paused objects only)
  ad_swarm.py live approve    --run-id R [--list | --all | --creative ID ...]                   (human creative approval)
  ad_swarm.py live observe    --run-id R                                                        (read-only)
  ad_swarm.py live checkpoint --run-id R                      (run when due, e.g. from cron; refuses to act early)
  ad_swarm.py live activate   --run-id R --yes                (the only command that turns ads on)

Every command prints the backend, model configuration, playbook, run and whether an ad-platform write will happen
before it does anything. All run state lives in the run store, keyed by run_id with mode `sim` or `live`.
A run created with --dry-run stays a dry run for every later command.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import time
import sys
from typing import List, Optional

from dotenv import load_dotenv

from store import DEFAULT_PATH, Store

HERE = os.path.dirname(os.path.abspath(__file__))


class CliError(Exception):
    pass


def _models() -> str:
    from agents.models import ROLE_MODELS, ROLE_TEMPERATURE
    return ", ".join(f"{r}={ROLE_MODELS[r].split('/')[-1]} (T={ROLE_TEMPERATURE[r]})" for r in ("planner", "channel", "verifier"))


def banner(**fields) -> None:
    print("-" * 72)
    for k, v in fields.items():
        print(f"{k.replace('_', ' '):<13} {v}")
    print("-" * 72, flush=True)


# ---------------------------------------------------------------- simulate
def cmd_sim_run(a) -> int:
    if a.split == "test":
        raise CliError("the test split is used once, by final_eval; simulate run only takes train or validation")
    banner(command="simulate run", backend="simulator (local; no ad platform)", models=_models(),
           playbook=a.playbook, run=f"new sim run, split={a.split}, seed={a.seed}", api_write="no")
    from improve.loop import load
    from sim.gate import evaluate_version
    rows = asyncio.run(evaluate_version(load(a.runner), a.playbook, a.split, n=1, seeds=(a.seed,), store_path=a.db))
    for key, r in rows.items():
        print(f"{key}: score {r['score']:.3f}  run {r.get('run_id')}  error {r['error']}")
    return 0


def cmd_sim_improve(a, rest: List[str]) -> int:
    banner(command="simulate improve", backend="simulator (local; no ad platform)", models=_models(),
           playbook=a.start, run=f"improvement session {a.name!r}", api_write="no (writes runs/ and the run store)")
    import run_improvement
    run_improvement.main(["--name", a.name, "--start", a.start, "--db", a.db, *rest])
    return 0


def cmd_sim_report(a) -> int:
    banner(command="simulate report", backend="none (reads the run store)", models="n/a", playbook="n/a",
           run=f"session {a.name!r}", api_write="no")
    from report.build_page import build
    print("report:", build(a.name, db_path=a.db))
    return 0


# -------------------------------------------------------------------- live
def _api(dry_run: bool, account_id: Optional[str] = None):
    from backends import MetaAPI
    if dry_run:
        return MetaAPI(account_id=account_id or "act_dry", page_id="page_dry", dry_run=True)
    api = MetaAPI.from_env()
    missing = [n for n, v in (("META_ACCESS_TOKEN", api.token), ("META_AD_ACCOUNT_ID", api.account_id),
                              ("META_PAGE_ID", api.page_id)) if not v]
    if missing:
        raise CliError(f"live mode needs {', '.join(missing)} (see .env.example), or use --dry-run")
    return api


def _open(store: Store, run_id: str):
    from backends import MetaBackend
    try:
        run = store.load_run_state(run_id)
    except KeyError:
        raise CliError(f"unknown run {run_id!r}")
    if run["mode"] != "live":
        raise CliError(f"{run_id} is a {run['mode']} run; live commands need a live run")
    cfg = run["config"]
    backend = MetaBackend.load(store, run_id, _api(bool(cfg.get("dry_run")), cfg.get("account_id")))
    return backend, run


def _live_banner(command: str, backend, run: dict, write: str) -> None:
    cfg = run["config"]
    mode = "Meta DRY RUN (no Graph API calls)" if cfg.get("dry_run") else "Meta LIVE (Graph API)"
    banner(command=f"live {command}", backend=mode, models=_models(), playbook=run["playbook_version"],
           account_run=f"{cfg.get('account_id')} / {run['run_id']}", api_write=write)


def _pending_note(store: Store, run_id: str) -> None:
    pending = store.list_creatives(run_id, "pending")
    if pending:
        print(f"{len(pending)} creative(s) pending human approval: ad_swarm.py live approve --run-id {run_id} --list")


def _create_cfg(a) -> dict:
    return {k: v for k, v in dict(
        product=a.product, avg_order_value=a.aov, total_budget=a.total_budget, n_checkpoints=a.checkpoints,
        interval_days=a.interval_days, read_delay_days=a.read_delay_days, attribution_window_days=a.attribution_window_days,
        start_date=a.start_date, max_total_spend=a.max_total_spend, max_period_spend=a.max_period_spend,
        min_impressions=a.min_impressions, min_conversions=a.min_conversions,
        seconds_per_day=a.seconds_per_day).items() if v is not None}


def cmd_live_create(a) -> int:
    from agents import runner
    from backends import MetaBackend
    api = _api(a.dry_run)
    cfg = _create_cfg(a)
    write = ("no (dry run)" if a.dry_run else "YES: creates a campaign and ad sets, all PAUSED; nothing is turned on")
    banner(command="live create", backend="Meta DRY RUN (no Graph API calls)" if a.dry_run else "Meta LIVE (Graph API)",
           models=_models(), playbook=a.playbook, account_run=f"{api.account_id} / new run", api_write=write)
    with Store(a.db) as store:
        backend = MetaBackend.create(store, api, cfg, playbook_version=a.playbook)
        state = runner.run_checkpoint(a.playbook, backend, runner.new_state(), store=store, run_id=backend.run_id,
                                      want_creatives=True)
        print(f"run {backend.run_id}: plan applied={state['plan_applied']}, errors={state['errors']}, next data due {state['due_at']}")
        _pending_note(store, backend.run_id)
        print("Objects are PAUSED. Nothing is delivering until `live activate`.")
        return 0 if state["plan_applied"] else 1


def cmd_live_approve(a) -> int:
    with Store(a.db) as store:
        backend, run = _open(store, a.run_id)
        _live_banner("approve", backend, run, "no" if a.list else "yes: creates PAUSED ads for the approved creatives")
        creatives = store.list_creatives(a.run_id)
        if a.list:
            for c in creatives:
                print(f"[{c['status']:<8}] {c['line_item']}/{c['creative_id']}: {c['content']}")
            return 0
        known = {c["creative_id"] for c in creatives}
        unknown = set(a.creative or []) - known
        if unknown:
            raise CliError(f"unknown creative id(s): {sorted(unknown)}")
        if not a.all and not a.creative:
            raise CliError("name creatives with --creative ID, or pass --all (see --list)")
        n = 0
        for c in store.list_creatives(a.run_id, "pending"):
            if a.all or c["creative_id"] in a.creative:
                store.set_creative_status(a.run_id, c["line_item"], c["creative_id"], "approved")
                n += 1
        res = backend.sync_creatives()
        print(f"approved {n}; sync: {res}")
        return 0 if res["ok"] else 1


def cmd_live_observe(a) -> int:
    with Store(a.db) as store:
        backend, run = _open(store, a.run_id)
        _live_banner("observe", backend, run, "no (reads Insights and stores the reading)")
        obs = backend.observe()
        print(f"status {obs['status']}  checkpoint {obs['checkpoint']}/{obs['campaign']['n_checkpoints']}  due_at {backend.due_at}")
        print(f"budgets {obs['budgets']}")
        for h in obs["history"][-2:]:
            print(f"period {h['period']['start']}..{h['period']['end']} read {h['read_no']} {h['status']} "
                  f"delivery_ok={h['delivery_ok']} window closes {h['attribution_window_closes_at']}")
            for li, m in h["line_items"].items():
                r = m["reported"]
                print(f"  {li}: spend ${m['spend']:.2f}, conversions {r['attributed_conversions']}, cpa {r['cpa']}, roas {r['roas']}")
        for c in obs["creatives"]:
            print(f"creative {c['line_item']}/{c['id']}: {c['status']}, age {c['age_checkpoints']}")
        return 0


def cmd_live_checkpoint(a) -> int:
    from agents import runner
    with Store(a.db) as store:
        backend, run = _open(store, a.run_id)
        _live_banner("checkpoint", backend, run, "possible: budget and creative changes within the spend caps")
        if backend.done:
            print("campaign finished; nothing to do")
            return 0
        if backend.status == "awaiting_data":
            print(f"not due until {backend.due_at}; exiting without any action")
            return 0
        state = run["state"] or runner.new_state()
        state = runner.run_checkpoint(run["playbook_version"], backend, state, store=store, run_id=a.run_id,
                                      want_creatives=True, echo=print)
        _print_readings(backend, 0)
        print(f"stage {state['stage']}, errors {state['errors']}, next data due {state['due_at']}")
        return 0


def _print_readings(backend, seen: int) -> int:
    """Print readings in the backend history after the first `seen`; returns the new count."""
    hist = backend.s["history"]
    for h in hist[seen:]:
        print(f"  reading cp{h['checkpoint']}.{h['read_no']} {h['period']['start']}..{h['period']['end']} [{h['status']}]")
        for li, m in h["line_items"].items():
            r = m["reported"]
            print(f"    {li}: spend ${m['spend']:.2f}, {m['impressions']} imps, {m['clicks']} clicks, "
                  f"{r['attributed_conversions']} conv, ROAS {r['roas']}")
    return len(hist)


def cmd_live_report(a) -> int:
    from report.live_report import build
    print("report:", build(a.run_id, a.db))
    return 0


def cmd_live_run(a) -> int:
    """Continuous: campaign after campaign (and playbook improvement) until Ctrl-C or `touch runs/STOP`."""
    if not a.dry_run:
        raise CliError("continuous mode is dry-run only for now: pass --dry-run (nothing is sent to Meta)")
    cfg = _create_cfg(a)
    banner(command="live run", backend="Meta DRY RUN (no Graph API calls), continuous", models=_models(),
           playbook=a.playbook, account_run=f"act_dry / cycles until stopped (max {a.max_cycles or 'none'})",
           api_write="no (dry run)" + (f"; improving playbook in simulator session {a.improve_session!r}" if a.improve_session else ""))
    import live_loop
    return live_loop.supervise(a, cfg, _api)


def cmd_live_watch(a) -> int:
    """Poll `checkpoint` until the campaign is done (time-compressed demos; otherwise schedule `checkpoint` externally)."""
    from agents import runner
    seen = 0
    while True:
        with Store(a.db) as store:
            backend, run = _open(store, a.run_id)
            if backend.done:
                verdict = (run["state"] or {}).get("verdict")
                print(f"campaign finished; verdict: {verdict}")
                print("report:", __import__("report.live_report", fromlist=["build"]).build(a.run_id, a.db))
                return 0
            if backend.status == "awaiting_data":
                seen = _print_readings(backend, seen)
                print(f"[{time.strftime('%H:%M:%S')}] waiting for data (due {backend.due_at} schedule time)", flush=True)
            else:
                state = runner.run_checkpoint(run["playbook_version"], backend, run["state"] or runner.new_state(),
                                              store=store, run_id=a.run_id, want_creatives=True, echo=print)
                seen = _print_readings(backend, seen)
                print(f"[{time.strftime('%H:%M:%S')}] checkpoint ran: stage {state['stage']}, errors {state['errors']}, "
                      f"next data due {state['due_at']}", flush=True)
        time.sleep(a.every)


def cmd_live_activate(a) -> int:
    with Store(a.db) as store:
        backend, run = _open(store, a.run_id)
        _live_banner("activate", backend, run, "no (dry run)" if run["config"].get("dry_run")
                     else "YES: TURNS ADS ON. Real money is spent from here, up to the configured caps")
        if not a.yes:
            raise CliError("activation is explicit: re-run with --yes to turn delivery on")
        res = backend.activate()
        print(res if not res["ok"] else f"activated; first data due {backend.due_at}")
        return 0 if res["ok"] else 1


# --------------------------------------------------------------------- cli
def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", default=DEFAULT_PATH, help="run store database")
    p = argparse.ArgumentParser(prog="ad_swarm.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = p.add_subparsers(dest="mode", required=True)

    sim = mode.add_parser("simulate").add_subparsers(dest="cmd", required=True)
    s = sim.add_parser("run", parents=[common], help="play one train/validation campaign with the agents")
    s.add_argument("--playbook", default="playbooks/v1.md")
    s.add_argument("--split", default="train", choices=("train", "validation", "test"))
    s.add_argument("--seed", type=int, default=0)
    s.add_argument("--runner", default="agents.runner:run")
    s = sim.add_parser("improve", parents=[common], help="run the N+1 loop (extra flags go to run_improvement.py)")
    s.add_argument("--name", required=True)
    s.add_argument("--start", default="playbooks/v1.md")
    s = sim.add_parser("report", parents=[common], help="render runs/<name>/index.html from the store")
    s.add_argument("--name", required=True)

    live = mode.add_parser("live").add_subparsers(dest="cmd", required=True)
    s = live.add_parser("create", parents=[common], help="create a PAUSED campaign from the agents' plan")
    r = live.add_parser("run", parents=[common], help="continuous: campaign after campaign until stopped (dry run only)")
    r.add_argument("--every", type=float, default=5.0, help="seconds between polls")
    r.add_argument("--max-cycles", type=int, default=None, help="stop after this many campaigns (default: never)")
    r.add_argument("--improve-session", default=None,
                   help="also improve the playbook in the simulator, in this session name (keeps going until stopped)")
    for s in (s, r):
        s.add_argument("--playbook", default="playbooks/v1.md")
        s.add_argument("--dry-run", action="store_true", help="no Graph API calls; same state and shapes")
        for flag, typ in (("product", str), ("aov", float), ("total-budget", float), ("checkpoints", int),
                          ("interval-days", int), ("read-delay-days", int), ("attribution-window-days", int),
                          ("start-date", str), ("max-total-spend", float), ("max-period-spend", float),
                          ("min-impressions", int), ("min-conversions", int), ("seconds-per-day", float)):
            s.add_argument(f"--{flag}", type=typ, default=None)
    s = live.add_parser("approve", parents=[common], help="approve pending creatives (a human decision)")
    s.add_argument("--run-id", required=True)
    s.add_argument("--list", action="store_true")
    s.add_argument("--all", action="store_true")
    s.add_argument("--creative", action="append")
    for name, hlp in (("observe", "show the latest readings (no changes)"), ("checkpoint", "run one checkpoint if due"),
                      ("activate", "turn ads on"), ("report", "write runs/<run_id>/index.html"), ("watch", "run checkpoints as they fall due until the campaign is done")):
        s = live.add_parser(name, parents=[common], help=hlp)
        s.add_argument("--run-id", required=True)
        if name == "watch":
            s.add_argument("--every", type=float, default=5.0, help="seconds between polls")
        if name == "activate":
            s.add_argument("--yes", action="store_true", help="required: confirms delivery will start")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    load_dotenv(os.path.join(HERE, ".env"))
    a, rest = build_parser().parse_known_args(argv)
    if rest and not (a.mode == "simulate" and a.cmd == "improve"):
        build_parser().error(f"unrecognized arguments: {' '.join(rest)}")
    try:
        if a.mode == "simulate":
            return {"run": cmd_sim_run, "report": cmd_sim_report}.get(a.cmd, lambda x: cmd_sim_improve(x, rest))(a)
        return {"create": cmd_live_create, "approve": cmd_live_approve, "observe": cmd_live_observe,
                "checkpoint": cmd_live_checkpoint, "activate": cmd_live_activate,
                "watch": cmd_live_watch, "run": cmd_live_run, "report": cmd_live_report}[a.cmd](a)
    except CliError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
