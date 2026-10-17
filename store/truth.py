"""Hidden-truth access. The ONLY module that reads or writes scores.debrief_json.

Allowed callers: sim.gate and sim.final_eval (save scores), and the Improver path in improve.loop (train-split
debriefs). Agent code (agents/, backends/) must never import this module; tests/test_store.py enforces it.
Debriefs are stored for the train split only, so no test-split debrief exists to leak.
"""
from __future__ import annotations

import json
from typing import Optional

from .db import Store


def save_score(store: Store, run_id: str, split: str, score: float, debrief: Optional[dict] = None) -> None:
    """Persist a score. A debrief is kept only for the train split; others are dropped."""
    blob = json.dumps(debrief) if (debrief is not None and split == "train") else None
    with store._txn() as c:
        c.execute("INSERT OR REPLACE INTO scores (run_id, split, score, debrief_json) VALUES (?,?,?,?)",
                  (run_id, split, score, blob))


def train_debrief(store: Store, run_id: str) -> Optional[dict]:
    """Hidden-truth post-mortem for one TRAIN run (Improver only)."""
    r = store.conn.execute("SELECT debrief_json FROM scores WHERE run_id = ? AND split = 'train'", (run_id,)).fetchone()
    return None if r is None or r["debrief_json"] is None else json.loads(r["debrief_json"])
