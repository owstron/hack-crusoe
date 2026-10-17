# hack-crusoe

A self-improving agent team, built on Crusoe Managed Inference. The team runs ad campaigns in a simulator with hidden ground truth, rewrites its own playbook after each run, keeps a change only if it measurably helps, and proves the gain on campaigns it has never seen.

Hackathon theme: **Version N+1** (with **Done Means Done** as the verifier).

- [brainstorm.md](brainstorm.md): project brief, architecture, and task split
- [sim_calibration.md](sim_calibration.md): research behind every simulator setting and trap
- [research/youdotcom/](research/youdotcom/): raw You.com research responses with sources
- [HACKATHON.md](HACKATHON.md): event rules and judging criteria

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env   # then add your keys
.venv/bin/python smoke_test.py
```

## What we built vs. third-party

*To be filled in before submission.*
