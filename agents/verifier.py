"""Verifier (Done Means Done): writes a definition of done before the first checkpoint, interprets holdout evidence
after the last one. It sees only public observations, never hidden truth."""
from __future__ import annotations

import json
from typing import Callable, Optional

from .models import CallResult, call_model
from .schemas import validate_done_definition, validate_verdict
from .views import compact_observation

DONE_SYSTEM = """You are the Verifier of an ad-campaign team. Before the campaign starts, write a definition of done for the
PLAYBOOK on this campaign: 3-6 concrete, checkable criteria about how budget is allocated and how performance and
incrementality claims must be supported by evidence (e.g. holdout results rather than platform-attributed conversions alone).
Reply with ONE JSON object: {"criteria": ["...", ...], "notes": "..."}"""

VERDICT_SYSTEM = """You are the Verifier of an ad-campaign team. The campaign has finished. Judge the observed evidence against the
definition of done. Be skeptical of platform-attributed conversions; holdout measurements are the only incrementality evidence.
Reply with ONE JSON object: {"verdict": "pass" | "fail" | "inconclusive", "summary": "...", "evidence": ["...", ...]}"""

DEFAULT_CRITERIA = {"criteria": [
    "Budget was split using evidence, not only platform-attributed CPA.",
    "Any line item with suspiciously cheap attributed CPA was tested with a holdout before scaling.",
    "Underperforming or saturated line items had budget reduced.",
], "notes": "default definition of done (model unavailable)"}
DEFAULT_VERDICT = {"verdict": "inconclusive", "summary": "verifier unavailable", "evidence": []}


def define_done(playbook: str, campaign: dict, *, client=None, recorder: Optional[Callable] = None,
                ctx: Optional[dict] = None) -> CallResult:
    messages = [{"role": "system", "content": DONE_SYSTEM},
                {"role": "user", "content": f"PLAYBOOK:\n{playbook}\n\nCAMPAIGN:\n{json.dumps(campaign['capabilities'])}\n"
                                            f"total_budget={campaign['total_budget']} n_checkpoints={campaign['n_checkpoints']}"}]
    return call_model("verifier", messages, client=client, recorder=recorder,
                      schema_check=validate_done_definition, fallback=DEFAULT_CRITERIA, ctx=ctx)


def final_verdict(done_def: dict, obs: dict, *, client=None, recorder: Optional[Callable] = None,
                  ctx: Optional[dict] = None) -> CallResult:
    messages = [{"role": "system", "content": VERDICT_SYSTEM},
                {"role": "user", "content": f"DEFINITION OF DONE:\n{json.dumps(done_def)}\n\n"
                                            f"FINAL OBSERVATION:\n{json.dumps(compact_observation(obs))}"}]
    return call_model("verifier", messages, client=client, recorder=recorder,
                      schema_check=validate_verdict, fallback=DEFAULT_VERDICT, ctx=ctx)
