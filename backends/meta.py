"""MetaBackend: the live CampaignBackend over the Meta Marketing API. Meta only (meta_prospecting, meta_retargeting).

Shape contract is sim/interface.py (schema v2); this file adds the live-only behavior:
  time        advance() only records `due_at` (period end + read_delay_days). status is "awaiting_data" until then, so a
              checkpoint run too early touches nothing. Each checkpoint can be a separate process: every bit of state
              lives in the run store (`backend_state`, `checkpoints`, `creatives`, `external_ids`).
  creatives   plan creatives are stored `pending`. Only `approved` ones ever reach Meta (apply_plan / sync_creatives /
              rotate_creative). Objects are created PAUSED; activate() is the only call that turns delivery on.
  data        an entry is `provisional` while its attribution window is open or an active ad set is below the
              delivery / conversion thresholds. No budget-moving action runs on a provisional latest reading. Once the
              window closes the period is re-read (read_no + 1); both readings are kept.
  spend caps  checked before every budget write against spend persisted per period, so restarts cannot reset them.
Unsupported operations (Google line items, holdouts) come back as {"ok": False, "error": ...}.
"""
from __future__ import annotations

import copy
import datetime as dt
import hashlib
from typing import Callable, Dict, List, Optional

from sim.interface import SCHEMA_VERSION, check_action, check_plan, check_spend
from store import Store

from .base import ActionResult, fail, ok
from .meta_api import MetaAPI, MetaError

LINE_ITEMS = ("meta_prospecting", "meta_retargeting")
ATTRIBUTION = "7d_click_1d_view"
UNAVAILABLE = ["search_impression_share", "search_budget_lost_impression_share", "search_rank_lost_impression_share",
               "measured.holdout"]
PURCHASE_ACTIONS = ("purchase", "omni_purchase", "offsite_conversion.fb_pixel_purchase")

DEFAULTS = {
    "product": "", "avg_order_value": 50.0, "total_budget": 400.0, "n_checkpoints": 4,
    "interval_days": 7,            # length of one checkpoint period
    "read_delay_days": 7,          # data is due this long after the period ends (>= window: first read is final)
    "attribution_window_days": 7,  # the platform's window; a period is provisional until it has closed
    "start_date": None,            # ISO date of period 1's first day (default: today)
    "max_total_spend": None, "max_period_spend": None,   # default: total_budget / period_budget
    "seconds_per_day": None,       # time-compressed demo mode: one schedule "day" lasts this many real seconds
    "min_impressions": 500, "min_conversions": 5,        # per active ad set, below which an entry is provisional
}


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


def _cents(daily_dollars: float) -> int:
    return int(round(daily_dollars * 100))


def synthetic_rows(daily_by_adset: Dict[str, float], days: int, since: str, aov: float) -> List[dict]:
    """Deterministic Graph-shaped Insights rows for dry runs (strings, like the real API)."""
    rows = []
    for adset_id, daily in daily_by_adset.items():
        if daily <= 0:
            continue  # Meta returns no row for an ad set that did not deliver
        h = int(hashlib.sha256(f"{adset_id}|{since}".encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
        spend = daily * days * (0.9 + 0.1 * h)
        cpm = 8 + 4 * h
        imps = int(spend / cpm * 1000)
        clicks = int(imps * 0.015)
        conv = int(spend / (20 + 10 * h))
        rows.append({"adset_id": adset_id, "spend": f"{spend:.2f}", "impressions": str(imps), "clicks": str(clicks),
                     "reach": str(int(imps / 1.5)), "frequency": "1.5", "ctr": "1.5", "cpm": f"{cpm:.2f}",
                     "cpc": f"{spend / clicks:.2f}" if clicks else "0",
                     "actions": [{"action_type": "purchase", "value": str(conv)}],
                     "action_values": [{"action_type": "purchase", "value": f"{conv * aov:.2f}"}]})
    return rows


def _pick(entries: Optional[list], key: str = "action_type") -> float:
    for e in entries or []:
        if e.get(key) in PURCHASE_ACTIONS:
            return float(e["value"])
    return 0.0


class MetaBackend:
    records_checkpoints = True  # observe() writes each reading (with the raw response) to the store itself

    def __init__(self, store: Store, run_id: str, api: MetaAPI, state: dict, clock: Optional[Callable[[], dt.datetime]] = None):
        self.store, self.run_id, self.api, self.s = store, run_id, api, state
        self.clock = clock or (self._scaled_clock if self.cfg.get("seconds_per_day") else _utcnow)

    # ---- construction ----
    @classmethod
    def create(cls, store: Store, api: MetaAPI, config: Optional[dict] = None, playbook_version: Optional[str] = None,
               run_id: Optional[str] = None, clock: Optional[Callable[[], dt.datetime]] = None) -> "MetaBackend":
        cfg = {**DEFAULTS, **(config or {})}
        now = (clock or _utcnow)()
        if cfg["n_checkpoints"] < 1 or cfg["interval_days"] < 1 or cfg["total_budget"] <= 0:
            raise ValueError("n_checkpoints and interval_days must be >= 1 and total_budget > 0")
        period = cfg["total_budget"] / cfg["n_checkpoints"]
        cfg["max_total_spend"] = cfg["total_budget"] if cfg["max_total_spend"] is None else cfg["max_total_spend"]
        cfg["max_period_spend"] = period if cfg["max_period_spend"] is None else cfg["max_period_spend"]
        cfg["start_date"] = cfg["start_date"] or now.date().isoformat()
        cfg["account_id"] = api.account_id
        if cfg["seconds_per_day"]:
            if cfg["seconds_per_day"] <= 0:
                raise ValueError("seconds_per_day must be > 0")
            cfg["start_date"], cfg["clock_anchor"] = _utcnow().date().isoformat(), _utcnow().isoformat()
        run_id = store.create_run("live", SCHEMA_VERSION, playbook_version, config={**cfg, "dry_run": api.dry_run},
                                  max_total_spend=cfg["max_total_spend"], campaign_ref=api.account_id, run_id=run_id)
        state = {"config": cfg, "checkpoint": 0, "plan_applied": False, "activated": False, "due_at": None,
                 "budgets": {li: period / len(LINE_ITEMS) for li in LINE_ITEMS}, "paused": {li: False for li in LINE_ITEMS},
                 "ids": {"campaign": None, "adsets": {}}, "ads": {}, "reads": {}, "spent_by_period": {}, "history": []}
        store.save_backend_state(run_id, state)
        return cls(store, run_id, api, state, clock)

    @classmethod
    def load(cls, store: Store, run_id: str, api: MetaAPI, clock: Optional[Callable[[], dt.datetime]] = None) -> "MetaBackend":
        state = store.load_backend_state(run_id)
        if state is None:
            raise KeyError(f"no backend state for run {run_id!r}")
        return cls(store, run_id, api, state, clock)

    def _save(self) -> None:
        self.store.save_backend_state(self.run_id, self.s)

    # ---- time ----
    @property
    def cfg(self) -> dict:
        return self.s["config"]

    def _scaled_clock(self) -> dt.datetime:
        """Compressed time: schedule time = midnight of start_date + real seconds since `clock_anchor` * (86400 / seconds_per_day)."""
        elapsed = (_utcnow() - dt.datetime.fromisoformat(self.cfg["clock_anchor"])).total_seconds()
        origin = dt.datetime.combine(dt.date.fromisoformat(self.cfg["start_date"]), dt.time())
        return origin + dt.timedelta(seconds=elapsed * 86400 / self.cfg["seconds_per_day"])

    def _period(self, k: int) -> tuple:
        start = dt.date.fromisoformat(self.cfg["start_date"]) + dt.timedelta(days=self.cfg["interval_days"] * (k - 1))
        return start, start + dt.timedelta(days=self.cfg["interval_days"] - 1)

    def _midnight_after(self, d: dt.date) -> dt.datetime:
        return dt.datetime.combine(d + dt.timedelta(days=1), dt.time())

    def _data_due(self, k: int) -> dt.datetime:
        return self._midnight_after(self._period(k)[1]) + dt.timedelta(days=self.cfg["read_delay_days"])

    def _closes_at(self, k: int) -> dt.datetime:
        return self._midnight_after(self._period(k)[1]) + dt.timedelta(days=self.cfg["attribution_window_days"])

    @property
    def done(self) -> bool:
        n = self.cfg["n_checkpoints"]
        r = self.s["reads"].get(str(n))
        return self.s["checkpoint"] == n and r is not None and r["window_closed"]

    @property
    def status(self) -> str:
        if self.done:
            return "done"
        k = self.s["checkpoint"]
        if k == 0:
            return "ready"
        r = self.s["reads"].get(str(k))
        wait_until = self._data_due(k) if r is None else (self._closes_at(k) if not r["window_closed"] and k == self.cfg["n_checkpoints"]
                                                          else None)
        return "awaiting_data" if wait_until and self.clock() < wait_until else "ready"

    @property
    def due_at(self) -> Optional[str]:
        return self.s["due_at"]

    # ---- campaign / observation ----
    def _campaign(self) -> dict:
        c, n = self.cfg, self.cfg["n_checkpoints"]
        return {"schema_version": SCHEMA_VERSION, "id": self.run_id, "product": c["product"],
                "avg_order_value": c["avg_order_value"], "total_budget": c["total_budget"],
                "period_budget": round(c["total_budget"] / n, 2), "n_checkpoints": n,
                "max_total_spend": c["max_total_spend"], "max_period_spend": c["max_period_spend"],
                "line_items": list(LINE_ITEMS),
                "capabilities": {"line_items": {li: {"channel": "meta"} for li in LINE_ITEMS}, "holdouts": False,
                                 "attribution": {li: ATTRIBUTION for li in LINE_ITEMS}}}

    def _age(self, li: str, at: int) -> int:
        live = [a["live_since"] for a in self.s["ads"].values() if a["line_item"] == li and a["live_since"] <= at]
        return at - max(live) if live else 0

    def _registry(self) -> List[dict]:
        k = self.s["checkpoint"]
        out = []
        for c in self.store.list_creatives(self.run_id):
            ad = self.s["ads"].get(c["creative_id"])
            out.append({"id": c["creative_id"], "line_item": c["line_item"], "status": c["status"],
                        "age_checkpoints": max(0, k - ad["live_since"]) if ad and c["status"] == "live" else 0})
        return out

    def observe(self) -> dict:
        self._refresh()
        k, n = self.s["checkpoint"], self.cfg["n_checkpoints"]
        return {"campaign": self._campaign(), "checkpoint": k, "checkpoints_left": n - k, "status": self.status,
                "budgets": {li: round(b, 2) for li, b in self.s["budgets"].items()}, "holdouts": {},
                "creatives": self._registry(), "history": copy.deepcopy(self.s["history"])}

    def _refresh(self) -> None:
        """Read what is due: the current period's first reading, and re-reads of periods whose window has closed."""
        now = self.clock()
        for j in range(1, self.s["checkpoint"] + 1):
            r = self.s["reads"].get(str(j))
            if r is None and j == self.s["checkpoint"] and now >= self._data_due(j):
                self._read(j)
            elif r is not None and not r["window_closed"] and now >= self._closes_at(j):
                self._read(j)

    def _read(self, j: int) -> None:
        start, end = self._period(j)
        s = self.s
        daily = {aid: s["budgets"][li] / self.cfg["interval_days"] for li, aid in s["ids"]["adsets"].items()}
        rows = self.api.insights(s["ids"]["campaign"], start.isoformat(), end.isoformat(),
                                 synth=lambda: synthetic_rows(daily, self.cfg["interval_days"], start.isoformat(),
                                                              self.cfg["avg_order_value"]))
        prev = s["reads"].get(str(j))
        read_no = 1 if prev is None else prev["read_no"] + 1
        window_closed = self.clock() >= self._closes_at(j)
        entry = self._normalize(rows, j, read_no, window_closed)
        self.store.record_checkpoint(self.run_id, j, entry, raw={"rows": rows}, read_no=read_no, period=entry["period"],
                                     provisional=entry["status"] == "provisional")
        s["reads"][str(j)] = {"read_no": read_no, "window_closed": window_closed, "status": entry["status"]}
        s["spent_by_period"][str(j)] = round(sum(m["spend"] for m in entry["line_items"].values()), 2)
        s["history"].append(entry)
        self._save()

    def _normalize(self, rows: List[dict], j: int, read_no: int, window_closed: bool) -> dict:
        s, cfg = self.s, self.cfg
        by_li = {li: next((r for r in rows if r.get("adset_id") == aid), {}) for li, aid in s["ids"]["adsets"].items()}
        start, end = self._period(j)
        items, delivery_ok, thin = {}, True, False
        for li in LINE_ITEMS:
            r = by_li.get(li, {})
            f = lambda k: float(r[k]) if r.get(k) not in (None, "") else None  # noqa: E731
            spend, imps = f("spend") or 0.0, int(f("impressions") or 0)
            conv, value = int(_pick(r.get("actions"))), _pick(r.get("action_values"))
            if s["budgets"][li] > 0:
                delivery_ok &= imps >= cfg["min_impressions"]
                thin |= imps < cfg["min_impressions"] or conv < cfg["min_conversions"]
            items[li] = {
                "spend": round(spend, 2), "impressions": imps, "clicks": int(f("clicks") or 0),
                "reach": None if f("reach") is None else int(f("reach")), "frequency": f("frequency"),
                "ctr": f("ctr"), "cpm": f("cpm"), "cpc": f("cpc"),
                "search_impression_share": None, "search_budget_lost_impression_share": None,
                "search_rank_lost_impression_share": None,
                "creative_age_checkpoints": self._age(li, j - 1), "unavailable": list(UNAVAILABLE),
                "reported": {"attribution": ATTRIBUTION, "attributed_conversions": conv,
                             "cpa": round(spend / conv, 2) if conv else None, "purchase_value": round(value, 2),
                             "roas": round(value / spend, 2) if spend else None},
                "measured": {"holdout": None}}
        return {"checkpoint": j, "read_no": read_no, "period": {"start": start.isoformat(), "end": end.isoformat()},
                "status": "final" if window_closed and not thin else "provisional",
                "attribution_window_closes_at": self._closes_at(j).date().isoformat(),
                "delivery_ok": bool(delivery_ok), "line_items": items}

    # ---- plan ----
    def apply_plan(self, plan: dict) -> ActionResult:
        s = self.s
        if s["plan_applied"]:
            return fail("plan already applied")
        camp = self._campaign()
        err = check_plan(plan, camp)
        if err:
            return fail(err)
        alloc = {li: max(0.0, float(v)) for li, v in (plan.get("allocations") or {}).items()}
        total = sum(alloc.values())
        budgets = {li: camp["period_budget"] * alloc.get(li, 0.0) / total for li in LINE_ITEMS}
        err = check_spend(budgets, 0.0, camp)
        if err:
            return fail(err)
        have = {(c["line_item"], c["creative_id"]) for c in self.store.list_creatives(self.run_id)}
        for li, items in (plan.get("creatives") or {}).items():
            for c in items:
                if (li, c["id"]) not in have:  # never downgrade a creative a human already approved
                    self.store.save_creative(self.run_id, li, c["id"], c, "pending")
        try:  # progress is saved after each object, so a failed apply_plan can be retried without duplicates
            if s["ids"]["campaign"] is None:
                s["ids"]["campaign"] = self.api.create_campaign(f"Swarm {self.run_id}")
                self.store.record_external_id(self.run_id, "campaign", s["ids"]["campaign"])
                self._save()
            for li in LINE_ITEMS:
                if li not in s["ids"]["adsets"]:
                    s["ids"]["adsets"][li] = self.api.create_adset(
                        s["ids"]["campaign"], f"{li} {self.run_id}", _cents(budgets[li] / self.cfg["interval_days"]))
                    self.store.record_external_id(self.run_id, "adset", s["ids"]["adsets"][li], li)
                    self._save()
        except MetaError as e:
            return fail(f"Meta error while creating the campaign: {e}")
        s["budgets"], s["paused"] = budgets, {li: budgets[li] <= 0 for li in LINE_ITEMS}
        s["plan_applied"] = True
        self._save()
        return self.sync_creatives()

    # ---- creatives ----
    def _attach(self, li: str, c: dict) -> None:
        cid, content = c["creative_id"], c["content"]
        status = "ACTIVE" if self.s["activated"] else "PAUSED"
        mc = self.api.create_creative(f"{cid} {self.run_id}", content.get("headline", ""), content.get("body", ""),
                                      content.get("cta", "LEARN_MORE"))
        ad = self.api.create_ad(f"{cid} {self.run_id}", self.s["ids"]["adsets"][li], mc, status)
        self.store.record_external_id(self.run_id, "ad", ad, li)
        self.store.set_creative_status(self.run_id, li, cid, "live", meta_id=ad)
        self.s["ads"][cid] = {"line_item": li, "ad_id": ad, "live_since": self.s["checkpoint"], "retired": False}
        self._save()

    def sync_creatives(self) -> ActionResult:
        """Create (paused) ads for approved creatives that are not on Meta yet. Pending creatives are never sent."""
        if not self.s["plan_applied"]:
            return fail("no plan applied")
        try:
            for c in self.store.list_creatives(self.run_id, status="approved"):
                if c["creative_id"] not in self.s["ads"]:
                    self._attach(c["line_item"], c)
        except MetaError as e:
            return fail(f"Meta error while creating ads: {e}")
        return ok()

    # ---- actions ----
    def _provisional(self) -> bool:
        h = self.s["history"]
        return bool(h) and h[-1]["status"] == "provisional"

    def _spent(self) -> float:
        return sum(self.s["spent_by_period"].values())

    def _write_budgets(self, new: Dict[str, float], pause: Optional[str] = None) -> Optional[str]:
        """Push changed budgets to Meta; on a failure put back what was already written. Returns an error or None."""
        old, written = self.s["budgets"], []
        try:
            for li in LINE_ITEMS:
                if abs(new[li] - old[li]) > 1e-9:
                    self.api.set_adset_budget(self.s["ids"]["adsets"][li], _cents(new[li] / self.cfg["interval_days"]))
                    written.append(li)
            if pause:
                self.api.set_status(self.s["ids"]["adsets"][pause], "PAUSED")
        except MetaError as e:
            for li in written:
                try:
                    self.api.set_adset_budget(self.s["ids"]["adsets"][li], _cents(old[li] / self.cfg["interval_days"]))
                except MetaError:
                    pass
            return f"Meta error while updating budgets: {e}"
        return None

    def act(self, action: dict) -> ActionResult:
        s = self.s
        try:
            err = check_action(action, self._campaign())
        except (KeyError, TypeError, ValueError) as e:
            err = f"malformed action: {e}"
        if err:
            return fail(err)
        if not s["plan_applied"]:
            return fail("no plan applied")
        typ = action["type"]
        if typ == "rotate_creative":
            return self._rotate(action["line_item"])
        if self._provisional():
            return fail("latest period data is provisional; no budget-moving actions until it is final")
        new = dict(s["budgets"])
        pause = None
        if typ == "shift_budget":
            src, dst = action["from"], action["to"]
            amount = min(float(action["amount"]), new[src])
            if amount <= 0:
                return fail(f"{src} has no budget to move")
            new[src] -= amount
            new[dst] += amount
        else:  # pause
            li = action["line_item"]
            others = [x for x in LINE_ITEMS if x != li and new[x] > 0]
            if new[li] <= 0:
                return fail(f"{li} is already paused")
            if not others:
                return fail("cannot pause the last active line item")
            freed, base = new[li], sum(new[x] for x in others)
            for x in others:
                new[x] += freed * new[x] / base
            new[li], pause = 0.0, li
        err = check_spend(new, self._spent(), self._campaign())
        if err:
            return fail(err)
        err = self._write_budgets(new, pause)
        if err:
            return fail(err)
        s["budgets"] = new
        if pause:
            s["paused"][pause] = True
        self._save()
        return ok()

    def _rotate(self, li: str) -> ActionResult:
        pool = [c for c in self.store.list_creatives(self.run_id, status="approved") if c["line_item"] == li]
        if not pool:
            return fail(f"no approved creative available for {li}")
        try:
            for cid, ad in self.s["ads"].items():
                if ad["line_item"] == li and not ad["retired"]:
                    self.api.set_status(ad["ad_id"], "PAUSED")
                    ad["retired"] = True
                    self.store.set_creative_status(self.run_id, li, cid, "retired")
            self._attach(li, pool[0])
        except MetaError as e:
            self._save()
            return fail(f"Meta error while rotating creative: {e}")
        return ok()

    # ---- time / activation ----
    def advance(self) -> dict:
        """Start the next period and record when its data is due. Never blocks."""
        s, k = self.s, self.s["checkpoint"]
        if self.done:
            raise RuntimeError("campaign finished")
        if k == 0 and not s["plan_applied"]:
            raise RuntimeError("no plan applied")
        if k > 0 and str(k) not in s["reads"]:
            raise RuntimeError(f"period {k} has not been observed yet")
        if k == self.cfg["n_checkpoints"]:
            raise RuntimeError("the final period is still maturing")
        s["checkpoint"] = k + 1
        s["due_at"] = self._data_due(k + 1).isoformat()
        self._save()
        return {"due_at": s["due_at"]}

    def activate(self) -> ActionResult:
        """The only call that turns delivery on. Needs an applied plan, a live approved ad and explicit spend caps."""
        s = self.s
        if s["activated"]:
            return fail("already activated")
        if not s["plan_applied"] or s["checkpoint"] != 1 or s["reads"]:
            return fail("activate only after create, before the first period is read")
        if not any(not a["retired"] for a in s["ads"].values()):
            return fail("no approved creative is attached; approve one and sync first")
        if not (self.cfg["max_total_spend"] and self.cfg["max_period_spend"]):
            return fail("spend caps are not configured")
        try:
            self.api.set_status(s["ids"]["campaign"], "ACTIVE")
            for li in LINE_ITEMS:
                if not s["paused"][li]:
                    self.api.set_status(s["ids"]["adsets"][li], "ACTIVE")
            for ad in s["ads"].values():
                if not ad["retired"]:
                    self.api.set_status(ad["ad_id"], "ACTIVE")
        except MetaError as e:
            return fail(f"Meta error while activating: {e}")
        if self.cfg.get("seconds_per_day"):  # compressed time restarts at day 0 of the schedule
            self.cfg["start_date"], self.cfg["clock_anchor"] = _utcnow().date().isoformat(), _utcnow().isoformat()
        else:
            self.cfg["start_date"] = self.clock().date().isoformat()  # the schedule starts when delivery does
        s["activated"] = True
        s["due_at"] = self._data_due(1).isoformat()
        self._save()
        self.store.set_run_status(self.run_id, "active", s["due_at"])
        return ok()
