"""Ad campaign simulator with hidden ground truth. Settings and trap sources: sim_calibration.md.

Mechanisms (all 6 traps are settings of these 4):
- incrementality ratio r: true incremental = r * attributed (traps 1-3)
- Hill saturation, S=1: attributed(spend) = A_max * s / (s + K) (trap 4)
- fatigue decay d: response *= (1 - d) ** weeks_since_rotation (trap 5)
- negative-binomial noise, var = 1.5 * mean, on observed counts and holdout tests (trap 6)
"""
from __future__ import annotations

import copy
import zlib
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import numpy as np

from .interface import (
    ACTION_TYPES, CHANNEL_OF, HOLDOUT_MAX, HOLDOUT_MIN, LINE_ITEMS, N_CHECKPOINTS, SPLITS,
)

NB_VAR_RATIO = 1.5
PRODUCTS = [
    "running shoes", "skincare serum", "coffee subscription", "yoga mats", "phone cases",
    "pet food", "wireless earbuds", "backpacks", "protein powder", "candles", "sunglasses",
    "kitchen knives", "board games", "water bottles", "bedding sets", "watches",
]

# (attributed CPA at spend=K, incrementality r, K as share of monthly budget, weekly fatigue decay)
RANGES = {
    "meta_prospecting": dict(cpa=(35, 60), r=(0.85, 1.20), k=(0.4, 0.8), d=(0.05, 0.15)),
    "meta_retargeting": dict(cpa=(10, 20), r=(0.08, 0.30), k=(0.05, 0.15), d=(0.15, 0.30)),
    "google_nonbrand": dict(cpa=(50, 90), r=(0.40, 0.65), k=(0.2, 0.5), d=(0.0, 0.05)),
    # Train/validation: brand defended against competitors, genuinely incremental (Edmunds-like).
    "google_brand": dict(cpa=(8, 20), r=(0.60, 0.90), k=(0.03, 0.10), d=(0.0, 0.05)),
}
# Test-only trap variations: brand over-credit (eBay) and faster creative fatigue.
TEST_OVERRIDES = {
    "google_brand": dict(r=(0.00, 0.25)),
    "meta_prospecting": dict(d=(0.15, 0.25)),
    "meta_retargeting": dict(d=(0.25, 0.40)),
}
SPLIT_SALT = {"train": 101, "validation": 202, "test": 303}


@dataclass
class Campaign:
    id: str
    split: str
    product: str
    total_budget: float
    truth: Dict[str, dict] = field(repr=False)  # hidden; never pass to agents

    @property
    def weekly_budget(self) -> float:
        return self.total_budget / N_CHECKPOINTS

    def public(self) -> dict:
        return {
            "id": self.id, "product": self.product, "total_budget": self.total_budget,
            "weekly_budget": self.weekly_budget, "n_weeks": N_CHECKPOINTS,
            "line_items": list(LINE_ITEMS),
        }


def generate_campaigns(n: int, split: str, seed: int = 0) -> List[Campaign]:
    if split not in SPLITS:
        raise ValueError(f"split must be one of {SPLITS}")
    rng = np.random.default_rng([seed, SPLIT_SALT[split]])
    out = []
    for i in range(n):
        budget = float(round(rng.uniform(20_000, 100_000), -2))
        truth = {}
        for li in LINE_ITEMS:
            rg = dict(RANGES[li])
            if split == "test":
                rg.update(TEST_OVERRIDES.get(li, {}))
            cpa = rng.uniform(*rg["cpa"])
            k_week = rng.uniform(*rg["k"]) * budget / N_CHECKPOINTS
            truth[li] = {
                "attr_cpa": float(cpa),
                "r": float(rng.uniform(*rg["r"])),
                "K": float(k_week),
                "decay": float(rng.uniform(*rg["d"])),
                "A_max": float(2 * k_week / cpa),  # so CPA at spend=K equals attr_cpa
            }
        out.append(Campaign(f"{split}-{seed}-{i}", split, PRODUCTS[rng.integers(len(PRODUCTS))], budget, truth))
    return out


def _attributed(t: dict, spend: float, age: int) -> float:
    if spend <= 0:
        return 0.0
    return t["A_max"] * spend / (spend + t["K"]) * (1 - t["decay"]) ** age


def best_weekly_incremental(campaign: Campaign, steps: int = 2000) -> tuple:
    """Oracle: greedy split of the weekly budget over concave curves, fresh creative, no holdouts."""
    spend = {li: 0.0 for li in LINE_ITEMS}
    inc = lambda li, s: campaign.truth[li]["r"] * _attributed(campaign.truth[li], s, 0)
    dx = campaign.weekly_budget / steps
    for _ in range(steps):
        li = max(LINE_ITEMS, key=lambda l: inc(l, spend[l] + dx) - inc(l, spend[l]))
        spend[li] += dx
    return sum(inc(li, spend[li]) for li in LINE_ITEMS), spend


def _nb(rng: np.random.Generator, mean: float) -> int:
    """Negative binomial with var = NB_VAR_RATIO * mean (gamma-Poisson mixture)."""
    if mean <= 0:
        return 0
    k = mean / (NB_VAR_RATIO - 1)
    return int(rng.poisson(rng.gamma(k, mean / k)))


class Simulator:
    def __init__(self, campaign: Campaign, seed: int = 0):
        self.campaign = campaign
        self.rng = np.random.default_rng([seed, zlib.crc32(campaign.id.encode())])
        w = campaign.weekly_budget
        self.budgets = {li: w / len(LINE_ITEMS) for li in LINE_ITEMS}
        self.holdouts: Dict[str, float] = {}
        self.age = {li: 0 for li in LINE_ITEMS}
        self.week = 0
        self.history: List[dict] = []
        self.trace: List[dict] = []
        self.achieved = 0.0  # expected true incremental conversions (noise-free)
        self._plan_applied = False
        self._ho_acc: Dict[str, dict] = {}

    # ---- agent-facing API ----
    @property
    def done(self) -> bool:
        return self.week >= N_CHECKPOINTS

    def apply_plan(self, plan: dict) -> dict:
        if self.week > 0 or self._plan_applied:
            return self._log_result("plan", plan, False, "plan can only be applied once, before week 1")
        alloc = {li: max(0.0, float(v)) for li, v in (plan.get("allocations") or {}).items() if li in LINE_ITEMS}
        total = sum(alloc.values())
        if total <= 0:
            return self._log_result("plan", plan, False, "allocations must contain positive shares for known line items")
        w = self.campaign.weekly_budget
        self.budgets = {li: w * alloc.get(li, 0.0) / total for li in LINE_ITEMS}
        for li, share in (plan.get("holdouts") or {}).items():
            if li in LINE_ITEMS and share:
                self._start_holdout(li, float(share))
        self._plan_applied = True
        return self._log_result("plan", plan, True, None)

    def observe(self) -> dict:
        return copy.deepcopy({
            "campaign": self.campaign.public(),
            "week": self.week,
            "weeks_left": N_CHECKPOINTS - self.week,
            "budgets": {li: round(v, 2) for li, v in self.budgets.items()},
            "holdouts": dict(self.holdouts),
            "history": self.history,
        })

    def act(self, action: dict) -> dict:
        try:
            err = self._apply(action)
        except (KeyError, TypeError, ValueError) as e:
            err = f"malformed action: {e}"
        return self._log_result("action", action, err is None, err)

    def step(self) -> None:
        if self.done:
            raise RuntimeError("campaign finished")
        self.week += 1
        week = {"week": self.week, "line_items": {}}
        for li in LINE_ITEMS:
            t, s, h = self.campaign.truth[li], self.budgets[li], self.holdouts.get(li, 0.0)
            age = self.age[li]
            attr = _attributed(t, s, age)
            inc = t["r"] * attr
            self.achieved += (1 - h) * inc
            obs_attr = _nb(self.rng, (1 - h) * attr)
            row = {
                "spend": round(s, 2),
                "attributed_conversions": obs_attr,
                "cpa": round(s / obs_attr, 2) if obs_attr else None,
                "creative_age_weeks": age,
                "holdout": self._measure_holdout(li, attr, inc, obs_attr) if h else None,
            }
            week["line_items"][li] = row
            if s > 0:
                self.age[li] += 1
        self.history.append(week)
        self.trace.append({"week": self.week, "kind": "step", "observed": week})

    # ---- hidden truth ----
    def debrief(self) -> dict:
        """Hidden truth for post-mortems. Use on TRAIN campaigns only."""
        best, best_spend = best_weekly_incremental(self.campaign)
        return {
            "campaign": self.campaign.public(),
            "score": score(self),
            "achieved_incremental": round(self.achieved, 1),
            "best_possible_incremental": round(best * N_CHECKPOINTS, 1),
            "line_items": {li: {
                "true_incrementality": round(t["r"], 3),
                "attributed_cpa_at_half_saturation": round(t["attr_cpa"], 2),
                "true_incremental_cpa_at_half_saturation": round(t["attr_cpa"] / t["r"], 2) if t["r"] > 0 else None,
                "half_saturation_weekly_spend": round(t["K"], 0),
                "weekly_fatigue_decay": round(t["decay"], 3),
                "optimal_weekly_spend": round(best_spend[li], 0),
                "avg_weekly_spend": round(np.mean([w["line_items"][li]["spend"] for w in self.history]), 0) if self.history else 0,
            } for li, t in self.campaign.truth.items()},
        }

    # ---- internals ----
    def _apply(self, a: dict) -> Optional[str]:
        typ = a.get("type")
        if typ not in ACTION_TYPES:
            return f"unknown action type {typ!r}; expected one of {ACTION_TYPES}"
        if self.done:
            return "campaign finished"
        if typ == "shift_budget":
            src, dst, amt = a["from"], a["to"], float(a["amount"])
            if src not in LINE_ITEMS or dst not in LINE_ITEMS or src == dst:
                return "from/to must be two different known line items"
            if amt <= 0:
                return "amount must be positive"
            amt = min(amt, self.budgets[src])
            self.budgets[src] -= amt
            self.budgets[dst] += amt
            return None
        li = a["line_item"]
        if li not in LINE_ITEMS:
            return f"unknown line item {li!r}"
        if typ == "pause":
            freed, self.budgets[li] = self.budgets[li], 0.0
            active = [l for l in LINE_ITEMS if self.budgets[l] > 0]
            if not active:
                self.budgets[li] = freed
                return "cannot pause the last active line item"
            tot = sum(self.budgets[l] for l in active)
            for l in active:
                self.budgets[l] += freed * self.budgets[l] / tot
            return None
        if typ == "rotate_creative":
            self.age[li] = 0
            return None
        share = float(a["share"])
        if share == 0:
            self.holdouts.pop(li, None)
            self._ho_acc.pop(li, None)
            return None
        return self._start_holdout(li, share)

    def _start_holdout(self, li: str, share: float) -> Optional[str]:
        if not HOLDOUT_MIN <= share <= HOLDOUT_MAX:
            return f"holdout share must be 0 or in [{HOLDOUT_MIN}, {HOLDOUT_MAX}]"
        if self.holdouts.get(li) != share:
            self._ho_acc[li] = {"inc": 0.0, "var": 0.0, "attr": 0.0, "weeks": 0}
        self.holdouts[li] = share
        return None

    def _measure_holdout(self, li: str, attr: float, inc: float, obs_attr: int) -> dict:
        """Treatment vs control comparison, scaled to the full audience; noise from NB counts."""
        h = self.holdouts[li]
        base = max(attr - inc, 0.1 * attr)  # would-convert-anyway conversions in this audience
        t_mean, c_mean = (1 - h) * (base + inc), h * base
        inc_hat = _nb(self.rng, t_mean) / (1 - h) - _nb(self.rng, c_mean) / h
        var = NB_VAR_RATIO * (t_mean / (1 - h) ** 2 + c_mean / h ** 2)
        acc = self._ho_acc[li]
        acc["inc"] += inc_hat
        acc["var"] += var
        acc["attr"] += obs_attr / (1 - h)
        acc["weeks"] += 1
        denom = max(acc["attr"], 1.0)
        est, half = acc["inc"] / denom, 1.96 * np.sqrt(acc["var"]) / denom
        return {
            "share": h, "weeks": acc["weeks"],
            "incremental_conversions_est": round(acc["inc"], 1),
            "incremental_share_est": round(est, 3),
            "ci95": [round(est - half, 3), round(est + half, 3)],
            "conclusive": bool(est - half > 0 or est + half < 0),
        }

    def _log_result(self, kind: str, payload: dict, ok: bool, err: Optional[str]) -> dict:
        self.trace.append({"week": self.week, "kind": kind, "payload": copy.deepcopy(payload), "ok": ok, "error": err})
        return {"ok": ok, "error": err}


def score(sim: Simulator) -> float:
    """True incremental conversions achieved / best possible for this budget. Code only, hidden truth."""
    best, _ = best_weekly_incremental(sim.campaign)
    return float(sim.achieved / (best * N_CHECKPOINTS)) if best > 0 else 0.0
