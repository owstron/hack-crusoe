# Playbook v1

1. Trust last-click CPA only when validated by holdout-based incrementality tests; otherwise use incrementality-adjusted CPA for decisions.
2. Each checkpoint, scale winners: move half of the worst-CPA line item's budget to the best-CPA line item.
3. After any reallocation, verify total spend matches the original budget and document holdout evidence, incrementality metrics, and budget-owner sign-off with timestamps.