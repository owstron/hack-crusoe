# Architecture

Two loops. The **inner loop** is the agent team playing one ad campaign by following the current playbook. The **outer loop** is the team improving itself: it studies its worst campaigns, proposes one playbook edit, and keeps the edit only if a code gate measures an improvement on campaigns the improver never studied.

Everything that decides what counts as better (simulator, score, gate, final eval) is plain code. Models play campaigns and propose edits; they never score anything.

## 1. Inner loop: one campaign

```mermaid
flowchart TB
    PB[/"Playbook vN<br/>numbered rules in markdown"/]

    subgraph RUN["agents/runner.py: one campaign, 4 weekly checkpoints"]
        direction TB
        VD["Verifier: definition of done<br/>Nemotron-3-Nano-Omni-Reasoning"]
        PL["Planner<br/>Nemotron-3-Super-120B<br/>budget split, holdouts, hypotheses"]
        subgraph WEEKLY["after weeks 1-3"]
            direction LR
            MA["Meta channel agent<br/>Nemotron-3.5-Lightning<br/>prospecting, retargeting"]
            GA["Google channel agent<br/>Nemotron-3.5-Lightning<br/>non-brand, brand"]
        end
        VAL{{"Code validators<br/>agents/schemas.py<br/>invalid JSON never reaches the simulator"}}
        VV["Verifier: final verdict<br/>pass / fail / inconclusive"]
    end

    SIM[("Simulator: sim/simulator.py<br/>hidden truth per line item:<br/>incrementality, saturation,<br/>creative fatigue, noise")]
    SC["Score, code only<br/>true incremental conversions<br/>÷ best possible"]

    PB --> VD
    PB --> PL
    PB --> MA
    PB --> GA
    PL -->|plan JSON| VAL
    MA -->|actions JSON| VAL
    GA -->|actions JSON| VAL
    VAL -->|apply_plan / act| SIM
    SIM -->|"observe: spend, CPA, CTR, frequency,<br/>impression share, holdout lift"| MA
    SIM -->|observe| GA
    SIM -->|final observation| VV
    SIM -. "hidden truth (agents never see it)" .-> SC
```

- **Actions:** `shift_budget` (to any line item), `pause`, `rotate_creative`, `set_holdout`. They apply to the next week.
- **What agents see** is shaped like Meta Insights and Google Ads reports: spend, platform-attributed purchases, CPA, CTR, CPM, CPC, frequency (Meta), search impression share (Google), ROAS, and holdout lift estimates for line items they chose to test. Holdouts cost real conversions.
- **If a model call fails**, the failure is logged and the campaign continues on its current budgets. The gate counts crashes.

## 2. Outer loop: Version N → N+1

```mermaid
flowchart TB
    CUR[/"Playbook vN"/] --> TR["Play 6 TRAIN campaigns<br/>(inner loop)"]
    TR --> FL["3 worst runs + debrief<br/>hidden truth, TRAIN only"]
    FL --> SUM["Summarizer, if long<br/>Nemotron-3-Nano"]
    SUM --> DG["Improver: diagnose<br/>Nemotron-3-Super-120B"]
    DG -->|one research question| YOU[("You.com Research API<br/>cached on disk")]
    YOU -->|cited answer + sources| ED["Improver: edit ONE rule<br/>Nemotron-3-Super-120B"]
    ED --> CAND[/"Candidate vN+1"/]
    CAND --> GATE{{"Gate, code only: sim/gate.py<br/>vN vs vN+1 on 6 VALIDATION campaigns<br/>× 3 seeds, same seeds for both"}}
    GATE -->|"gain > 0.01 and 95% CI > 0: accept"| NEXT[/"vN+1 becomes current"/]
    GATE -->|"otherwise: reject, keep vN"| CUR
    NEXT --> CUR
    GATE --> LOG[("Change log<br/>diff, rationale, sources, gate result<br/>→ results page")]
    NEXT -. "after the last round, once" .-> FE["Final eval: sim/final_eval.py<br/>v1 vs final on 6 TEST campaigns<br/>incl. a trap never seen in training"]
```

- **Three-way split:** the improver studies **train**, the gate decides on **validation**, and **test** is used once at the end for the before/after score.
- **The test set contains a trap variation the team never trained on:** brand search is genuinely incremental in train and validation but over-credited in test (as in eBay's paid-search experiment). A team that memorized "brand is good" fails; a team that learned "test cheap last-click items before scaling" passes.
- **You.com only shapes which edit is proposed.** Whether it is kept is decided by the gate on the deterministic simulator. Answers are cached, so reruns see the same research.

## Agents

| Agent | Model | When it runs | Reads | Writes | Never sees |
|---|---|---|---|---|---|
| Planner | `nvidia/NVIDIA-Nemotron-3-Super-120B-A12B` | Before week 1 | Playbook, public campaign info | Plan: budget shares, holdouts, hypotheses | Hidden truth, score |
| Meta channel agent | `nvidia/Nemotron-3.5-Lightning-30B-A3B` | After weeks 1-3 | Playbook, latest observation for its line items, all CPAs | Actions on Meta prospecting / retargeting | Hidden truth, score |
| Google channel agent | `nvidia/Nemotron-3.5-Lightning-30B-A3B` | After weeks 1-3 | Same, for Google non-brand / brand | Actions on Google line items | Hidden truth, score |
| Verifier | `nvidia/Nemotron-3-Nano-Omni-Reasoning-30B-A3B` | Before week 1 and after week 4 | Playbook and campaign; final observation | Definition of done; verdict | Hidden truth, score |
| Summarizer | `nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B` | Each improvement round, if the failure report is long | Worst train runs | Compressed post-mortem | Validation and test campaigns |
| Improver | `nvidia/NVIDIA-Nemotron-3-Super-120B-A12B` | Each improvement round (2 calls) | Playbook, 3 worst **train** runs with hidden truth, earlier edits, one You.com answer | One playbook edit + rationale + sources | Validation and test campaigns |

All models run on Crusoe Managed Inference at temperature 0, and the model configuration is fixed across a gate comparison, so the gate measures the playbook edit, not sampling.

## Code components (no models)

| Component | File | Role |
|---|---|---|
| Simulator | `sim/simulator.py` | 4 line items × 4 weekly checkpoints; 6 research-based traps from 4 mechanisms ([sim_calibration.md](sim_calibration.md)) |
| Score | `sim/simulator.py: score()` | True incremental conversions ÷ best possible for the budget, from hidden truth |
| Validators | `agents/schemas.py` | Reject malformed plans and actions before they reach the simulator |
| Gate | `sim/gate.py` | Paired comparison on validation; accept/reject |
| Improvement loop | `improve/loop.py`, `run_improvement.py` | Rounds of train → improver → gate → keep or revert |
| Research tool | `improve/research.py` | You.com Research/Search with disk cache |
| Final eval | `sim/final_eval.py` | One-time v1 vs final on test |
| Run store | `store/` | SQLite record of runs, model calls, actions, change log; hidden truth readable only via `store/truth.py` |
| Results page | `report/` | Static HTML: score per round, accepted/rejected edits with diffs, test before/after |
| Backends | `backends/` | Simulator today; a paused-by-default Meta backend is the next step |
