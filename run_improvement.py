"""Run the self-improvement loop.

    .venv/bin/python run_improvement.py --name main --rounds 6 \
        --runner agents.runner:run --improver agents.improver:propose_edit

Without --runner/--improver it uses the model-free stubs in improve/stubs.py (loop smoke test).
"""
import argparse
import asyncio
import os

from dotenv import load_dotenv

from improve import loop
from improve.loop import improve, load
from sim import gate
from sim.gate import MARGIN
from store import DEFAULT_PATH


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--name", required=True, help="run directory under runs/")
    p.add_argument("--rounds", type=int, default=6)
    p.add_argument("--start", default="playbooks/v1.md", help="starting playbook")
    p.add_argument("--runner", default="improve.stubs:runner")
    p.add_argument("--improver", default="improve.stubs:propose_edit")
    p.add_argument("--margin", type=float, default=MARGIN)
    p.add_argument("--overwrite", action="store_true")
    p.add_argument("--resume", action="store_true", help="continue an earlier --name from its last logged round")
    p.add_argument("--campaigns", type=int, default=None, help="validation/test campaigns per eval (default 6)")
    p.add_argument("--seeds", type=int, default=None, help="sim seeds per validation/test campaign (default 3)")
    p.add_argument("--train-campaigns", type=int, default=None, help="train campaigns per round (default 6)")
    p.add_argument("--concurrency", type=int, default=None,
                   help="max simultaneous model requests and campaigns (default: 6 requests, 16 campaigns)")
    p.add_argument("--final-eval", action="store_true", help="after the loop, run the test split once: v1 vs final")
    p.add_argument("--db", default=DEFAULT_PATH, help="run store database")
    a = p.parse_args(argv)

    if a.concurrency:
        os.environ["MODEL_CONCURRENCY"] = str(a.concurrency)  # read when agents.models is imported
        gate.CONCURRENCY = a.concurrency
    if a.campaigns:
        gate.N_CAMPAIGNS = a.campaigns
    if a.seeds:
        gate.SEEDS = tuple(range(a.seeds))
    if a.train_campaigns:
        loop.N_TRAIN = a.train_campaigns

    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
    if a.start == "playbooks/v1.md" and not os.path.exists(a.start) and a.runner.startswith("improve.stubs"):
        from improve.stubs import STUB_V1
        a.start = os.path.join("runs", f"{a.name}.stub_v1.md")
        os.makedirs("runs", exist_ok=True)
        open(a.start, "w").write(STUB_V1)
    if a.runner.startswith("improve.stubs") or a.improver.startswith("improve.stubs"):
        print("NOTE: using model-free stubs from improve/stubs.py")

    summary = asyncio.run(improve(load(a.runner), load(a.improver), a.start, a.name, a.rounds, a.margin, a.overwrite,
                                 a.db, a.resume))
    print("\nversions:")
    for v in summary["versions"]:
        print(f"  round {v['round']}: {v['version']}  val {v['val_mean']:.3f}")
    print(f"final: {summary['final_version']}")
    if a.final_eval:
        from sim.final_eval import final_eval
        first = summary["versions"][0]["version"]
        r = asyncio.run(final_eval(load(a.runner), first, summary["final_version"],
                                   out_path=os.path.join("runs", a.name, "final_eval.json"),
                                   store_path=a.db, session=a.name))
        print(f"test: v1 {r['v1_mean']:.3f} -> final {r['final_mean']:.3f}  diff {r['mean_diff']:+.3f} "
              f"CI {r['ci95']}  W{r['wins']}/L{r['losses']}")
    from report.build_page import build
    print("report:", build(a.name, db_path=a.db))


if __name__ == "__main__":
    main()
