"""Ad campaign simulator with hidden ground truth. Settings and trap sources: sim_calibration.md.

Mechanisms (all 6 traps are settings of these 4):
- incrementality ratio r: true incremental = r * attributed (traps 1-3)
- Hill saturation, S=1: attributed(spend) = A_max * s / (s + K) (trap 4)
- fatigue decay d: response *= (1 - d) ** checkpoints_since_rotation (trap 5)
- negative-binomial noise, var = 1.5 * mean, on observed counts and holdout tests (trap 6)
"""
from __future__ import annotations

import copy
import datetime as dt
import zlib
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import numpy as np

from .interface import (
    HOLDOUT_MAX, HOLDOUT_MIN, LINE_ITEMS, N_CHECKPOINTS, SCHEMA_VERSION, SPLITS,
    check_action, check_plan, check_spend,
)

NB_VAR_RATIO = 1.5
PRODUCTS = [
    "running shoes", "skincare serum", "coffee subscription", "yoga mats", "phone cases",
    "pet food", "wireless earbuds", "backpacks", "protein powder", "candles", "sunglasses",
    "kitchen knives", "board games", "water bottles", "bedding sets", "watches",
]

# (attributed CPA at spend=K, incrementality r, K as share of monthly budget, fatigue decay per checkpoint)
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

# Delivery metrics, shaped like a Meta Insights / Google Ads row (sim_calibration.md section 2).
# Meta: CPM and CTR around Triple Whale e-commerce medians ($15.06, 2.39%); retargeting CPM/CTR higher
# (warm audience; our assumption). Google: CPC and CTR around WordStream ($5.26, 6.66%); brand terms
# are cheap with high CTR (our assumption). Conversion rate is not set directly: it falls out of
# conversions / clicks and lands near the benchmarks (Meta ~1.4%, Google non-brand ~7.5%).
DELIVERY = {
    "meta_prospecting": dict(cpm=(11.5, 21.8), ctr=(0.019, 0.030)),
    "meta_retargeting": dict(cpm=(15.0, 30.0), ctr=(0.020, 0.035)),
    "google_nonbrand": dict(cpc=(3.5, 7.5), ctr=(0.045, 0.085)),
    "google_brand": dict(cpc=(1.0, 3.0), ctr=(0.15, 0.30)),
}
# The simulator's line-item catalog: each line item declares its channel (published in capabilities).
SIM_CHANNELS = {
    "meta_prospecting": "meta", "meta_retargeting": "meta",
    "google_nonbrand": "google", "google_brand": "google",
}
# Fields a platform does not report (distinct from a transient None such as "no spend this period").
UNAVAILABLE = {
    "meta": ["search_impression_share", "search_budget_lost_impression_share", "search_rank_lost_impression_share"],
    "google": ["reach", "frequency"],
}
PERIOD_DAYS = 7
PERIOD_EPOCH = dt.date(2026, 1, 5)  # synthetic calendar: checkpoint i covers 7 days from here
# What the platform credits: each platform's default. Meta: 7-day click / 1-day view. Google Ads:
# data-driven attribution is the default for new conversion actions (GOOGLE_SEARCH_ATTRIBUTION_DATA_DRIVEN).
ATTRIBUTION = {
    "meta_prospecting": "7d_click_1d_view", "meta_retargeting": "7d_click_1d_view",
    "google_nonbrand": "data_driven", "google_brand": "data_driven",
}
# Google search impression share: the share of eligible searches you showed for, Google's saturation
# signal (Meta's is frequency). The rest is lost to ad rank (a fixed ceiling) or to budget.
SEARCH_RANK_LOST = {"google_nonbrand": (0.15, 0.35), "google_brand": (0.02, 0.08)}
FREQ_AT_K = 1 / (1 - np.exp(-1))  # per-period frequency at half-saturation spend (~1.58)
CTR_FREQ_EXPONENT = 0.43           # click likelihood ~ frequency^-0.43 (Meta creative fatigue analysis [B])


@dataclass
class Campaign:
    id: str
    split: str
    product: str
    total_budget: float
    truth: Dict[str, dict] = field(repr=False)  # hidden; never pass to agents
    avg_order_value: float = 80.0
    max_total_spend: Optional[float] = None   # default: total_budget
    max_period_spend: Optional[float] = None  # default: period_budget

    @property
    def period_budget(self) -> float:
        return self.total_budget / N_CHECKPOINTS

    def public(self) -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "id": self.id, "product": self.product, "total_budget": self.total_budget,
            "period_budget": self.period_budget, "n_checkpoints": N_CHECKPOINTS,
            "max_total_spend": self.total_budget if self.max_total_spend is None else self.max_total_spend,
            "max_period_spend": self.period_budget if self.max_period_spend is None else self.max_period_spend,
            "avg_order_value": self.avg_order_value,
            "line_items": list(LINE_ITEMS),
            "capabilities": {
                "line_items": {li: {"channel": SIM_CHANNELS[li]} for li in LINE_ITEMS},
                "holdouts": True,
                "attribution": dict(ATTRIBUTION),
            },
        }


def generate_campaigns(n: int, split: str, seed: int = 0) -> List[Campaign]:
    if split not in SPLITS:
        raise ValueError(f"split must be one of {SPLITS}")
    rng = np.random.default_rng([seed, SPLIT_SALT[split]])
    rng_delivery = np.random.default_rng([seed, SPLIT_SALT[split], 1])  # separate stream: core truth unchanged
    rng_search = np.random.default_rng([seed, SPLIT_SALT[split], 2])
    out = []
    for i in range(n):
        budget = float(round(rng.uniform(20_000, 100_000), -2))
        truth = {}
        for li in LINE_ITEMS:
            rg = dict(RANGES[li])
            if split == "test":
                rg.update(TEST_OVERRIDES.get(li, {}))
            cpa = rng.uniform(*rg["cpa"])
            k_period = rng.uniform(*rg["k"]) * budget / N_CHECKPOINTS
            truth[li] = {
                "attr_cpa": float(cpa),
                "r": float(rng.uniform(*rg["r"])),
                "K": float(k_period),
                "decay": float(rng.uniform(*rg["d"])),
                "A_max": float(2 * k_period / cpa),  # so CPA at spend=K equals attr_cpa
            }
            dv = DELIVERY[li]
            ctr0 = float(rng_delivery.uniform(*dv["ctr"]))
            if "cpm" in dv:
                cpm = float(rng_delivery.uniform(*dv["cpm"]))
                # audience size such that per-period frequency at spend K is FREQ_AT_K
                truth[li].update(cpm=cpm, ctr0=ctr0, audience=k_period / cpm * 1000)
            else:
                cpc = float(rng_delivery.uniform(*dv["cpc"]))
                # eligible searches per period such that impression share at spend K is ~63% of the rank ceiling
                truth[li].update(cpc=cpc, ctr0=ctr0, eligible=k_period / cpc / ctr0,
                                 rank_lost=float(rng_search.uniform(*SEARCH_RANK_LOST[li])))
        aov = float(round(rng_delivery.uniform(60, 120), 2))
        out.append(Campaign(f"{split}-{seed}-{i}", split, PRODUCTS[rng.integers(len(PRODUCTS))], budget, truth, aov))
    return out


def _delivery(rng: np.random.Generator, t: dict, spend: float, age: int, conversions: int, aov: float) -> dict:
    """Impressions, reach, frequency, clicks, CTR, CPM, CPC, purchase value, ROAS for one period.
    Meta: frequency rises as spend saturates the audience; CTR falls with frequency and creative age.
    Google search: no reach/frequency; impression share rises toward the rank ceiling as spend grows;
    CTR falls only with (small) creative fatigue."""
    search = "cpm" not in t
    if spend <= 0:
        return {"impressions": 0, "reach": None if search else 0, "frequency": None, "clicks": 0,
                "ctr": None, "cpm": None, "cpc": None, "purchase_value": 0.0, "roas": None,
                "search_impression_share": 0.0 if search else None,
                "search_budget_lost_impression_share": round(1 - t["rank_lost"], 3) if search else None,
                "search_rank_lost_impression_share": round(t["rank_lost"], 3) if search else None}
    fatigue = (1 - t["decay"]) ** age
    imp_share = budget_lost = None
    if not search:
        imps = int(rng.poisson(spend / t["cpm"] * 1000))
        n = t["audience"]
        reach = int(round(n * (1 - np.exp(-imps / n))))
        freq = imps / reach if reach else None
        ctr = t["ctr0"] * fatigue * (max(freq or 0, FREQ_AT_K) / FREQ_AT_K) ** -CTR_FREQ_EXPONENT
    else:
        ctr = t["ctr0"] * fatigue
        imps = int(rng.poisson(spend / t["cpc"] / ctr))
        reach, freq = None, None
        ceiling = 1 - t["rank_lost"]
        imp_share = ceiling * (1 - np.exp(-imps / t["eligible"]))
        budget_lost = ceiling - imp_share
    clicks = _nb(rng, imps * ctr)
    value = round(conversions * aov * float(np.clip(rng.normal(1, 0.05), 0.8, 1.2)), 2)
    return {
        "impressions": imps, "reach": reach, "frequency": round(freq, 2) if freq else None,
        "clicks": clicks, "ctr": round(100 * clicks / imps, 2) if imps else None,  # percent, as Meta reports it
        "cpm": round(1000 * spend / imps, 2) if imps else None,
        "cpc": round(spend / clicks, 2) if clicks else None,
        "purchase_value": value, "roas": round(value / spend, 2),
        # fractions, as the Google Ads API reports them (0.92 = 92%); None for Meta
        "search_impression_share": round(imp_share, 3) if search else None,
        "search_budget_lost_impression_share": round(budget_lost, 3) if search else None,
        "search_rank_lost_impression_share": round(t["rank_lost"], 3) if search else None,
    }


def _attributed(t: dict, spend: float, age: int) -> float:
    if spend <= 0:
        return 0.0
    return t["A_max"] * spend / (spend + t["K"]) * (1 - t["decay"]) ** age


def best_period_incremental(campaign: Campaign, steps: int = 2000) -> tuple:
    """Oracle: greedy split of the period budget over concave curves, fresh creative, no holdouts."""
    spend = {li: 0.0 for li in LINE_ITEMS}
    inc = lambda li, s: campaign.truth[li]["r"] * _attributed(campaign.truth[li], s, 0)
    dx = campaign.period_budget / steps
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
        self.rng_delivery = np.random.default_rng([seed, zlib.crc32(campaign.id.encode()), 1])
        self._public = campaign.public()
        w = campaign.period_budget
        self.budgets = {li: w / len(LINE_ITEMS) for li in LINE_ITEMS}
        self.holdouts: Dict[str, float] = {}
        self.age = {li: 0 for li in LINE_ITEMS}
        self.checkpoint = 0
        self.history: List[dict] = []
        self.trace: List[dict] = []
        self.achieved = 0.0  # expected true incremental conversions (noise-free)
        self.spent = 0.0     # actual spend to date, for the lifetime cap
        self._plan_applied = False
        self._ho_acc: Dict[str, dict] = {}
        # creative registry: {li: [{"id", "status"}]}. Implicit unlimited pool unless the plan supplies creatives.
        self._explicit_creatives = False
        self._creatives: Dict[str, List[dict]] = {li: [{"id": f"{li}-c0", "status": "live"}] for li in LINE_ITEMS}

    # ---- agent-facing API ----
    @property
    def done(self) -> bool:
        return self.checkpoint >= N_CHECKPOINTS

    @property
    def status(self) -> str:
        return "done" if self.done else "ready"

    def apply_plan(self, plan: dict) -> dict:
        if self.checkpoint > 0 or self._plan_applied:
            return self._log_result("plan", plan, False, "plan can only be applied once, before checkpoint 1")
        err = check_plan(plan, self._public)
        if err:
            return self._log_result("plan", plan, False, err)
        alloc = {li: max(0.0, float(v)) for li, v in (plan.get("allocations") or {}).items()}
        total = sum(alloc.values())
        w = self.campaign.period_budget
        budgets = {li: w * alloc.get(li, 0.0) / total for li in LINE_ITEMS}
        err = check_spend(budgets, self.spent, self._public)
        if err:
            return self._log_result("plan", plan, False, err)
        self.budgets = budgets
        for li, share in (plan.get("holdouts") or {}).items():
            if share:
                self._start_holdout(li, float(share))
        if plan.get("creatives"):
            self._explicit_creatives = True
            for li, items in plan["creatives"].items():
                self._creatives[li] = [{"id": str(c["id"]), "status": "approved"} for c in items]
                if self._creatives[li]:
                    self._creatives[li][0]["status"] = "live"
        self._plan_applied = True
        return self._log_result("plan", plan, True, None)

    def observe(self) -> dict:
        creatives = [{"id": c["id"], "line_item": li, "status": c["status"],
                      "age_checkpoints": self.age[li] if c["status"] == "live" else 0}
                     for li, items in self._creatives.items() for c in items]
        return copy.deepcopy({
            "campaign": self._public,
            "checkpoint": self.checkpoint,
            "checkpoints_left": N_CHECKPOINTS - self.checkpoint,
            "status": self.status,
            "budgets": {li: round(v, 2) for li, v in self.budgets.items()},
            "holdouts": dict(self.holdouts),
            "creatives": creatives,
            "history": self.history,
        })

    def act(self, action: dict) -> dict:
        try:
            err = self._apply(action)
        except (KeyError, TypeError, ValueError) as e:
            err = f"malformed action: {e}"
        return self._log_result("action", action, err is None, err)

    def step(self) -> dict:
        """Run one checkpoint. Returns {"due_at": None}: simulated data is available immediately."""
        if self.done:
            raise RuntimeError("campaign finished")
        self.checkpoint += 1
        start = PERIOD_EPOCH + dt.timedelta(days=PERIOD_DAYS * (self.checkpoint - 1))
        end = start + dt.timedelta(days=PERIOD_DAYS - 1)
        entry = {
            "checkpoint": self.checkpoint, "read_no": 1,
            "period": {"start": start.isoformat(), "end": end.isoformat()},
            "status": "final", "attribution_window_closes_at": end.isoformat(), "delivery_ok": True,
            "line_items": {},
        }
        for li in LINE_ITEMS:
            t, s, h = self.campaign.truth[li], self.budgets[li], self.holdouts.get(li, 0.0)
            age = self.age[li]
            attr = _attributed(t, s, age)
            inc = t["r"] * attr
            self.achieved += (1 - h) * inc
            self.spent += s
            obs_attr = _nb(self.rng, (1 - h) * attr)
            delivery = _delivery(self.rng_delivery, t, s, age, obs_attr, self.campaign.avg_order_value)
            value, roas = delivery.pop("purchase_value"), delivery.pop("roas")
            entry["line_items"][li] = {
                "spend": round(s, 2),
                **delivery,
                "creative_age_checkpoints": age,
                "unavailable": list(UNAVAILABLE[SIM_CHANNELS[li]]),
                "reported": {
                    "attribution": ATTRIBUTION[li],
                    "attributed_conversions": obs_attr,
                    "cpa": round(s / obs_attr, 2) if obs_attr else None,
                    "purchase_value": value, "roas": roas,
                },
                "measured": {"holdout": self._measure_holdout(li, attr, inc, obs_attr) if h else None},
            }
            if s > 0:
                self.age[li] += 1
        self.history.append(entry)
        self.trace.append({"checkpoint": self.checkpoint, "kind": "step", "observed": entry})
        return {"due_at": None}

    # ---- hidden truth ----
    def debrief(self) -> dict:
        """Hidden truth for post-mortems. Use on TRAIN campaigns only."""
        best, best_spend = best_period_incremental(self.campaign)
        return {
            "campaign": self._public,
            "score": score(self),
            "achieved_incremental": round(self.achieved, 1),
            "best_possible_incremental": round(best * N_CHECKPOINTS, 1),
            "line_items": {li: {
                "true_incrementality": round(t["r"], 3),
                "attributed_cpa_at_half_saturation": round(t["attr_cpa"], 2),
                "true_incremental_cpa_at_half_saturation": round(t["attr_cpa"] / t["r"], 2) if t["r"] > 0 else None,
                "half_saturation_period_spend": round(t["K"], 0),
                "fatigue_decay_per_checkpoint": round(t["decay"], 3),
                "optimal_period_spend": round(best_spend[li], 0),
                "avg_period_spend": round(np.mean([e["line_items"][li]["spend"] for e in self.history]), 0) if self.history else 0,
            } for li, t in self.campaign.truth.items()},
        }

    # ---- internals ----
    def _apply(self, a: dict) -> Optional[str]:
        err = check_action(a, self._public)
        if err:
            return err
        if self.done:
            return "campaign finished"
        typ = a["type"]
        if typ == "shift_budget":
            src, dst, amt = a["from"], a["to"], float(a["amount"])
            if amt <= 0:
                return "amount must be positive"
            amt = min(amt, self.budgets[src])
            budgets = dict(self.budgets)
            budgets[src] -= amt
            budgets[dst] += amt
            return self._commit_budgets(budgets)
        li = a["line_item"]
        if typ == "pause":
            budgets = dict(self.budgets)
            freed, budgets[li] = budgets[li], 0.0
            active = [l for l in LINE_ITEMS if budgets[l] > 0]
            if not active:
                return "cannot pause the last active line item"
            tot = sum(budgets[l] for l in active)
            for l in active:
                budgets[l] += freed * budgets[l] / tot
            return self._commit_budgets(budgets)
        if typ == "rotate_creative":
            return self._rotate(li)
        share = float(a["share"])
        if share == 0:
            self.holdouts.pop(li, None)
            self._ho_acc.pop(li, None)
            return None
        return self._start_holdout(li, share)

    def _commit_budgets(self, budgets: Dict[str, float]) -> Optional[str]:
        err = check_spend(budgets, self.spent, self._public)
        if err:
            return err
        self.budgets = budgets
        return None

    def _rotate(self, li: str) -> Optional[str]:
        pool = self._creatives[li]
        nxt = next((c for c in pool if c["status"] == "approved"), None)
        if nxt is None and not self._explicit_creatives:  # implicit unlimited pool (simulator only)
            nxt = {"id": f"{li}-c{len(pool)}", "status": "approved"}
            pool.append(nxt)
        if nxt is None:
            return f"no approved creative available for {li}"
        for c in pool:
            if c["status"] == "live":
                c["status"] = "retired"
        nxt["status"] = "live"
        self.age[li] = 0
        return None

    def _start_holdout(self, li: str, share: float) -> Optional[str]:
        if not HOLDOUT_MIN <= share <= HOLDOUT_MAX:
            return f"holdout share must be 0 or in [{HOLDOUT_MIN}, {HOLDOUT_MAX}]"
        if self.holdouts.get(li) != share:
            self._ho_acc[li] = {"inc": 0.0, "var": 0.0, "attr": 0.0, "checkpoints": 0}
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
        acc["checkpoints"] += 1
        denom = max(acc["attr"], 1.0)
        est, half = acc["inc"] / denom, 1.96 * np.sqrt(acc["var"]) / denom
        return {
            "share": h, "checkpoints": acc["checkpoints"],
            "incremental_conversions_est": round(acc["inc"], 1),
            "incremental_share_est": round(est, 3),
            "ci95": [round(est - half, 3), round(est + half, 3)],
            "conclusive": bool(est - half > 0 or est + half < 0),
        }

    def _log_result(self, kind: str, payload: dict, ok: bool, err: Optional[str]) -> dict:
        self.trace.append({"checkpoint": self.checkpoint, "kind": kind, "payload": copy.deepcopy(payload),
                           "ok": ok, "error": err})
        return {"ok": ok, "error": err}


def score(sim: Simulator) -> float:
    """True incremental conversions achieved / best possible for this budget. Code only, hidden truth."""
    best, _ = best_period_incremental(sim.campaign)
    return float(sim.achieved / (best * N_CHECKPOINTS)) if best > 0 else 0.0
