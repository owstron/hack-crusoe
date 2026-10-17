"""Improver: reads train-failure post-mortems, optionally researches once, and returns ONE revised playbook.

Plug-in for improve.loop:  propose_edit(playbook, failures, history) -> dict | None
The hidden truth in `failures` comes from train campaigns only (improve.loop controls that); the gate, not this
module, decides whether the edit is kept.
"""
from __future__ import annotations

import json
from typing import Callable, List, Optional

from improve import research as research_mod

from .models import call_model

SUMMARIZE_AT = 12000  # chars of failure JSON above which the Nano summarizer compresses the context

SUMMARIZER_SYSTEM = """Compress these ad-campaign post-mortems for a playbook editor. Keep: scores, which line items were over- or
under-funded versus the hidden-truth optimum, true incrementality versus what the platform reported, creative fatigue, and
what the agents did or failed to do. Drop raw per-checkpoint tables. Reply with plain text, under 600 words."""

DIAGNOSE_SYSTEM = """You improve the PLAYBOOK of an ad-campaign agent team. You see the worst recent runs (with hidden ground truth,
available for training campaigns only) and the earlier edits already tried. Diagnose the most costly failure pattern and
ask ONE web research question that would help fix it (about measuring incrementality, saturation, creative fatigue, etc.).
Reply with ONE JSON object: {"diagnosis": "...", "research_question": "..."}"""

EDIT_SYSTEM = """You improve the PLAYBOOK of an ad-campaign agent team. Make exactly ONE focused change to the playbook that fixes the
diagnosed failure: add, replace or sharpen a single numbered rule. Keep every other rule, the numbering, and the style
(short imperative rules, no code). The playbook must stay a plain numbered policy that works for any campaign; do not
mention specific campaign ids, scores, or hidden-truth numbers. Do not repeat edits listed in HISTORY, especially rejected ones.
Every rule must be EXECUTABLE by the agents in a 4-checkpoint campaign:
- Name an action they have: the initial plan (budget shares, holdouts), shift_budget (with an amount or share),
  pause, rotate_creative, set_holdout (share 0.05-0.5, or 0 to end it).
- Say WHEN to take it, using only data the agents observe: reported CPA, ROAS, CTR, frequency (Meta),
  search impression share and share lost to budget (Google), creative age, and holdout estimates
  (incremental_share_est with a 95% CI, often inconclusive on small line items).
- Give a decision threshold that still works when evidence is noisy or inconclusive (e.g. act on the point
  estimate). Holdouts cost conversions: say which line items get one and when it ends.
- Never require verification, documentation, sign-off, or evidence the agents cannot produce.
Reply with ONE JSON object: {"new_playbook": "<the FULL revised playbook text>", "summary": "<5-10 words>", "rationale": "<why this should help>"}"""


def _check_edit(raw) -> dict:
    if not isinstance(raw, dict) or not isinstance(raw.get("new_playbook"), str) or not raw["new_playbook"].strip():
        raise ValueError("reply needs a non-empty 'new_playbook' string")
    return {"new_playbook": raw["new_playbook"], "summary": str(raw.get("summary", "")).strip(),
            "rationale": str(raw.get("rationale", "")).strip()}


def _check_diagnosis(raw) -> dict:
    if not isinstance(raw, dict) or not str(raw.get("research_question", "")).strip():
        raise ValueError("reply needs a 'research_question'")
    return {"diagnosis": str(raw.get("diagnosis", "")), "research_question": str(raw["research_question"]).strip()}


def propose_edit(playbook: str, failures: list, history: list, *, client=None,
                 research_fn: Callable = research_mod.research) -> Optional[dict]:
    calls: List[dict] = []

    def recorder(rec: dict) -> None:  # prompts stay out of the change log; they are large
        calls.append({k: rec.get(k) for k in ("role", "model", "temperature", "error", "fallback_used", "attempts")})

    context = json.dumps(failures, default=str)
    if len(context) > SUMMARIZE_AT:
        res = call_model("summarizer", [{"role": "system", "content": SUMMARIZER_SYSTEM},
                                        {"role": "user", "content": context[:60000]}],
                         client=client, recorder=recorder, expect="text", fallback=None, max_retries=2)
        context = res.parsed if res.ok else context[:SUMMARIZE_AT]
    past = json.dumps([{k: h.get(k) for k in ("round", "summary", "accepted", "mean_diff", "reason")} for h in history])
    user = f"PLAYBOOK:\n{playbook}\n\nWORST RUNS:\n{context}\n\nHISTORY (earlier edits):\n{past}"

    diag = call_model("improver", [{"role": "system", "content": DIAGNOSE_SYSTEM}, {"role": "user", "content": user}],
                      client=client, recorder=recorder, schema_check=_check_diagnosis, fallback=None).parsed
    findings, sources = "", []
    if diag:  # at most one research call per round; its sources pass through unchanged
        r = research_fn(diag["research_question"])
        findings = research_mod.format_for_prompt(r)
        sources = [s["url"] for s in r.get("sources") or [] if s.get("url")]
        user += f"\n\nDIAGNOSIS:\n{diag['diagnosis']}\n\nRESEARCH ({diag['research_question']}):\n{findings}"
    res = call_model("improver", [{"role": "system", "content": EDIT_SYSTEM}, {"role": "user", "content": user}],
                     client=client, recorder=recorder, schema_check=_check_edit, fallback=None)
    if not res.ok:
        raise RuntimeError(f"improver returned no valid edit: {res.error}")  # loop logs improver_error and moves on
    edit = res.parsed
    return {**edit, "sources": sources, "improver_calls": calls,
            "diagnosis": diag["diagnosis"] if diag else None}
