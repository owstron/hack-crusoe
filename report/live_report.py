"""Static report for one live run: runs/<run_id>/index.html (readings per period, agent timeline).

    ad_swarm.py live report --run-id R
"""
from __future__ import annotations

import html
import os

from store import Store


def _rows(history: list) -> str:
    out = []
    for h in history:
        for li, m in h["line_items"].items():
            r = m["reported"]
            out.append(f"<tr><td>{h['checkpoint']}.{h['read_no']}</td><td>{h['period']['start']}</td><td>{h['status']}</td>"
                       f"<td>{li}</td><td>${m['spend']:.2f}</td><td>{m['impressions']}</td><td>{m['clicks']}</td>"
                       f"<td>{r['attributed_conversions']}</td><td>{r['cpa'] if r['cpa'] is not None else '-'}</td>"
                       f"<td>{r['roas'] if r['roas'] is not None else '-'}</td></tr>")
    return "\n".join(out)


def _timeline(trace: list) -> str:
    out = []
    for e in trace:
        if e["kind"] == "model_call":
            body = html.escape(str(e["parsed"] if e["parsed"] is not None else e["error"]))[:600]
            out.append(f"<li><b>cp {e['checkpoint_idx']} {html.escape(str(e['role']))}</b> "
                       f"<code>{html.escape(str(e['model']))}</code>"
                       f"{' (fallback)' if e['fallback_used'] else ''}<pre>{body}</pre></li>")
        else:
            a = html.escape(str(e["action"]))[:400]
            state = "ok" if e["ok"] else f"FAILED {html.escape(str(e['error']))}"
            out.append(f"<li><b>cp {e['checkpoint_idx']} action</b> {state}<pre>{a}</pre></li>")
    return "\n".join(out)


def build(run_id: str, db_path: str) -> str:
    with Store(db_path) as st:
        run = st.load_run_state(run_id)
        bs = st.load_backend_state(run_id) or {}
        trace = st.get_trace(run_id)
    history = bs.get("history", [])
    total = sum(sum(m["spend"] for m in h["line_items"].values()) for h in history if h["read_no"] == 1)
    page = f"""<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Live run {html.escape(run_id)}</title>
<style>body{{font:15px system-ui;margin:2rem auto;max-width:960px;padding:0 16px;color:#222}}
table{{border-collapse:collapse;width:100%}}td,th{{border-bottom:1px solid #ddd;padding:4px 8px;text-align:left}}
pre{{white-space:pre-wrap;background:#f6f6f6;padding:6px;margin:4px 0}}li{{margin-bottom:8px}}
@media(prefers-color-scheme:dark){{body{{background:#111;color:#ddd}}pre{{background:#222}}td,th{{border-color:#333}}}}</style>
<h1>Live run {html.escape(run_id)}</h1>
<p>status <b>{html.escape(str(run['status']))}</b> &middot; playbook {html.escape(str(run['playbook_version']))}
&middot; {'dry run' if run['config'].get('dry_run') else 'live'} &middot; total spend ${total:.2f}
of cap ${run['config'].get('max_total_spend')}</p>
<h2>Readings</h2><table><tr><th>cp.read</th><th>period</th><th>status</th><th>line item</th><th>spend</th><th>imps</th>
<th>clicks</th><th>conv</th><th>CPA</th><th>ROAS</th></tr>{_rows(history)}</table>
<h2>Agent timeline</h2><ol>{_timeline(trace)}</ol>"""
    out = os.path.join("runs", run_id, "index.html")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w") as f:
        f.write(page)
    return out
