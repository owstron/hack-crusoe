# Action Plan: NVIDIA Swarm, Simulator, and Meta Integration

## Outcome

Deliver a versioned, NVIDIA-model ad-agent swarm that first improves itself safely in the existing simulator, then can create and manage paused Meta campaigns through the same plan/action interface.

The simulator remains the source of truth for playbook acceptance. Live Meta results are operational evidence, not inputs to the simulator gate.

## Current baseline

### Already available

| Capability | Existing implementation | Use in this plan |
|---|---|---|
| Simulator and hidden-truth score | `sim/simulator.py` | Keep hidden-truth mechanics and scoring. The interface schema changes once, in Phase 0.5 (`schema_version` 2). |
| Shared plan/action/observation contract | `sim/interface.py` | Canonical interface; migrated to schema v2 in Phase 0.5, then frozen. |
| Validation gate | `sim/gate.py` | Use for every candidate playbook decision. |
| Train failure summaries and improvement driver | `improve/loop.py` | Plug in the real runner and improver. |
| Test-set evaluation | `sim/final_eval.py` | Run once after the improvement loop. |
| Cached You.com research | `improve/research.py` | Give the Improver one cached research call per round. |
| Static results page | `report/build_page.py` | Render every completed simulator run. |
| Meta Graph API foundation | `ad_swarm_full.py` | Refactor only after simulator agents work. |

### Missing

- NVIDIA model client with role-specific configuration and structured-output parsing.
- Versioned playbook source (`playbooks/v1.md`).
- Planner, channel-agent, verifier, runner, and improver modules.
- A simulator adapter that exposes the common backend protocol.
- A Meta backend that normalizes platform data into the simulator’s observation shape.
- A unified CLI for selecting simulation or live mode.

## Decisions locked before implementation

### Model routing

These choices supersede the earlier model names in `planning/brainstorm.md`:

| Role | Model | Temperature during evaluated simulation |
|---|---|---|
| Planner | `nvidia/NVIDIA-Nemotron-3-Super-120B-A12B` | `0` |
| Channel agent | `nvidia/Nemotron-3.5-Lightning-30B-A3B` | `0` |
| Verifier | `nvidia/Nemotron-3-Nano-Omni-Reasoning-30B-A3B` | `0` |
| Trace/research summarizer | `nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B` | `0` |
| Improver | `nvidia/NVIDIA-Nemotron-3-Super-120B-A12B` | `0` |
| Visual creative reviewer | `nvidia/NVIDIA-Nemotron-3.5-Super-VL` | n/a until visual assets exist |

The evaluation model configuration must be recorded in the run store (`model_calls`) and remain fixed across a gate comparison. The code, not a model, validates JSON plans and actions.

Before implementation, confirm the exact model IDs exposed by Crusoe for these choices. Treat temperature as an explicit evaluation setting: keep it fixed within a gate comparison, record it in `model_calls`, and compare the configured value on a small representative workload before locking it in.

### Backend scope

- **Phase 1:** the agent runner targets `sim.Simulator` only.
- **Phase 2:** Meta supports `meta_prospecting` and `meta_retargeting` only.
- **Phase 2:** Meta rejects Google line-item actions and `set_holdout` with a normalized error.
- **Phase 3:** add Meta lift experiments and visual review when real experiment and asset inputs are available.

### Simulator vs live differences

The interfaces share data shapes, not timing. In the simulator `advance()` is instant; live, it only records `due_at`, and each checkpoint is a separate process invocation. Live observations can be partial or delayed (attribution windows, no holdouts, no Google fields), so agents and playbooks must tolerate `None` and `provisional` data. Simulator scores never predict live results.

## Work sequence

### Phase 0 — Prepare a stable implementation base

**Tasks**

1. Reconcile dependency metadata: add the legacy Meta requirements (`requests`, `youdotcom`) to `requirements.txt`, or make `requirements.txt` derive from the project dependency list.
2. Add `.env.example` documenting `CRUSOE_API_KEY`, `YDC_API_KEY`, and the optional Meta variables without including real credentials.
3. Create `playbooks/v1.md` with a deliberately naive, numbered policy. It must be readable without any model call.
4. Add a compact README section with the simulation command, expected environment variables, and the distinction between simulator score and live metrics.
5. Create the `agents/` package skeleton (`agents/__init__.py`) so imports work before its implementation phases.

**Done when**

- A clean environment can import `sim`, `improve`, `report`, and the `agents` package skeleton.
- The starting playbook is a tracked file and can be copied by `improve/loop.py`.

### Phase 0.5 — Interface schema v2

The v1 contract is simulator-shaped (fixed weeks, no creatives, no data-quality signal). Change it once, before any agent code depends on it, so a live backend does not have to pretend to be a simulator. Hidden-truth mechanics, the split policy, units (dollars, CTR in percent), and the action vocabulary stay as they are, except where noted.

**Files**

```text
sim/interface.py        (schema v2)
sim/simulator.py        (emit v2 fields)
sim/gate.py, sim/final_eval.py, improve/loop.py, report/build_page.py, smoke_test.py  (rename fields)
```

**Tasks**

1. **Checkpoints replace weeks.** `n_weeks`→`n_checkpoints`, `week`→`checkpoint`, `weeks_left`→`checkpoints_left`, `weekly_budget`→`period_budget`. Each history entry gets `period: {"start", "end"}` and `read_no` (for re-reads of the same period). In the simulator a period is 7 synthetic days.
2. **Data-quality fields** on each history entry: `status` (`"final"` | `"provisional"`), `attribution_window_closes_at`, and `delivery_ok`. The simulator may under-report recent conversions as provisional so agents learn to wait; keep this off in the first iteration if it changes baseline scores, and record that choice in `schema_version` notes.
3. **Explicit time and state.** `advance()` returns `{"due_at": str | None}`; the backend exposes `status` (`awaiting_data` | `ready` | `done`). The simulator is always `ready` until `done`.
4. **Capabilities.** The campaign object gets `capabilities: {"line_items", "holdouts": bool, "attribution": {...}}` and each line item declares its `channel`, replacing the fixed `CHANNEL_OF` table. Plan and action validators read this, so a Meta-only live run rejects Google items and `set_holdout` through the schema.
5. **Explicit unavailability.** Each line-item entry carries `unavailable: [field, ...]` so "not reported" is distinct from zero (replaces implicit `None` conventions).
6. **Reported vs measured.** Group platform-attributed numbers under `reported` and the holdout/lift estimate under `measured`, so live lift studies (Phase 8) have a place to land without changing the entry shape.
7. **Creatives.** Plan gains optional `creatives: {line_item: [{"id", "headline", "body", "cta", "image_ref"?}]}`. The observation gets a creative registry (`id`, `age_checkpoints`, `status`). `rotate_creative` selects an already approved creative for that line item from the registry; if none is available, it returns a normalized error and makes no change. It does not generate or submit new creative content. The `set_creative` action and per-creative fatigue modeling are deferred until the rest of the loop works (listed in Phase 8).
8. **Safety limits.** Campaign object carries `max_total_spend` for lifetime spend and `max_period_spend` for spend within the configured checkpoint period. `act()` and `apply_plan()` enforce both in every backend. Persist period boundaries and spend to date so restarts and rereads cannot reset the cap. A calendar-day cap, if needed, is a separate setting.
9. **Versioning.** Add `schema_version: 2` to the campaign object. Scores from v1 runs are not comparable with v2; keep them only as archived history.

**Done when**

- `smoke_test.py` passes against v2 and a baseline policy scores the same as before the migration (or the difference is explained by any provisional-data change).
- No active schema or runtime code uses the old contract fields `weeks_left`, `n_weeks`, `weekly_budget`, or `CHANNEL_OF`; historical documentation and explicitly retained compatibility code are listed as exceptions.
- The contract docstring in `sim/interface.py` documents every v2 field.

### Phase 0.6 — Unified run store

One SQLite database stores every run, simulated or live, so resumption, reporting, and cross-mode comparison use the same code. This replaces the earlier file layout (`runs/<name>/` for simulation, `live_runs/<run_id>.json` for live).

**Files**

```text
store/__init__.py
store/schema.sql
store/db.py
```

**Schema**

```text
runs          run_id, mode (sim|live), schema_version, playbook_version, config_json, status,
              created_at, due_at, max_total_spend, campaign_ref
checkpoints   run_id, checkpoint_idx, read_no, period_start, period_end,
              observation_json, raw_json, provisional, ts      PK(run_id, checkpoint_idx, read_no)
actions       event_id, run_id, checkpoint_idx, read_no, action_json, result_ok, error, ts
model_calls   event_id, run_id, checkpoint_idx, read_no, role, model, temperature, prompt, raw_response,
              parsed_json, error, fallback_used
creatives     run_id, line_item, creative_id, content_json, status (pending|approved|live|retired), meta_id
external_ids  run_id, kind (campaign|adset|ad), line_item, meta_id
scores        run_id, split, score, debrief_json              (simulator only)
playbooks     version, parent, text, diff, gate_result_json, research_urls
improvement   round, base_version, candidate_version, stats_json, accepted
```

`event_id` is unique and monotonically ordered within a run, providing stable trace ordering. Actions and model calls link to the checkpoint read that caused them; planner calls before the first observation may use `checkpoint_idx = 0`, `read_no = 0`. Persist the runner state needed to resume, including whether the plan has been applied and the next due checkpoint. Re-reading a period increments `read_no` without overwriting earlier observations or their events.

**Tasks**

1. Create a new database file for the unified store (default `store/adswarm.db`). Do not reuse the legacy `ad_swarm.db`, whose tables belong to `ad_swarm_full.py`.
2. Use WAL mode and short transactions so parallel gate campaigns can write safely; one connection per worker.
3. Expose functions, not raw SQL, to the rest of the code: `create_run`, `record_checkpoint`, `record_action`, `record_model_call`, `save_creative`, `set_creative_status`, `load_run_state`, and so on.
4. **Hidden-truth access:** `scores.debrief_json` is readable only through gate, final-eval, and Improver (train split) functions. Agents never receive a database handle, and no function returns test-split debriefs outside `final_eval`.
5. Replace `sim.trace` as the persisted trace: the in-memory trace may remain for the simulator, but every entry is also written through the store.
6. Adapt `improve/loop.py`, `sim/gate.py`, `sim/final_eval.py`, and `report/build_page.py` to read and write through the store. `build_page.py` selects by `mode`, so simulator scores and live outcomes render in separate sections.
7. Keep generated artifacts (HTML report) in `runs/<name>/index.html`; the database is the source of record.
8. The database may be discarded and recreated when starting fresh. When resetting it, close all connections and remove the database together with its `-wal` and `-shm` files; never delete only the main database file while connections are open.

**Done when**

- A simulated run and a mocked live run both create rows, and a run can be resumed from `load_run_state` in a fresh process.
- The report builds from the database alone.
- A test confirms agent code paths cannot read `scores.debrief_json`.

### Phase 1 — Build the shared backend boundary

**Files**

```text
backends/__init__.py
backends/base.py
backends/simulator.py
```

**Tasks**

1. Define a `CampaignBackend` protocol with `apply_plan`, `observe`, `act`, `advance`, and `done`.
2. Define a normalized action result: `{"ok": bool, "error": str | None}`.
3. Implement the schema v2 `advance()` result (`{"due_at": str | None}`) and `status` in the protocol. The simulator returns `None` (the next checkpoint is available immediately). A live backend returns the ISO timestamp of the next checkpoint and does not block.
4. Creative text is generated by the Planner/Channel agents and carried in the plan's `creatives` field (schema v2). The simulator records it in the trace. A live backend creates ads only from approved creatives; `rotate_creative` selects an approved creative already in the registry and never generates or submits new content. If none is available, it returns an error without changing delivery. New creative approval is a separate workflow.
5. Implement `SimulatorBackend` as a thin wrapper over `sim.Simulator`; it delegates to the simulator and maps `advance()` to `step()`.
6. Use the schema v2 dictionaries as-is; do not introduce a second observation schema.
7. Document which observation fields a live backend may mark `unavailable` or `provisional` (`holdout`, Google fields, attributed conversions inside the attribution window). Agents and the playbook must treat these as optional.

**Done when**

- A short local harness can run a baseline policy through `SimulatorBackend` to completion and obtain the same score as direct `sim.Simulator` use.
- `SimulatorBackend.advance()` returns `{"due_at": None}` and a plan containing `creatives` runs unchanged.

### Phase 2 — Add NVIDIA model infrastructure

**Files**

```text
agents/__init__.py
agents/models.py
agents/schemas.py
```

**Tasks**

1. Build a Crusoe OpenAI-compatible client using `CRUSOE_API_KEY` and the existing Crusoe base URL.
2. Centralize the role-to-model mapping listed above.
3. Implement one typed model-call helper that records role, model ID, temperature, prompts, raw response, parsed result, and errors.
4. Implement robust extraction of a JSON object or array from model output.
5. Implement deterministic code validation for planner plans and channel actions using the exact line items and action types in `sim/interface.py`.
6. Define a retry/fallback policy per role and record fallback use in the trace. Do not change models silently inside a gate comparison.

**Done when**

- A mocked model response and a real model response both produce either a validated payload or a structured failure.
- Invalid action JSON never reaches a backend.

### Phase 3 — Implement the simulator agent team

**Files**

```text
agents/planner.py
agents/channel.py
agents/verifier.py
agents/runner.py
```

**Tasks**

1. **Planner:** read the playbook and the public campaign object; return the contract’s `allocations`, optional `holdouts`, and per-line-item hypotheses.
2. **Channel agents:** at each checkpoint, receive the normalized observation and return only permitted actions. Start with one agent per channel, filtering each agent to its own line items.
3. **Verifier:** write a definition of done before the first checkpoint, then interpret the observed holdout evidence after the final checkpoint. The verifier must not see hidden truth.
4. **Runner:** split the work into a resumable unit and a loop:
   - `run_checkpoint(version, backend, state)` handles exactly one checkpoint: observe, call Channel agents and the Verifier as needed, apply actions, call `advance()`, and return updated state (including `due_at`). On the first call (checkpoint 0) it also runs the Planner and `apply_plan`.
   - `run(version, sim)` is the plug-in signature already consumed by `sim/gate.py` and `improve/loop.py`. It wraps the simulator in `SimulatorBackend` and calls `run_checkpoint` until `done`.
   - Live mode (Phase 6/7) calls `run_checkpoint` once per scheduled date. Agent logic lives only in `run_checkpoint`, so simulator and live behavior cannot diverge.
5. Append plan, prompts, model metadata, outputs, actions, action results, and verification records through the run store.
6. If a model call fails, record a trace error in the store and safely continue the campaign using its current allocation. Let the existing gate count agent crashes.

**Done when**

- `run_improvement.py --runner agents.runner:run` runs one complete simulated campaign.
- `run_checkpoint` can be called from a fresh process with only saved state and a backend, and produces the same result as the in-process loop.
- The runner exposes no hidden truth to Planner, Channel, or Verifier.
- `improve/loop.py` can include the resulting trace in its train-failure summaries.

### Phase 4 — Implement the NVIDIA Improver

**Files**

```text
agents/improver.py
```

**Tasks**

1. Implement `propose_edit(playbook, failures, history)` using the existing plug-in contract.
2. Optionally use the Nano summarizer to reduce the failure and research context.
3. Make at most one call to `improve.research.research()` or `search()` per round. Pass its sources through unchanged.
4. Have the Super Improver return exactly one full revised playbook plus summary, rationale, and sources.
5. Reject malformed, empty, or identical edits before the validation gate; `improve/loop.py` already records these outcomes.

**Done when**

- The real improver can replace `improve.stubs:propose_edit`.
- Each change-log entry contains the actual playbook diff, validation result, and research URLs when research was used.

### Phase 5 — Prove the simulated N+1 loop

**Tasks**

1. Run a short smoke improvement cycle with one or two rounds.
2. Inspect trace quality, JSON failures, gate errors, and generated candidate playbooks.
3. Run the configured number of rounds on train and validation.
4. Run `sim.final_eval.final_eval()` once against the first and final accepted playbooks.
5. Render `runs/<name>/index.html` from the run store through `report/build_page.py`.

**Done when**

- The output contains at least Version 1, an auditable change log, and a final test evaluation.
- Every accepted edit passed the paired validation gate.
- The test split was not used to choose a playbook.

### Phase 6 — Refactor the existing Meta implementation

**Files**

```text
backends/meta.py
```

**Tasks**

1. Move Meta HTTP helpers, account discovery, creative creation, Insights polling, and paused campaign creation out of `ad_swarm_full.py`.
2. Replace the single broad ad set with two named ad sets: `meta_prospecting` and `meta_retargeting`.
3. Persist campaign/ad-set/ad IDs, mapping to shared line items, budgets, checkpoint dates, actions, and observations in the run store (`runs`, `external_ids`, `checkpoints`, `actions`).
4. Implement the shared backend methods:
   - `apply_plan`: create a paused campaign, the supported ad sets, and the ads from the plan's `creatives`.
   - `observe`: retrieve Insights for the elapsed window and produce a normalized history entry.
   - `act`: update budgets, pause an ad set, or attach an already approved creative selected by `rotate_creative`. If no approved replacement exists, return an error and leave delivery unchanged.
   - `advance`: compute and persist `due_at` (the next checkpoint date, from a predefined interval in the run config); never block a process for days.
5. Reject unsupported Google and holdout operations with `{"ok": false, "error": "..."}`.
6. **Creative approval gate:** generated creatives are stored in the `creatives` table as `pending`. `apply_plan` creates ads only for creatives marked approved; `rotate_creative` can attach only an already approved creative (by a human via `live approve --run-id ...`). Unapproved creatives are never sent to Meta.
7. **Resumable checkpoints:** each `live checkpoint` invocation loads the run file, checks `due_at` and refuses to act early, calls `run_checkpoint`, and saves state. Scheduling (cron, `/schedule`, or manual) is external to the backend.
8. **Minimum-data rule:** if an ad set's delivery or attributed conversions are below a configured threshold, or the attribution window has not closed for the observed period, the observation marks the entry as `provisional` and the run takes no budget-moving actions that checkpoint. Re-read the same period at the next checkpoint once the window closes, and store both readings.
9. **Spend cap:** enforce `max_total_spend` over the campaign lifetime and `max_period_spend` over the configured checkpoint period before every write in `apply_plan` and `act`. Persist period boundaries and spend to date so restarts and rereads cannot reset the cap. A calendar-day cap, if required, is configured separately. Budget changes that exceed a cap return `{"ok": false, "error": "..."}`.
10. Fields Meta cannot supply (`holdout`, Google fields) are `None`; creative age is tracked in the `creatives` table.

**Done when**

- A dry run produces the same state and response shapes as a live run, without Graph API writes.
- A live create command makes only paused objects, and only from approved creatives.
- `observe()` returns normalized data while retaining the raw Meta response in live-run state for auditability.
- Two `live checkpoint` runs on different dates, in separate processes, continue the same run from the saved file; a checkpoint run before `due_at` exits without writes.

### Phase 7 — Unified CLI and operational guardrails

**Files**

```text
ad_swarm.py
```

**Tasks**

1. Add `simulate run`, `simulate improve`, and `simulate report` commands.
2. Add `live create`, `live approve`, `live observe`, and `live checkpoint` commands. `live checkpoint` calls `run_checkpoint` and exits; it is meant to be triggered on a schedule.
3. Require an explicit `live activate --run-id ...` command to turn on ads. The default is paused creation. Activation requires approved creatives and a configured spend cap.
4. Print the backend, model configuration, playbook version, account/run ID, and whether an API write will occur before each command executes.
5. Keep all run state in the unified store, keyed by `run_id` with `mode` of `sim` or `live`; HTML reports go to `runs/<name>/`.

**Done when**

- A user can select simulator or Meta without modifying source code; the agents run unchanged in both.
- No live command can activate a campaign accidentally.

### Phase 8 — Later: real lift experiments and visual review

**Tasks**

1. Design and implement a Meta Conversion Lift or approved audience-split integration for `set_holdout`.
2. Normalize completed lift-study results into a separate live-measurement section of the observation.
3. Add `NVIDIA-Nemotron-3.5-Super-VL` only for supplied creative images or video frames.
4. Store visual-review input references and structured feedback in the run store.
5. Add the `set_creative` action and per-creative fatigue modeling to the simulator (deferred from Phase 0.5).

**Done when**

- Live lift reports clearly identify experiment status, measurement window, estimate, and confidence interval.
- The results page labels simulator scores and live outcomes separately.

## Recommended execution order

1. Phase 0
2. Phase 0.5 (schema v2)
3. Phase 0.6 (run store)
4. Phase 1
5. Phase 2
6. Phase 3
7. Phase 4
8. Phase 5
9. Phase 6
10. Phase 7
11. Phase 8

Do not start the live backend before Phase 5 produces an auditable simulator run. It would create two debugging surfaces before the agent behavior, model routing, playbook format, and validation loop are proven.

## First implementation slice

The first code change should deliver a vertical slice, not all roles at once:

1. Add `playbooks/v1.md`.
2. Add `agents/models.py` and a validated Planner response.
3. Add `agents/runner.py` that applies the plan, advances the simulator through four checkpoints without channel actions, and logs a trace.
4. Run it through one simulator campaign.
5. Add the Channel and Verifier only after the planner path is reliable.

This isolates model connectivity and schema validation before introducing sequential action decisions, improvement rounds, or Meta API writes.
