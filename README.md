# hack-crusoe

A self-improving agent team, built on Crusoe Managed Inference. The team runs ad campaigns in a simulator with hidden ground truth, rewrites its own playbook after each run, keeps a change only if it measurably helps, and proves the gain on campaigns it has never seen.

Hackathon theme: **Version N+1** (with **Done Means Done** as the verifier).

- [ARCHITECTURE.md](ARCHITECTURE.md): how the agents connect (diagrams of one campaign and of the improvement loop)
- [planning/brainstorm.md](planning/brainstorm.md): project brief, architecture, and task split
- [sim_calibration.md](sim_calibration.md): research behind every simulator setting and trap
- [research/youdotcom/](research/youdotcom/): raw You.com research responses with sources
- [planning/HACKATHON.md](planning/HACKATHON.md): event rules and judging criteria

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env   # then add your keys
.venv/bin/python smoke_test.py
```

## Run a simulation

```bash
# loop smoke test with model-free stubs
.venv/bin/python run_improvement.py --name smoke --rounds 2
# agent swarm (once agents.runner / agents.improver exist; see planning/action_plan.md)
.venv/bin/python run_improvement.py --name main --rounds 6 \
    --runner agents.runner:run --improver agents.improver:propose_edit
```

## Unified CLI

`ad_swarm.py` runs the same agents on the simulator or on Meta. Every command first prints the backend, models, playbook, run and whether an ad-platform write will happen.

```bash
.venv/bin/python ad_swarm.py simulate improve --name main --rounds 6 --runner agents.runner:run --improver agents.improver:propose_edit
.venv/bin/python ad_swarm.py simulate report --name main

.venv/bin/python ad_swarm.py live create --dry-run          # paused objects only; --dry-run makes no Graph API calls
.venv/bin/python ad_swarm.py live approve --run-id R --list # creatives stay pending until a human approves them
.venv/bin/python ad_swarm.py live approve --run-id R --all
.venv/bin/python ad_swarm.py live checkpoint --run-id R     # schedule externally; exits without acting when not yet due
.venv/bin/python ad_swarm.py live activate --run-id R --yes # the only command that turns ads on
```

Environment: `CRUSOE_API_KEY` and `CRUSOE_URL` (models), `YDC_API_KEY` (research); `META_*` only for live mode.

**Simulator score vs live metrics:** the simulator score comes from hidden ground truth and is the only thing that gates playbook changes. Live Meta metrics are platform-reported operational evidence; they never feed the gate, and simulator scores do not predict live results.

## What we built vs. third-party

*To be filled in before submission.*
