# hack-crusoe

A self-improving agent team, built on Crusoe Managed Inference. The team runs ad campaigns in a simulator with hidden ground truth, rewrites its own playbook after each run, keeps a change only if it measurably helps, and proves the gain on campaigns it has never seen.

Hackathon theme: **Version N+1** (with **Done Means Done** as the verifier).

- [ARCHITECTURE.md](ARCHITECTURE.md): how the agents connect (diagrams of one campaign and of the improvement loop)
- [planning/brainstorm.md](planning/brainstorm.md): project brief, architecture, and task split
- [planning/action_plan.md](planning/action_plan.md): the phased implementation plan
- [sim_calibration.md](sim_calibration.md): research behind every simulator setting and trap
- [research/youdotcom/](research/youdotcom/): raw You.com research responses with sources
- [planning/HACKATHON.md](planning/HACKATHON.md): event rules and judging criteria

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env   # then add your keys
.venv/bin/python smoke_test.py
.venv/bin/python -m pytest -q tests   # needs pytest (dev dependency group in pyproject.toml)
```

## How it works

- **One campaign.** A Planner turns the playbook into an initial budget plan, Channel agents (Meta, Google) act at each checkpoint, and a Verifier writes a definition of done up front and judges the evidence at the end. Agents only see observable metrics; the simulator's hidden truth never reaches them. Every model call, action and observation is stored in the run store (SQLite, `store/adswarm.db`).
- **The improvement loop** (`improve/loop.py`). Each round: play train campaigns, give the Improver the worst runs, let it research (You.com, up to two questions), and have it propose up to three candidate playbooks that each change one rule. Every candidate is scored against the current playbook on **validation** campaigns with a paired comparison. A candidate is accepted only if it beats the current playbook by more than the margin (default 0.01) **and** its 95% confidence interval excludes zero; at most the best passing candidate is kept per round.
- **Exploration.** After two rounds in a row with no accepted edit, the Improver gets a seeded random direction (for example "a cap on the share of budget any one line item may hold") and samples at temperature 1.0. An explore winner must also pass the gate again on fresh simulator seeds.
- **Guards.** Edits that repeat a rejected edit are dropped before the gate. Each gate entry records budget moves per run, and a candidate whose agents made far fewer moves than the base is flagged as idle (rules too vague to act on).
- **Splits.** Train is for the Improver, validation decides, and the **test** split is used once by the final evaluation (`--final-eval`, v1 versus the final playbook).
- **Models** (all temperature 0 except explore): Planner and Improver on `NVIDIA-Nemotron-3-Super-120B-A12B`, Channel agents on `Nemotron-3.5-Lightning-30B-A3B`, Verifier on `Nemotron-3-Nano-Omni-Reasoning-30B-A3B`, summarizer on `NVIDIA-Nemotron-3-Nano-30B-A3B`.

## Run a simulation

```bash
# loop smoke test with model-free stubs
.venv/bin/python run_improvement.py --name smoke --rounds 2

# agent swarm, full size (6 train campaigns per round; 6 campaigns x 3 seeds per validation or test eval)
.venv/bin/python run_improvement.py --name main --rounds 6 \
    --runner agents.runner:run --improver agents.improver:propose_edit

# a small, fast run
.venv/bin/python run_improvement.py --name quick --rounds 2 --campaigns 4 --seeds 1 --train-campaigns 2 \
    --runner agents.runner:run --improver agents.improver:propose_edit

# extend a finished or interrupted run (the --rounds value is the new total; same size flags as before)
.venv/bin/python run_improvement.py --name main --rounds 9 --resume \
    --runner agents.runner:run --improver agents.improver:propose_edit
```

Flags: `--rounds`, `--start` (starting playbook), `--margin`, `--overwrite`, `--resume`, `--campaigns`, `--seeds`, `--train-campaigns`, `--concurrency` (simultaneous model requests), `--final-eval`, `--db`. Resume restores the playbook, baseline, version list and history from the run store and continues from the next round; a round interrupted mid-way is redone from its start. Keep the size flags the same when resuming so scores stay comparable.

Environment variables: `CRUSOE_API_KEY` and `CRUSOE_URL` (models), `YDC_API_KEY` (research), `META_ACCESS_TOKEN`, `META_AD_ACCOUNT_ID`, `META_PAGE_ID`, `META_LINK` (live mode only). Optional tuning: `MODEL_CONCURRENCY` (default 6 in-flight requests), `MODEL_BACKOFF_CAP_S` (longest retry wait, default 10), `GATE_CONCURRENCY` (default 16 campaigns at once).

## Unified CLI

`ad_swarm.py` runs the same agents on the simulator or on Meta. Every command first prints the backend, models, playbook, run and whether an ad-platform write will happen.

```bash
.venv/bin/python ad_swarm.py simulate improve --name main --rounds 6 --runner agents.runner:run --improver agents.improver:propose_edit
.venv/bin/python ad_swarm.py simulate report --name main

.venv/bin/python ad_swarm.py live create --dry-run          # paused objects only; --dry-run makes no Graph API calls
.venv/bin/python ad_swarm.py live approve --run-id R --list # creatives stay pending until a human approves them
.venv/bin/python ad_swarm.py live approve --run-id R --all
.venv/bin/python ad_swarm.py live observe --run-id R        # read-only
.venv/bin/python ad_swarm.py live checkpoint --run-id R     # schedule externally; exits without acting when not yet due
.venv/bin/python ad_swarm.py live activate --run-id R --yes # the only command that turns ads on
```

### Live Meta backend (`backends/meta.py`)

- Meta only (`meta_prospecting`, `meta_retargeting`). Google line items and holdouts are rejected.
- Everything is created **paused**. Generated creatives are stored as `pending`; only creatives a human approves ever reach Meta.
- Each checkpoint is a separate process run. State lives in the run store, so a checkpoint run before it is due exits without reading or writing anything.
- An entry is `provisional` while its attribution window is open or an ad set is below the delivery or conversion thresholds; budget moves are refused until the data is final, and the period is re-read once the window closes (both readings are kept).
- Lifetime and per-period spend caps are checked before every budget write against spend persisted per period.
- A dry run (`--dry-run`) has the same state and response shapes as a live run and never calls the Graph API.

**Simulator score vs live metrics:** the simulator score comes from hidden ground truth and is the only thing that gates playbook changes. Live Meta metrics are platform-reported operational evidence; they never feed the gate, and simulator scores do not predict live results.

## Status

- The pipeline runs end to end in the simulator: train, research, candidate edits, paired gate, explore mode, final test eval, and the results page (`runs/<name>/index.html`).
- **No accepted improvement yet.** In the committed runs under `runs/` every candidate was rejected by the gate, so the final playbook is still v1. In `runs/main2` the final test eval compares v1 with itself (0.595 versus 0.592, a difference that is run-to-run noise). The most promising result so far was a fixed 80/20 starting split that beat v1 on all 4 validation campaigns (+0.058) but could not clear the confidence interval with so few pairs.
- Findings that shaped the Improver: vague or cautious rules make the agents stop moving budget, which scores like doing nothing, so candidate rules must be concrete and executable.
- The Meta backend is tested with a dry-run backend and a canned Graph API fake. It has **not** been run against the real Meta API.

## What we built vs. third-party

**Third-party services and libraries (not ours):**

- Crusoe Managed Inference and the NVIDIA Nemotron models it serves (the Planner, Channel, Verifier, summarizer and Improver models above).
- You.com research and search API, through the `youdotcom` SDK.
- Meta Marketing (Graph) API, called with `requests`.
- Python libraries: `openai` (as an OpenAI-compatible client for Crusoe), `httpx`, `requests`, `numpy`, `python-dotenv`, `pytest`; SQLite from the standard library.
- Published marketing-measurement research cited in [sim_calibration.md](sim_calibration.md) to set the simulator's parameters. It informed the numbers; no code was taken from it.

**Written by our team for this project (the repository, all committed on 17 October 2026):**

- The simulator with hidden ground truth, trap campaigns, scoring and baseline policies (`sim/`).
- The validation gate, final test evaluation and the improvement loop (`sim/gate.py`, `sim/final_eval.py`, `improve/`).
- The agents: Planner, Channel agents, Verifier, the runner and the Improver (`agents/`).
- The unified run store (`store/`), the shared backend interface with the simulator and Meta backends (`backends/`), the CLI (`ad_swarm.py`) and the results page (`report/`).

**Notes to confirm before submission:**

- `ad_swarm_full.py` is an earlier standalone Meta script. State when it was written and whether it predates the event, since the rules require clearly identifying what was created during the hackathon.
- Parts of the code (the Meta backend, unified CLI, resumable loop, explore mode and executable-rule prompts) were written with Claude Code, an AI coding assistant. Disclose AI assistance in the submission if the rules require it.
