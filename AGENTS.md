# SpielOS source checkout

This repository is the source product used to create and update SpielOS homes. It runs from source with:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -B -m company COMMAND
```

The public product uses one runtime-owned Goal loop and portable Agent-owned Departments. Do not reintroduce retired Workgroups, Workers, Workbooks, or Workkits into docs, commands, templates, or onboarding.

`company/init_templates/` is what `spielos init` ships. Keep executable spine files byte-identical between `company/` and `company/init_templates/agents/company/`. Private `.spielos/` runtime state is ignored by Git.

DECIDE is the reasoning seam of the one Goal loop: a Run the runtime cannot
decide for the owner parks a structured decision request (run DECIDE/waiting,
one owner notification, no Intervention and no WorkOrder) that the owner
answers with `company goal decide` — adopting a candidate Department workflow
or assigning bounded direct work whose instruction is mandatory. Stalled goals
(a metric flat across `stall_threshold` evaluated runs with no new evidence)
and `review_every` checkpoints park for `company goal resume`. Progressing
runs chain automatically: there are no per-run owner gates.

Memory is causal and classified: WorkOrder briefs carry the bounded active
learning for the work they open (workflow-scope claims for workflow orders,
goal-relevant claims for direct ones — including the goal's own direct-work
lessons, which are goal-keyed and reach future direct orders of the same
Goal), so the next execution builds on the
last one's evidence-backed lesson. Owner preferences write owner scope
(`profile set`), operational lessons write workflow scope
(`tasks --complete --learning`, only when something reusable was learned),
and owner strategic direction writes strategy scope (`memory add --scope
strategy`). The deterministic runtime fabricates none of these; a stale
claim is retired with `company memory retire` (row and evidence preserved).

System changes require a bounded system-improvement Goal, exact allowed files, and actual acceptance evidence. External actions—including publishing or sending—remain approval-gated.
