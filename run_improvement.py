"""Run the self-improvement loop.

    .venv/bin/python run_improvement.py --name main --rounds 6 \
        --runner agents.runner:run --improver agents.improver:propose_edit

Without --runner/--improver it uses the model-free stubs in improve/stubs.py (loop smoke test).
"""
import argparse
import asyncio
import os

from dotenv import load_dotenv

from improve.loop import improve, load
from sim.gate import MARGIN
from store import DEFAULT_PATH


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--name", required=True, help="run directory under runs/")
    p.add_argument("--rounds", type=int, default=6)
    p.add_argument("--start", default="playbooks/v1.md", help="starting playbook")
    p.add_argument("--runner", default="improve.stubs:runner")
    p.add_argument("--improver", default="improve.stubs:propose_edit")
    p.add_argument("--margin", type=float, default=MARGIN)
    p.add_argument("--overwrite", action="store_true")
    p.add_argument("--db", default=DEFAULT_PATH, help="run store database")
    a = p.parse_args()

    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
    if a.start == "playbooks/v1.md" and not os.path.exists(a.start) and a.runner.startswith("improve.stubs"):
        from improve.stubs import STUB_V1
        a.start = os.path.join("runs", f"{a.name}.stub_v1.md")
        os.makedirs("runs", exist_ok=True)
        open(a.start, "w").write(STUB_V1)
    if a.runner.startswith("improve.stubs") or a.improver.startswith("improve.stubs"):
        print("NOTE: using model-free stubs from improve/stubs.py")

    summary = asyncio.run(improve(load(a.runner), load(a.improver), a.start, a.name, a.rounds, a.margin, a.overwrite,
                                 a.db))
    print("\nversions:")
    for v in summary["versions"]:
        print(f"  round {v['round']}: {v['version']}  val {v['val_mean']:.3f}")
    print(f"final: {summary['final_version']}")


if __name__ == "__main__":
    main()
