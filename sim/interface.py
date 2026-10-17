"""Simulator <-> agent contract. Person B codes against this; Person A owns the implementation.

Usage:
    from sim import generate_campaigns, Simulator, score

    for campaign in generate_campaigns(6, "train", seed=0):
        sim = Simulator(campaign, seed=1)
        sim.apply_plan(plan)                # once, before week 1 (planner output, see PLAN below)
        while not sim.done:
            obs = sim.observe()             # visible metrics only (see OBSERVATION below)
            for action in actions:          # channel agent tool calls (see ACTIONS below)
                result = sim.act(action)    # {"ok": bool, "error": str | None}
            sim.step()                      # run one weekly checkpoint
        s = score(sim)                      # 0..1, from hidden truth. Never shown to agents mid-run.
        debrief = sim.debrief()             # hidden truth + score. TRAIN SPLIT ONLY (improver post-mortems).

Rules:
- 4 checkpoints (weeks). Weekly budget is fixed = total_budget / 4. Agents only split it.
- Same (campaign, seed) + same actions -> identical observations. Score uses expected
  (noise-free) true incremental conversions, so it depends only on the actions taken.
- Splits: "train" (improver studies), "validation" (gate decides), "test" (final eval, once).
  Test campaigns contain trap variations not seen in train/validation.
"""

LINE_ITEMS = ["meta_prospecting", "meta_retargeting", "google_nonbrand", "google_brand"]
CHANNEL_OF = {
    "meta_prospecting": "meta",
    "meta_retargeting": "meta",
    "google_nonbrand": "google",
    "google_brand": "google",
}
N_CHECKPOINTS = 4
SPLITS = ("train", "validation", "test")

HOLDOUT_MIN, HOLDOUT_MAX = 0.05, 0.5

# PLAN (planner output, passed to sim.apply_plan):
# {
#   "allocations": {"meta_prospecting": 0.4, "meta_retargeting": 0.2, ...},  # shares of weekly budget, normalized to sum 1
#   "holdouts":    {"meta_retargeting": 0.2},                               # optional, share in [HOLDOUT_MIN, HOLDOUT_MAX]
#   "hypotheses":  {"meta_retargeting": "cheap CPA may be over-credited"},   # optional, stored in trace only
# }
# Missing line items get 0. A holdout withholds ads from that share of the audience: it costs that
# share of the line item's true incremental conversions, and in return the observation reports a
# measured incremental estimate with a 95% CI.

# ACTIONS (sim.act), applied to the NEXT week:
# {"type": "shift_budget", "from": li, "to": li, "amount": dollars}   # amount capped at from's weekly budget
# {"type": "pause", "line_item": li}            # budget -> 0, freed money spread over active items pro rata
# {"type": "rotate_creative", "line_item": li}  # resets creative fatigue clock to 0
# {"type": "set_holdout", "line_item": li, "share": 0.2}   # share 0 ends the holdout
ACTION_TYPES = ("shift_budget", "pause", "rotate_creative", "set_holdout")

# OBSERVATION (sim.observe()):
# {
#   "campaign": {"id", "product", "total_budget", "weekly_budget", "n_weeks", "line_items"},
#   "week": int,                 # weeks completed so far (0 before the first step)
#   "weeks_left": int,
#   "budgets": {li: dollars},    # weekly budget for the next week
#   "holdouts": {li: share},     # active holdouts
#   "history": [                 # one entry per completed week
#     {"week": 1, "line_items": {li: {
#         "spend": float,
#         "attributed_conversions": int,       # last-click, noisy
#         "cpa": float | None,                 # spend / attributed_conversions
#         "creative_age_weeks": int,           # weeks since last rotation, at time of serving
#         "holdout": None | {                  # cumulative since this holdout began
#             "share": float, "weeks": int,
#             "incremental_conversions_est": float,
#             "incremental_share_est": float,  # est. incremental / attributed (1.0 = last-click is right)
#             "ci95": [lo, hi],                # on incremental_share_est
#             "conclusive": bool,              # CI excludes 0
#         },
#     }}},
#   ],
# }
