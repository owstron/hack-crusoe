"""Build the results page for one improvement run: runs/<name>/index.html (static, open in a browser).

    .venv/bin/python -m report.build_page --name main            # once
    .venv/bin/python -m report.build_page --name main --watch    # rebuild every 5s during a run

Reads the improvement session (change log, summary, final eval) from the run store, store/adswarm.db;
falls back to runs/<name>/final_eval.json or results/final_eval.json for a final eval stored elsewhere. While the run is in progress the page
reloads itself every 10s.
"""
from __future__ import annotations

import argparse
import json
import os
import time

from store import DEFAULT_PATH, Store

HERE = os.path.dirname(os.path.abspath(__file__))


def build(name: str, final_path: str = None, db_path: str = DEFAULT_PATH) -> str:
    root = os.path.join("runs", name)
    with Store(db_path) as st:
        session = st.session_data(name)
    changelog = list(session["changelog"]) if session else []
    for e in changelog:
        e.pop("val_scores", None)  # per-run scores are not shown; keep the page small
        e.pop("traceback", None)
    complete = bool(session and session["complete"])
    final_eval = session["final_eval"] if session else None
    if final_eval is None:  # fall back to a final eval JSON written outside the store
        for p in [final_path, os.path.join(root, "final_eval.json"), os.path.join("results", "final_eval.json")]:
            if p and os.path.exists(p):
                with open(p) as f:
                    final_eval = json.load(f)
                break
    if final_eval:
        for d in ("v1_detail", "final_detail"):
            final_eval.get(d, {}).pop("runs", None)
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
    p.add_argument("--db", default=DEFAULT_PATH)
    a = p.parse_args()
    while True:
        out = build(a.name, a.final, a.db)
        print(f"wrote {out}", flush=True)
        with Store(a.db) as st:
            data = st.session_data(a.name)
        if not a.watch or (data and data["complete"]):
            break
        time.sleep(5)
