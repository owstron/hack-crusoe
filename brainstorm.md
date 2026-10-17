# Hackathon Project Brief: Self-Improving Ad Campaign Agent Team

Status: v2. Updated against HACKATHON.md rules and measured Crusoe API results. Team of 2.

## 1. Constraints (from HACKATHON.md)

- **Build window: 10:45 AM to 5:00 PM (about 6¼ hours).** Submissions due at 5:00.
- **Demo: about 3 minutes**, plus 1-2 minutes of Q&A. Top 6 teams demo again on stage.
- **Public repo. New work only:** no project code written before 10:45. The demo must show only what we built during the event. Planning, docs, and API tests beforehand are fine.
- **Banned:** Streamlit apps, and any project where a dashboard is the main feature. Our results page is a window into the agents, not the product.
- **Judging:** Technicality 40%, Creativity 25%, Live Demo 20%, Future Potential 15%. In the final round, all four count equally.
- **Data rights:** use only data we have rights to. We generate our own data (the simulator), so this is covered.

## 2. Theme and pitch

**Theme: Version N+1** (primary). **Done Means Done** is folded in as the verifier. Shared Brain does not fit, so we don't claim it.

One-line pitch: *An agent team runs ad campaigns in a simulator with hidden ground truth, rewrites its own playbook after each run, keeps a change only if it measurably helps, and proves it on campaigns it has never seen.*

Ads are the setting, not the product. The claim is "the team measurably improved itself and the improvements were checked against ground truth", **not** "this makes better real-world campaigns".

## 3. Why a simulator, not a dataset

Real ad data records who clicked and converted, not what those people would have done without the ad, so true incremental lift can't be scored from it. The simulator knows the true lift because we set it and hide it from the agents. The simulator settings, the campaign generator, and the agents' run logs are all the data we need. We build them during the event.

## 4. Architecture

### Simulator (plain Python + numpy, ~150 lines, deterministic given a seed)

Settings are sampled from real-world ranges in [sim_calibration.md](sim_calibration.md) (researched with You.com, with sources). Line items: Meta prospecting, Meta retargeting, Google non-brand search, Google brand search.

- Each campaign has a product, a budget, 2 channels (Meta, Google), and 2-3 audiences per channel (e.g. prospecting, retargeting).
- Each line item has hidden settings: true incremental conversion rate, baseline conversion rate (people who would convert anyway), how fast returns drop off as spend grows, and how fast creative wears out.
- The simulator runs in **4 checkpoints**, not daily pacing. At each checkpoint the agents see observed metrics (spend, last-click conversions, CPA). They see holdout results only for line items where they chose to run a holdout test.
- **Traps (6, each based on research; full sources in [sim_calibration.md](sim_calibration.md) section 7):**
  1. Retargeting over-credit: great last-click CPA, near-zero true lift. *(Google ghost-ad study, 432 experiments)*
  2. Brand search over-credit: the same trap on Google. **Held back for test campaigns only.** *(eBay experiment, Econometrica 2015)*
  3. Prospecting under-credit: the line item that looks worst on last-click is actually the best. *(Haus, 640 Meta experiments; Gordon et al., 663 Facebook RCTs)*
  4. Diminishing returns: scaling a winner past saturation wastes budget. *(Johnson, Lewis & Reiley; Försch & de Haan 2018)*
  5. Creative fatigue: performance decays unless creative is rotated. *(Braun & Moe 2013)*
  6. Small-sample noise: early "winners" are random luck, and holdout tests on small line items are inconclusive. *(Lewis & Rao, QJE 2015)*
- **Scope note:** the 6 traps come from only 4 simulator mechanisms (incrementality ratio, saturation curve, fatigue decay, noise). Traps 1-3 are all the incrementality ratio with different values, so traps 2 and 3 add settings, not code.
- **Test campaigns contain trap variations not in train or validation:** brand search over-credit appears only in test, plus different fatigue curves.

### Score (code only, never an AI model)

`score = true incremental conversions achieved / best possible incremental conversions for that budget`, using the simulator's hidden truth. Range 0 to 1.

### Agent team (the thing that gets improved)

| Agent | Model | Role |
|---|---|---|
| Planner | gpt-oss-120b | Reads the playbook, outputs a media plan as JSON: line items, budgets, a hypothesis per line item, which line items get a holdout test |
| Channel agents (Meta, Google) | gpt-oss-120b | At each checkpoint, read metrics and act through tools: shift budget (within limits), pause, rotate creative |
| Verifier | gpt-oss-120b | Writes a definition of done up front (hypotheses plus lift checks). At the end, checks results against it. Lift and confidence intervals are computed in code; the verifier interprets them |

A plain Python loop plays orchestrator. It is not an agent.

### Improvement layer

| Component | Role |
|---|---|
| Improver | DeepSeek-V4-Pro. Reads failure traces from **train** campaigns, researches the failure with You.com (see below), and proposes one specific edit to the playbook or a prompt, with a rationale and cited sources |
| Research tool | You.com Research API (`research_effort: "lite"`, ~6s) or Search (~1s). At most one call per improvement round. Responses cached to disk by question |
| Gate (code) | Runs Version N and candidate N+1 on **validation** campaigns, same seeds for both, temperature 0. Accepts only if the mean score improves by more than a set margin (paired comparison) |
| Change log | JSON-lines file: every proposed edit, its diff, cited sources, validation score change, accepted or rejected |
| Final eval | Runs Version 1 and the final version once on **test** campaigns. Test is never used for decisions |

**How You.com fits:** search results only affect *which edits get proposed*. Whether an edit is kept is decided by the gate on the simulator, which is deterministic. Live search results change over time, so they never touch scoring. Pitch line: *the team reads the web to form hypotheses about its own failures, then tests each one before trusting it.*

Verified 2026-10-17: the Research API answered "why does retargeting show low incremental lift despite strong last-click CPA?" in 6s with a cited answer recommending holdout tests.

**Data split (to avoid leakage):** train ~6 campaigns (improver studies these), validation ~6 (gate decides), test ~6 (final score only, touched once).

### State

Playbooks and prompts are versioned files (`playbooks/v1.md`, `v2.md`, ...), so any version can be rerun. No database, no crash recovery.

## 5. Measured model results (2026-10-17, 20 calls each, 16 at a time)

| Model | Tool calls | JSON plan format | Typical latency |
|---|---|---|---|
| openai/gpt-oss-120b | 20/20 | 20/20 | 1.5-2.4s |
| deepseek-ai/DeepSeek-V4-Pro | 20/20 | 20/20 | 2.7-3.0s |
| nvidia/Nemotron-3-Super-120B | 19/20 | 20/20 | 14-17s |
| nvidia/Nemotron-3.5-Lightning | 20/20 | 7/20 | 5-25s |
| zai-org/GLM-5.3-Flash | 20/20 | 8/20 | 7-17s |
| deepseek-ai/DeepSeek-V4.1-Flash | 15/20 | 0/20 | 7-19s |

No rate limiting seen at 16 parallel calls. Reasoning models need a large `max_tokens` (1000+) or they return empty text; treat empty responses as failures, not crashes.

**Budget estimate:** ~10 model calls per campaign, about 25s. One gate check (2 versions × 6 campaigns × 3 seeds = 36 runs, 16 at a time) takes about 1.5-2 minutes, so 5-8 improvement rounds fit in the afternoon.

## 6. Work breakdown (2 people)

**Person A: Environment and evaluation** (deterministic code). Owns simulator, generator, score, lift calculator, gate, final eval.
**Person B: Agents and improvement** (model-facing code). Owns model client, agents, tools, playbook v1, improver, change log, results page.

### Status (12:54)

**Person A's code is done and pushed** (`sim/`):
- `sim/interface.py`: the contract for B (plan, action and observation formats). Read this instead of the suggested interface below.
- `sim/simulator.py`: simulator with all 6 traps, `generate_campaigns`, `score`, `sim.debrief()` (hidden truth, train only). Holdout results include the lift estimate and 95% CI, so the lift calculator is built in.
- `sim/baselines.py`: sanity check passes. Naive last-click 0.76 / 0.74 / **0.56** (train / val / test) vs smart holdout 0.90 / 0.92 / **0.92**. Naive collapses on the test-only brand trap.
- `sim/gate.py`: paired gate on validation, 6 campaigns × 3 seeds = 18 pairs, 16 concurrent. Accepts if mean improvement > 0.01 **and** the 95% CI excludes 0. Agent crashes finish on autopilot and count as errors; >25% crashed runs means reject. Also serves as the parallel runner (`evaluate_version`).
- `sim/final_eval.py`: Version 1 vs final on test, writes `results/final_eval.json` once (refuses to overwrite).

**Plug-in points for B** (both sync or async; contract in `improve/loop.py`):
- `runner(version, sim)`: plays a whole campaign. `version` = playbook path. Anything appended to `sim.trace` (e.g. agent reasoning) is shown to the improver.
- `propose_edit(playbook, failures, history) -> {"new_playbook", "summary", "rationale", "sources"}` or `None` to stop. `failures` = the 3 worst train runs with hidden truth, weekly metrics and actions (~3k tokens). `history` = earlier proposals with accepted/rejected and score change.
- Run: `.venv/bin/python run_improvement.py --name main --rounds 6 --runner agents.runner:run --improver agents.improver:propose_edit`. Without the two flags it runs model-free stubs (`improve/stubs.py`): 3 edits accepted, 1 rejected, test 0.56 → 0.92.

**Moved from B to A, all three done and tested with model-free stubs:**
1. ✅ Improvement loop driver + change log (`run_improvement.py`, `improve/loop.py`). The improver is a plug-in: B writes the prompt and model call.
2. ✅ You.com tool (`improve/research.py`): `research(q)` (Research API, ~8s) and `search(q)` (~1s) both return `{answer, sources, cached}`; `format_for_prompt(r)` makes a prompt block; `RESEARCH_TOOL` is an OpenAI-format tool definition. Cached in `research/cache/` (committed, so reruns get identical answers). Errors come back as `{"error": ...}`, never raised. `YDC_DISABLED=1` turns it off (cache included) for the with/without-You.com comparison.
3. ✅ Results page (`report/`): static HTML, no server. `.venv/bin/python -m report.build_page --name main --watch` rebuilds `runs/main/index.html` every 5s during a run (the page auto-reloads until the run finishes). Shows test before/after tiles, validation score per round (accepted vs rejected edits, hover for details), test budget share per line item vs optimal, and the change log with sources and playbook diffs. For the final eval, save to `runs/<name>/final_eval.json` (`final_eval(..., out_path=...)`) so it appears on the page.

**B keeps:** model client, planner, channel agents, verifier, playbook v1, improver prompt and call, trace view, backup demo video.

**Blocked on B:** nothing can run end to end until `runner` and `propose_edit` exist (no agent code pushed as of 12:54). Version 1 score target: ~2:30.

**Revised timeline** (replaces the times in the sections below; we started ~1.5h behind):
- ~2:30: Version 1 agents score on validation (A checks the score is well below 1.0)
- 3:30 at the latest: full improvement run started (5-8 rounds, ~2 min each)
- 4:15: feature freeze
- 4:50: submit

**Afternoon run order** (once B's `runner` and `propose_edit` exist):
1. One campaign end to end: `Simulator(generate_campaigns(1, "train")[0], seed=0)` → `runner("playbooks/v1.md", sim)` → `score(sim)`.
2. Version 1 on validation: `asyncio.run(evaluate_version(runner, "playbooks/v1.md", "validation"))`. Compare with `python -m sim.baselines` (naive ≈ 0.74).
3. Full run: `.venv/bin/python run_improvement.py --name main --rounds 6 --runner agents.runner:run --improver agents.improver:propose_edit`, with `python -m report.build_page --name main --watch` in a second terminal.
4. Final eval, once: `final_eval(runner, "runs/main/playbooks/v1.md", <final_version from runs/main/summary.json>, out_path="runs/main/final_eval.json")`, then rebuild the page.
5. Commit `runs/main/` (playbooks, change log, final eval, page) so the results are in the public repo.

**Simulator facts for B's prompts and for Q&A:**
- Agents see only last-click metrics plus holdout results for line items they chose to test. Holdouts cost that share of true conversions.
- Brand search is genuinely incremental in train/validation (0.60-0.90) but over-credited in test (0.00-0.25). The improver must learn "test cheap last-click items before scaling", not "brand is good".
- Rotating creative is free; rotating weekly alone is worth ~+0.08. Expect it to be the first accepted edit.
- Holdouts on small line items are noisy: the `conclusive` flag can be wrong (trap 6). Prompts shouldn't treat one week's holdout as proof.
- The score uses expected (noise-free) conversions, so it depends only on the agents' decisions; noise is only in what agents observe.
- Model-free reference points: equal split 0.74 / 0.73 / 0.59, naive last-click 0.76 / 0.74 / 0.56, smart holdout 0.90 / 0.92 / 0.92 (train / val / test). Oracle = 1.0.

**Remaining for A:** check Version 1's agent score (if near 1.0, make traps harder), start the full run by 3:30 at the latest, watch it, final test eval, Q&A prep, README. Stretch: with/without-You.com comparison run (`YDC_DISABLED=1`).

**Layout:** each improvement run lives in `runs/<name>/` (`playbooks/v1.md, v2.md, ...`, `candidates/`, `changelog.jsonl`), so a second run or the with/without-You.com comparison doesn't overwrite the first. The starting playbook is `playbooks/v1.md` (B).

### 10:45-11:00: both, agree on interfaces before splitting

- [x] Create the public repo, first commit (`.gitignore`, `requirements.txt`, README stub).
- [x] Agree on the simulator interface Person B codes against (done: `sim/interface.py`). Originally suggested:
  - `generate_campaigns(n, split, seed) -> list[Campaign]`
  - `sim = Simulator(campaign, seed)`; `sim.observe() -> dict` (visible metrics only); `sim.act(action) -> result`; `sim.step()` (advance one checkpoint); `sim.done`
  - `score(sim) -> float` (uses hidden truth)
- [x] Agree on the JSON formats for the media plan and for actions (shift_budget, pause, rotate_creative, set_holdout).
- [ ] Agree on the playbook format: a markdown file of numbered rules the planner and channel agents read.

### 11:00-12:15

| Person A | Person B |
|---|---|
| Simulator with hidden settings and 4 checkpoints | Model client wrapper: retries, empty-response handling, JSON parsing, call logging |
| The 6 traps (settings from sim_calibration.md) | Planner agent with strict JSON output |
| Campaign generator with train / validation / test splits (test gets new trap variations) | Channel agent with tools, coded against a stub simulator until A's is ready |
| `score()` and a lift calculator with confidence intervals (numpy/scipy) | Playbook v1: deliberately naive ("trust last-click CPA, scale winners") |
| **Sanity check:** a basic non-AI strategy runs and scores below a smart non-AI strategy | |

### 12:15-1:45: integrate, Version 1 end to end

- [ ] **Both:** connect agents to the real simulator. One campaign runs end to end.
- [ ] **B:** verifier agent (definition of done at the start, check at the end).
- [x] **A:** parallel runner: many campaigns × seeds at once, 16 concurrent calls (`sim/gate.py: evaluate_version`).
- [ ] **Milestone by ~1:45: Version 1 score on validation and test.** If Version 1 already scores near 1.0, the traps are too easy; A makes them harder.

### 1:45-3:00: improvement loop

| Person A | Person B |
|---|---|
| ~~Gate: paired comparison of N vs N+1 on validation, same seeds, accept margin~~ done | Improver agent: reads train failure traces, proposes one edit with a rationale |
| ~~Final eval script (test split, run once)~~ done | |
| ~~You.com research tool for the improver, with disk cache (moved from B)~~ done | |
| ~~Change log (JSON lines: diff, sources, score change, accepted or rejected) (moved from B)~~ done | |
| ~~Loop: improve → gate → keep or revert → next round (moved from B)~~ done | |

### 3:00: start the full improvement run (5-8 rounds). Do not slip this.

### 3:00-4:00

| Person A | Person B |
|---|---|
| Watch the run; fix crashes; if time, a second run to show the result repeats. Stretch: run an improver without You.com side by side and compare accepted edits and final test score | |
| ~~Results page (FastAPI or plain HTML, **not Streamlit**): score per version, accepted and rejected edits as diffs, final test before/after (moved from B)~~ done (`report/`) | |
| Final test-set evaluation: Version 1 vs final version | Trace view of one test campaign: Version 1 fooled by retargeting, final version not |

### 4:00-5:00: feature freeze

- [ ] **Both:** README listing what we built vs. third-party libraries, how to run it, the model choice table, and the data split.
- [ ] **B:** record a backup demo video.
- [ ] **A:** prepare Q&A answers (section 8).
- [ ] **Both:** rehearse the 3-minute demo at least 3 times. Submit by 4:50.

## 7. Demo (3 minutes)

1. **0:00-0:20:** "Agent teams are as good on run 100 as on run 1. Ours rewrites itself and has to prove each change."
2. **0:20-1:15:** Version 1 on a test campaign: last-click says retargeting is the winner, true lift says it's nearly worthless. The verifier's check fails.
3. **1:15-2:15:** The change log: the improver researched the failure with You.com and proposed edits with cited sources. The gate rejected one (the score dropped) and accepted another, with numbers.
4. **2:15-3:00:** Before vs. after on test campaigns it never saw, including a trap variation it never trained on. Close with why this matters for autonomous agents.

## 8. Q&A prep

- **"Isn't the simulator rigged?"** The gate rejected some edits, test campaigns have trap variations never seen in training, and the learned rules are general ("check incrementality before scaling retargeting"), not memorized numbers.
- **"Isn't it overfitting?"** Three-way split. The test set was used once, at the end.
- **"Is the improvement just noise?"** Same seeds for both versions, temperature 0, multiple seeds, and an accept margin.
- **"Why these models?"** Measured: section 5.
- **"Doesn't web search make results unreproducible?"** Search only shapes which edits are proposed; the gate decides on a deterministic simulator. Responses are cached, so reruns use the same results.
- **"Does this work for real ads?"** Not claimed. The claim is that the team measurably improves itself against ground truth. A real deployment would score against real holdout experiments.

## 9. Cut list

**Already cut:** crash recovery and persistence, Meta Ad Library / Google Transparency research, escalation ladder, extra channels, orchestrator agent, critic and experiment-design agents.

**Cut next if behind (in this order):** second improvement run, with/without-You.com comparison, creative-fatigue trap, verifier agent (keep the lift calculator in code), the results page's trace view (show the change log in the terminal instead).

**Never cut:** simulator with at least traps 1, 2 and 6 (retargeting, brand search for the test set, noise), score, train/validation/test split, improver, gate, final test before/after.

## 10. Before 10:45 (allowed: no project code)

- [X] Both teammates get Crusoe and You.com keys working (`smoke_test.py`).
- [ ] Agree on who is Person A and who is Person B.
- [ ] Read this brief, and agree on the section 6 interfaces on paper.
- [ ] Review the trap settings in sim_calibration.md, and check any [A] source you plan to quote on stage.
- [ ] Ask the organizers: submission format, whether we can list two problem statements, and whether You.com has a sponsor prize.

## 11. Open questions

- Research API vs. Search for the improver: Research gives cited, synthesized answers (~6s); Search is faster (~1s) but returns raw snippets. Default: Research, `lite` effort.
- Exact accept margin for the gate: set it after seeing how much scores vary between seeds around 1:45.
