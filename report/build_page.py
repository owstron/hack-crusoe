"""Build the results page for one improvement run: runs/<name>/index.html (static, open in a browser).

    .venv/bin/python -m report.build_page --name main            # once
    .venv/bin/python -m report.build_page --name main --watch    # rebuild every 5s during a run

Reads runs/<name>/changelog.jsonl, summary.json (present once the run finishes) and the final eval
(runs/<name>/final_eval.json, else results/final_eval.json). While the run is in progress the page
reloads itself every 10s.
"""
from __future__ import annotations

import argparse
import json
import os
import time

HERE = os.path.dirname(os.path.abspath(__file__))


def build(name: str, final_path: str = None) -> str:
    root = os.path.join("runs", name)
    log_path = os.path.join(root, "changelog.jsonl")
    changelog = []
    if os.path.exists(log_path):
        with open(log_path) as f:
            changelog = [json.loads(line) for line in f if line.strip()]
    for e in changelog:
        e.pop("val_scores", None)  # per-run scores are not shown; keep the page small
        e.pop("traceback", None)
    complete = os.path.exists(os.path.join(root, "summary.json"))
    final_eval = None
    for p in [final_path, os.path.join(root, "final_eval.json"), os.path.join("results", "final_eval.json")]:
        if p and os.path.exists(p):
            with open(p) as f:
                final_eval = json.load(f)
            for d in ("v1_detail", "final_detail"):
                final_eval.get(d, {}).pop("runs", None)
            break
    data = {"name": name, "complete": complete, "changelog": changelog, "final_eval": final_eval,
            "built": time.strftime("%H:%M:%S")}
    with open(os.path.join(HERE, "template.html")) as f:
        html = f.read()
    payload = json.dumps(data).replace("</", "<\\/")
    html = html.replace("/*DATA*/null", payload)
    if not complete:
        html = html.replace("<!--REFRESH-->", '<meta http-equiv="refresh" content="10">')
    out = os.path.join(root, "index.html")
    os.makedirs(root, exist_ok=True)
    with open(out, "w") as f:
        f.write(html)
    return out


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--name", required=True)
    p.add_argument("--final", default=None, help="final eval JSON (default: runs/<name>/final_eval.json)")
    p.add_argument("--watch", action="store_true")
    a = p.parse_args()
    while True:
        out = build(a.name, a.final)
        print(f"wrote {out}", flush=True)
        if not a.watch or os.path.exists(os.path.join("runs", a.name, "summary.json")):
            break
        time.sleep(5)
