"""Improver: reads train-failure post-mortems, researches (up to two questions), and returns candidate playbook edits.

Plug-in for improve.loop:  propose_edit(playbook, failures, history) -> dict | None
The result is the first candidate (new_playbook, summary, rationale, sources, ...) plus `alternatives`: the other
candidates, each a single focused edit of the same shape. The loop gates every candidate and accepts at most the best
passing one. The hidden truth in `failures` comes from train campaigns only (improve.loop controls that); the gate,
not this module, decides whether an edit is kept.
"""
from __future__ import annotations

import difflib
import json
import random
from typing import Callable, List, Optional

from improve import research as research_mod

from .models import call_model

SUMMARIZE_AT = 12000  # chars of failure JSON above which the Nano summarizer compresses the context
SIMILAR_TO_REJECTED = 0.6  # an edit whose changed rules match a rejected edit this closely is a repeat
SIMILAR_TO_EACH_OTHER = 0.85  # candidates this alike count as one
MAX_QUESTIONS = 2  # research questions per round
N_CANDIDATES = 3  # candidate edits per round
EXPLORE_TEMPERATURE = 1.0  # explore mode samples (NVIDIA's recommended setting) so retries actually differ
# Explore mode draws one of these at random (seeded by the loop) instead of following the usual diagnosis.
DIRECTIONS = [
    "how much budget to move at each checkpoint, and when to hold instead of moving",
    "a cap on the share of the budget any one line item may hold",
    "pausing a line item whose evidence of incremental value is weak",
    "rotating creative to counter fatigue and rising frequency",
    "running a short holdout and then acting on what it measures",
    "the starting split between prospecting and retargeting before any data exists",
    "moving budget toward line items that are far from saturation",
    "treating retargeting and brand search as likely over-credited by last-click",
    "waiting for enough data before the first reallocation",
    "keeping a minimum budget on every line item so it keeps producing evidence",
]

SUMMARIZER_SYSTEM = """Compress these ad-campaign post-mortems for a playbook editor. Keep: scores, which line items were over- or
under-funded versus the hidden-truth optimum, true incrementality versus what the platform reported, creative fatigue, and
what the agents did or failed to do. Drop raw per-checkpoint tables. Reply with plain text, under 600 words."""

DIAGNOSE_SYSTEM = """You improve the PLAYBOOK of an ad-campaign agent team. You see the worst recent runs (with hidden ground truth,
available for training campaigns only) and the earlier edits already tried. Diagnose the most costly failure pattern, and
name the numbered playbook rule that causes it ("target_rule", e.g. "2"); the rule that decides how budget is moved is as
likely a cause as the rule about which signal to trust. Then ask up to TWO web research questions: the first about
evidence (measuring incrementality, saturation, creative fatigue), the second about what policy or action to take
(how to reallocate budget, when to hold or shift spend) given that evidence.
A rejected edit made the policy worse: diagnose a DIFFERENT failure pattern or a different target rule than the rejected
edits used, and ask different research questions than before. Each run lists "budget_moves": how many times the agents
actually moved money. 0 means they sat idle, which scores like doing nothing; rules the agents cannot act on cause this.
Reply with ONE JSON object: {"diagnosis": "...", "target_rule": "...", "research_questions": ["...", "..."]}"""

EDIT_SYSTEM = """You improve the PLAYBOOK of an ad-campaign agent team. Propose THREE alternative candidate playbooks that fix the
diagnosed failure. Each candidate makes exactly ONE focused change: add, replace or sharpen a single numbered rule, keeping
every other rule, the numbering, and the style (short imperative rules, no code). The three candidates must differ in
mechanism, not in wording: for example one changes the rule the diagnosis names as the cause, one changes the rule that
decides budget moves, one adds a new rule. The playbook must stay a plain numbered policy that works for any campaign; do
not mention specific campaign ids, scores, or hidden-truth numbers. HISTORY shows the exact rule changes already tried. A
rejected edit made the policy worse: do not propose the same idea in other words, and do not retry the same rule with the
same approach.
EXECUTABLE RULES: the agents follow a rule only if they can apply it mechanically to the numbers they see each checkpoint
(spend, attributed conversions, CPA, ROAS, frequency, CTR, creative age, impression share, holdout estimates, budgets).
State exact amounts, thresholds and which line item gives to which ("move 30% of X's budget to Y when ..."), and say what
to do when the condition is not met. A vague or cautious rule ("only after validating", "when appropriate") makes the
agents do nothing, and doing nothing scores worse than a plain reallocation rule. A rule that replaces a budget-moving
rule must still move budget. WORST RUNS and HISTORY show budget moves per run: if an earlier edit made them drop to 0, its
rules were not executable.
Reply with ONE JSON object: {"candidates": [{"new_playbook": "<the FULL revised playbook text>", "summary": "<5-10 words>",
"rationale": "<why this should help>"}, ... three of them]}"""


EXPLORE_SYSTEM = """You improve the PLAYBOOK of an ad-campaign agent team. The usual fixes have all been tried and rejected (see
HISTORY), so this round is an exploration: write NEW rules from the DIRECTION you are given, not from the earlier
diagnosis. Propose THREE alternative candidate playbooks. Each makes exactly ONE focused change and may add, replace or
completely rewrite any single numbered rule, including the rule that decides how budget is moved; keep the other rules,
the numbering and the style (short imperative rules, no code). The playbook must stay a plain numbered policy that works
for any campaign; do not mention campaign ids, scores or hidden-truth numbers. The three candidates must differ in
mechanism, not wording, and none may resemble a rejected edit in HISTORY.
EXECUTABLE RULES: the agents follow a rule only if they can apply it mechanically to the numbers they see each checkpoint
(spend, attributed conversions, CPA, ROAS, frequency, CTR, creative age, impression share, holdout estimates, budgets).
State exact amounts, thresholds and which line item gives to which ("move 30% of X's budget to Y when ..."), and say what
to do when the condition is not met. A vague or cautious rule makes the agents do nothing, and doing nothing scores worse
than a plain reallocation rule. A rule that replaces a budget-moving rule must still move budget.
Reply with ONE JSON object: {"candidates": [{"new_playbook": "<the FULL revised playbook text>", "summary": "<5-10 words>",
"rationale": "<why this should help>"}, ... three of them]}"""


def _check_edit(raw) -> dict:
    if not isinstance(raw, dict) or not isinstance(raw.get("new_playbook"), str) or not raw["new_playbook"].strip():
        raise ValueError("reply needs a non-empty 'new_playbook' string")
    return {"new_playbook": raw["new_playbook"], "summary": str(raw.get("summary", "")).strip(),
            "rationale": str(raw.get("rationale", "")).strip()}


def _check_edits(raw) -> List[dict]:
    """A list of valid edits from {"candidates": [...]} or from a single edit object."""
    items = raw["candidates"] if isinstance(raw, dict) and "candidates" in raw else [raw]
    if not isinstance(items, list):
        raise ValueError("'candidates' must be a list")
    out = []
    for item in items[:N_CANDIDATES]:
        try:
            out.append(_check_edit(item))
        except ValueError:
            continue
    if not out:
        raise ValueError("reply needs at least one candidate with a non-empty 'new_playbook'")
    return out


def _check_diagnosis(raw) -> dict:
    qs = raw.get("research_questions") or raw.get("research_question") if isinstance(raw, dict) else None
    qs = [qs] if isinstance(qs, str) else (qs or [])
    qs = [str(q).strip() for q in qs if str(q).strip()][:MAX_QUESTIONS]
    if not qs:
        raise ValueError("reply needs 'research_questions'")
    return {"diagnosis": str(raw.get("diagnosis", "")), "target_rule": str(raw.get("target_rule", "")).strip(),
            "research_questions": qs}


def _added_lines(diff: Optional[str]) -> List[str]:
    return [l[1:].strip() for l in (diff or "").splitlines() if l.startswith("+") and not l.startswith("+++")]


def _added_text(old: str, new: str) -> str:
    return " ".join(_added_lines("".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True)))))


def _repeats_rejected(old: str, new: str, history: list) -> Optional[dict]:
    """The rejected history entry whose added rules this edit closely resembles, if any."""
    mine = _added_text(old, new)
    for h in history:
        if h.get("accepted") is False and mine:
            past = " ".join(_added_lines(h.get("diff")))
            if past and difflib.SequenceMatcher(None, mine, past).ratio() >= SIMILAR_TO_REJECTED:
                return h
    return None


def _novel(playbook: str, edits: List[dict], history: list) -> tuple:
    """(edits that are not repeats of a rejected edit or of each other, a rejected entry one of them resembles)."""
    keep, kept_text, repeat = [], [], None
    for e in edits:
        h = _repeats_rejected(playbook, e["new_playbook"], history)
        if h:
            repeat = repeat or h
            continue
        mine = _added_text(playbook, e["new_playbook"])
        if mine and any(difflib.SequenceMatcher(None, mine, t).ratio() >= SIMILAR_TO_EACH_OTHER for t in kept_text):
            continue
        keep.append(e)
        kept_text.append(mine)
    return keep, repeat


def propose_edit(playbook: str, failures: list, history: list, *, client=None,
                 research_fn: Callable = research_mod.research, explore: Optional[dict] = None) -> Optional[dict]:
    """`explore` ({"seed": str}) switches to exploration: a seeded random DIRECTION replaces the diagnosis."""
    calls: List[dict] = []

    def recorder(rec: dict) -> None:  # prompts stay out of the change log; they are large
        calls.append({k: rec.get(k) for k in ("role", "model", "temperature", "error", "fallback_used", "attempts")})

    context = json.dumps(failures, default=str)
    if len(context) > SUMMARIZE_AT:
        res = call_model("summarizer", [{"role": "system", "content": SUMMARIZER_SYSTEM},
                                        {"role": "user", "content": context[:60000]}],
                         client=client, recorder=recorder, expect="text", fallback=None, max_retries=2)
        context = res.parsed if res.ok else context[:SUMMARIZE_AT]
    past = json.dumps([{**{k: h.get(k) for k in ("round", "summary", "accepted", "mean_diff", "reason")},
                        "rule_changes": _added_lines(h.get("diff"))} for h in history])
    user = f"PLAYBOOK:\n{playbook}\n\nWORST RUNS:\n{context}\n\nHISTORY (earlier edits):\n{past}"

    sources: List[str] = []
    diag, system, temperature, direction = None, EDIT_SYSTEM, None, None
    if explore:
        direction = random.Random(explore["seed"]).choice(DIRECTIONS)
        system, temperature = EXPLORE_SYSTEM, EXPLORE_TEMPERATURE
        q = f"What does the evidence say about {direction} when splitting an ad budget across Meta and Google line items?"
        r = research_fn(q)
        sources = [s["url"] for s in r.get("sources") or [] if s.get("url")]
        user += f"\n\nDIRECTION:\n{direction}\n\nRESEARCH ({q}):\n{research_mod.format_for_prompt(r)}"
    else:
        diag = call_model("improver", [{"role": "system", "content": DIAGNOSE_SYSTEM}, {"role": "user", "content": user}],
                          client=client, recorder=recorder, schema_check=_check_diagnosis, fallback=None).parsed
    if diag:  # up to MAX_QUESTIONS research calls per round; their sources pass through unchanged
        blocks = []
        for q in diag["research_questions"]:
            r = research_fn(q)
            blocks.append(f"RESEARCH ({q}):\n{research_mod.format_for_prompt(r)}")
            sources += [s["url"] for s in r.get("sources") or [] if s.get("url") and s["url"] not in sources]
        target = f"\nTARGET RULE: {diag['target_rule']}" if diag["target_rule"] else ""
        user += f"\n\nDIAGNOSIS:\n{diag['diagnosis']}{target}\n\n" + "\n\n".join(blocks)

    def ask(extra: str = "") -> list:
        res = call_model("improver", [{"role": "system", "content": system}, {"role": "user", "content": user + extra}],
                         client=client, recorder=recorder, schema_check=_check_edits, fallback=None,
                         temperature=temperature)
        if not res.ok:
            raise RuntimeError(f"improver returned no valid edit: {res.error}")  # loop logs improver_error and moves on
        return res.parsed

    edits = ask()
    good, repeat = _novel(playbook, edits, history)
    if not good and repeat:  # every candidate repeated a rejected edit: one retry, naming it
        edits = ask(f"\n\nAll of your candidates repeat the rejected edit from round {repeat['round']} "
                    f"({repeat.get('summary')!r}). Propose three clearly different changes.")
        good, repeat = _novel(playbook, edits, history)
        if not good:
            raise RuntimeError("improver kept repeating a rejected edit; round skipped before the gate")
    first, rest = good[0], good[1:]
    common = {"sources": sources, "improver_calls": calls, "diagnosis": diag["diagnosis"] if diag else None,
              **({"explore": {"seed": explore["seed"], "direction": direction}} if explore else {})}
    return {**first, **common, "alternatives": [{**e, **common} for e in rest]}
