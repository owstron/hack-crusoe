"""SQLite run store. Functions, not raw SQL, for the rest of the code.

One `Store` (one connection) per worker: WAL mode and short transactions let parallel gate campaigns write
safely. Hidden-truth debriefs are written and read only through store.truth; nothing here returns them.
"""
from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import time
import uuid
from typing import Any, Callable, Dict, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PATH = os.path.join(HERE, "adswarm.db")


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _j(obj: Any) -> Optional[str]:
    return None if obj is None else json.dumps(obj)


def _load(text: Optional[str]) -> Any:
    return None if text is None else json.loads(text)


def reset_db(path: str = DEFAULT_PATH) -> None:
    """Remove the database with its -wal and -shm files. Close every connection first."""
    for suffix in ("", "-wal", "-shm"):
        with contextlib.suppress(FileNotFoundError):
            os.remove(path + suffix)


class Store:
    def __init__(self, path: str = DEFAULT_PATH):
        self.path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.conn = sqlite3.connect(path, timeout=30, isolation_level=None)  # autocommit; explicit short txns
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA busy_timeout=30000")
        self.conn.execute("PRAGMA foreign_keys=ON")
        with open(os.path.join(HERE, "schema.sql")) as f:
            self.conn.executescript(f.read())

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @contextlib.contextmanager
    def _txn(self):
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        self.conn.execute("COMMIT")

    def _next_event_id(self, run_id: str) -> int:
        row = self.conn.execute(
            "UPDATE runs SET next_event_id = next_event_id + 1 WHERE run_id = ? RETURNING next_event_id - 1",
            (run_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown run {run_id!r}")
        return row[0]

    # ---- runs ----
    def create_run(self, mode: str, schema_version: int, playbook_version: Optional[str] = None,
                   config: Optional[dict] = None, max_total_spend: Optional[float] = None,
                   campaign_ref: Optional[str] = None, due_at: Optional[str] = None,
                   run_id: Optional[str] = None) -> str:
        run_id = run_id or f"{mode}-{uuid.uuid4().hex[:12]}"
        with self._txn() as c:
            c.execute(
                "INSERT INTO runs (run_id, mode, schema_version, playbook_version, config_json, status, created_at,"
                " due_at, max_total_spend, campaign_ref) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (run_id, mode, schema_version, playbook_version, json.dumps(config or {}), "created", _now(),
                 due_at, max_total_spend, campaign_ref))
        return run_id

    def set_run_status(self, run_id: str, status: str, due_at: Optional[str] = None) -> None:
        with self._txn() as c:
            c.execute("UPDATE runs SET status = ?, due_at = ? WHERE run_id = ?", (status, due_at, run_id))

    def save_state(self, run_id: str, state: dict, due_at: Optional[str] = None) -> None:
        """Persist the runner state needed to resume (plan applied, next checkpoint, ...)."""
        with self._txn() as c:
            c.execute("UPDATE runs SET state_json = ?, due_at = ? WHERE run_id = ?", (json.dumps(state), due_at, run_id))

    def load_run_state(self, run_id: str) -> dict:
        """Everything a fresh process needs to resume a run. Never includes hidden truth."""
        r = self.conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if r is None:
            raise KeyError(f"unknown run {run_id!r}")
        last = self.conn.execute(
            "SELECT checkpoint_idx, read_no FROM checkpoints WHERE run_id = ? ORDER BY checkpoint_idx DESC, read_no DESC LIMIT 1",
            (run_id,)).fetchone()
        return {
            "run_id": run_id, "mode": r["mode"], "schema_version": r["schema_version"],
            "playbook_version": r["playbook_version"], "config": _load(r["config_json"]), "status": r["status"],
            "created_at": r["created_at"], "due_at": r["due_at"], "max_total_spend": r["max_total_spend"],
            "campaign_ref": r["campaign_ref"], "state": _load(r["state_json"]),
            "last_checkpoint": last["checkpoint_idx"] if last else None,
            "last_read_no": last["read_no"] if last else None,
        }

    def list_runs(self, mode: Optional[str] = None) -> List[dict]:
        q, args = "SELECT run_id, mode, status, playbook_version, created_at FROM runs", ()
        if mode:
            q, args = q + " WHERE mode = ?", (mode,)
        return [dict(r) for r in self.conn.execute(q + " ORDER BY created_at, run_id", args)]

    # ---- checkpoints, actions, model calls ----
    def record_checkpoint(self, run_id: str, checkpoint_idx: int, observation: dict, raw: Optional[dict] = None,
                          read_no: int = 1, period: Optional[dict] = None, provisional: bool = False) -> None:
        """A re-read of a period uses a higher read_no and never overwrites an earlier reading."""
        period = period or {}
        with self._txn() as c:
            c.execute(
                "INSERT INTO checkpoints (run_id, checkpoint_idx, read_no, period_start, period_end, observation_json,"
                " raw_json, provisional, ts) VALUES (?,?,?,?,?,?,?,?,?)",
                (run_id, checkpoint_idx, read_no, period.get("start"), period.get("end"), json.dumps(observation),
                 _j(raw), int(provisional), _now()))

    def get_checkpoint(self, run_id: str, checkpoint_idx: int, read_no: Optional[int] = None) -> Optional[dict]:
        q = "SELECT * FROM checkpoints WHERE run_id = ? AND checkpoint_idx = ?"
        args: list = [run_id, checkpoint_idx]
        if read_no is not None:
            q, args = q + " AND read_no = ?", args + [read_no]
        r = self.conn.execute(q + " ORDER BY read_no DESC LIMIT 1", args).fetchone()
        if r is None:
            return None
        return {"checkpoint_idx": r["checkpoint_idx"], "read_no": r["read_no"], "observation": _load(r["observation_json"]),
                "raw": _load(r["raw_json"]), "provisional": bool(r["provisional"]), "ts": r["ts"]}

    def record_action(self, run_id: str, checkpoint_idx: int, action: dict, result_ok: Optional[bool],
                      error: Optional[str] = None, read_no: int = 1) -> int:
        with self._txn() as c:
            eid = self._next_event_id(run_id)
            c.execute(
                "INSERT INTO actions (event_id, run_id, checkpoint_idx, read_no, action_json, result_ok, error, ts)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (eid, run_id, checkpoint_idx, read_no, json.dumps(action),
                 None if result_ok is None else int(result_ok), error, _now()))
        return eid

    def record_model_call(self, record: dict) -> int:
        """Accepts the record dict emitted by agents.models.call_model's recorder (role, model, temperature, prompt,
        raw_response, parsed_json, error, fallback_used, plus run_id / checkpoint_idx / read_no from its ctx)."""
        run_id = record["run_id"]
        with self._txn() as c:
            eid = self._next_event_id(run_id)
            c.execute(
                "INSERT INTO model_calls (event_id, run_id, checkpoint_idx, read_no, role, model, temperature, prompt,"
                " raw_response, parsed_json, error, fallback_used, ts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (eid, run_id, record.get("checkpoint_idx", 0), record.get("read_no", 0), record.get("role"),
                 record.get("model"), record.get("temperature"), _as_text(record.get("prompt")),
                 _as_text(record.get("raw_response")), _j(record.get("parsed_json")), record.get("error"),
                 int(bool(record.get("fallback_used"))), _now()))
        return eid

    def model_call_recorder(self, run_id: str, checkpoint_idx: int = 0, read_no: int = 0) -> Callable[[dict], int]:
        """A recorder callable to pass to call_model; ctx keys in the record override these defaults."""
        return lambda rec: self.record_model_call({"run_id": run_id, "checkpoint_idx": checkpoint_idx,
                                                   "read_no": read_no, **rec})

    def get_trace(self, run_id: str) -> List[dict]:
        """Actions and model calls in event_id order (stable trace ordering). Agent-safe: no hidden truth."""
        out = []
        for r in self.conn.execute("SELECT * FROM actions WHERE run_id = ?", (run_id,)):
            out.append({"event_id": r["event_id"], "kind": "action", "checkpoint_idx": r["checkpoint_idx"],
                        "read_no": r["read_no"], "action": _load(r["action_json"]),
                        "ok": None if r["result_ok"] is None else bool(r["result_ok"]), "error": r["error"]})
        for r in self.conn.execute("SELECT * FROM model_calls WHERE run_id = ?", (run_id,)):
            out.append({"event_id": r["event_id"], "kind": "model_call", "checkpoint_idx": r["checkpoint_idx"],
                        "read_no": r["read_no"], "role": r["role"], "model": r["model"],
                        "temperature": r["temperature"], "prompt": r["prompt"], "raw_response": r["raw_response"],
                        "parsed": _load(r["parsed_json"]), "error": r["error"], "fallback_used": bool(r["fallback_used"])})
        return sorted(out, key=lambda e: e["event_id"])

    # ---- backend state (live backends) ----
    def save_backend_state(self, run_id: str, state: dict) -> None:
        with self._txn() as c:
            c.execute("INSERT INTO backend_state (run_id, state_json, updated_at) VALUES (?,?,?)"
                      " ON CONFLICT (run_id) DO UPDATE SET state_json = excluded.state_json, updated_at = excluded.updated_at",
                      (run_id, json.dumps(state), _now()))

    def load_backend_state(self, run_id: str) -> Optional[dict]:
        r = self.conn.execute("SELECT state_json FROM backend_state WHERE run_id = ?", (run_id,)).fetchone()
        return None if r is None else json.loads(r["state_json"])

    # ---- creatives and external ids ----
    def save_creative(self, run_id: str, line_item: str, creative_id: str, content: dict,
                      status: str = "pending", meta_id: Optional[str] = None) -> None:
        with self._txn() as c:
            c.execute(
                "INSERT INTO creatives (run_id, line_item, creative_id, content_json, status, meta_id) VALUES (?,?,?,?,?,?)"
                " ON CONFLICT (run_id, line_item, creative_id) DO UPDATE SET content_json = excluded.content_json,"
                " status = excluded.status, meta_id = COALESCE(excluded.meta_id, creatives.meta_id)",
                (run_id, line_item, creative_id, json.dumps(content), status, meta_id))

    def set_creative_status(self, run_id: str, line_item: str, creative_id: str, status: str,
                            meta_id: Optional[str] = None) -> None:
        with self._txn() as c:
            cur = c.execute(
                "UPDATE creatives SET status = ?, meta_id = COALESCE(?, meta_id)"
                " WHERE run_id = ? AND line_item = ? AND creative_id = ?", (status, meta_id, run_id, line_item, creative_id))
            if cur.rowcount == 0:
                raise KeyError(f"unknown creative {creative_id!r} for {line_item!r} in {run_id!r}")

    def list_creatives(self, run_id: str, status: Optional[str] = None) -> List[dict]:
        q, args = "SELECT * FROM creatives WHERE run_id = ?", [run_id]
        if status:
            q, args = q + " AND status = ?", args + [status]
        return [{"line_item": r["line_item"], "creative_id": r["creative_id"], "content": _load(r["content_json"]),
                 "status": r["status"], "meta_id": r["meta_id"]} for r in self.conn.execute(q + " ORDER BY line_item, creative_id", args)]

    def record_external_id(self, run_id: str, kind: str, meta_id: str, line_item: str = "") -> None:
        with self._txn() as c:
            c.execute("INSERT OR IGNORE INTO external_ids (run_id, kind, line_item, meta_id) VALUES (?,?,?,?)",
                      (run_id, kind, line_item, meta_id))

    def external_ids(self, run_id: str, kind: Optional[str] = None) -> List[dict]:
        q, args = "SELECT kind, line_item, meta_id FROM external_ids WHERE run_id = ?", [run_id]
        if kind:
            q, args = q + " AND kind = ?", args + [kind]
        return [dict(r) for r in self.conn.execute(q, args)]

    # ---- scores (no debriefs here; see store.truth) ----
    def get_score(self, run_id: str, split: str) -> Optional[float]:
        r = self.conn.execute("SELECT score FROM scores WHERE run_id = ? AND split = ?", (run_id, split)).fetchone()
        return None if r is None else r["score"]

    # ---- improvement sessions, playbooks, change log ----
    def start_session(self, name: str, overwrite: bool = False) -> None:
        with self._txn() as c:
            if c.execute("SELECT 1 FROM sessions WHERE name = ?", (name,)).fetchone():
                if not overwrite:
                    raise RuntimeError(f"session {name!r} exists; pick a new --name or pass --overwrite")
                for t in ("playbooks", "improvement"):
                    c.execute(f"DELETE FROM {t} WHERE session = ?", (name,))
                c.execute("DELETE FROM sessions WHERE name = ?", (name,))
            c.execute("INSERT INTO sessions (name, created_at) VALUES (?, ?)", (name, _now()))

    def save_playbook(self, session: str, version: str, text: str, parent: Optional[str] = None,
                      diff: Optional[str] = None, gate_result: Optional[dict] = None,
                      research_urls: Optional[List[str]] = None) -> None:
        with self._txn() as c:
            c.execute("INSERT OR REPLACE INTO playbooks (session, version, parent, text, diff, gate_result_json, research_urls)"
                      " VALUES (?,?,?,?,?,?,?)", (session, version, parent, text, diff, _j(gate_result), _j(research_urls)))

    def record_improvement(self, session: str, entry: dict) -> None:
        """Append one change-log entry (the dict the loop logs: round, event, ...)."""
        with self._txn() as c:
            seq = c.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM improvement WHERE session = ?", (session,)).fetchone()[0]
            c.execute("INSERT INTO improvement (session, seq, round, base_version, candidate_version, stats_json, accepted)"
                      " VALUES (?,?,?,?,?,?,?)",
                      (session, seq, entry.get("round", 0), entry.get("base_version"),
                       entry.get("candidate") or entry.get("version"), json.dumps(entry),
                       None if "accepted" not in entry else int(bool(entry["accepted"]))))

    def finish_session(self, session: str, summary: dict) -> None:
        with self._txn() as c:
            c.execute("UPDATE sessions SET complete = 1, summary_json = ? WHERE name = ?", (json.dumps(summary), session))

    def save_final_eval(self, session: str, result: dict) -> None:
        with self._txn() as c:
            c.execute("UPDATE sessions SET final_eval_json = ? WHERE name = ?", (json.dumps(result), session))

    def session_data(self, session: str) -> Optional[dict]:
        """Everything the results page needs, from the database alone."""
        s = self.conn.execute("SELECT * FROM sessions WHERE name = ?", (session,)).fetchone()
        if s is None:
            return None
        log = [_load(r["stats_json"]) for r in
               self.conn.execute("SELECT stats_json FROM improvement WHERE session = ? ORDER BY seq", (session,))]
        return {"name": session, "complete": bool(s["complete"]), "changelog": log,
                "summary": _load(s["summary_json"]), "final_eval": _load(s["final_eval_json"])}


def _as_text(v: Any) -> Optional[str]:
    if v is None or isinstance(v, str):
        return v
    return json.dumps(v)
