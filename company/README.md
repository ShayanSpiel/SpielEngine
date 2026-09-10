# SpielOS

SpielOS is a local clean-core runtime for durable Goals.

[spielos.xyz](https://spielos.xyz) · `GoalRuntime` owns the persisted loop:
observe, decide, act, and evaluate. Departments are declarative packages;
Agents, Skills, Connections, Capabilities, and Hosts provide replaceable
execution declarations. Every material result is recorded as Evidence and can
inform owner, workflow, or strategy Memory.

## Talk to your Director

```sh
opencode    # or: codex
```

In **OpenCode**: run `/agents`, select the **Director** agent, and talk to
it — it already sees your company state.

In **Codex**: talk to the **Director** agent — it already sees your company
state.

The host injects a fresh, read-only company projection into every model
request; do not begin with a manual status probe.

## Commands

```sh
spielos status            # one Goal or the company snapshot
spielos overview          # the full company projection
spielos observe           # read-only dashboard (health, goals, attention, repetition)
spielos observe --goal <id> # causal trace: Goal -> Runs -> Interventions
spielos context           # the same context your host injects
spielos memory summary    # owner, workflow, and strategy memory
spielos memory add --scope workflow --claim "..." --evidence <id> --goal <id> --run <id>
spielos memory retire <memory_id>   # flip one stale claim out of the active set
spielos profile list      # owner profile claims
spielos notifications list
spielos runner tick       # advance every ready Run once
spielos goal create --name "..." --owner director --metric ... --target ...
spielos goal list          # every Goal with its Run and stage
spielos goal decide <goal_id> --kind execute_workflow --workflow <department>:<workflow>
spielos goal decide <goal_id> --kind request_agent --agent <id> --instruction "..." --evidence-kind <kind>
spielos goal resume <goal_id>   # open the next run of a stalled or review-parked goal
spielos approve <goal_id> --note "..." [--key legal] [--scope run]
spielos tasks             # open work orders
spielos tasks <id> --complete <agent> --evidence '[...]' [--learning "claim"]
spielos layout            # audit the canonical layout for drift
spielos update --dir .    # refresh this home (global CLI, after pipx upgrade)
```

Always run `update` through the global `spielos` command — it refreshes the
home from the installed release's templates. Running
`PYTHONPATH=.agents python3 -m company update` inside the home would try to
copy the home's own files onto themselves. `update` overwrites the vendored
`.agents/` spine and the host-adapter files the release itself ships
(Director prompts, Codex hooks, the notifications plugin) — those always
refresh to the current release bytes, including in homes created before the
vendored manifest existed. Private `.spielos/` state,
`opencode.json`/`AGENTS.md` owner edits, and every owner-created file in the
user layers (Departments, Skills, Capabilities, Connections, Strategy,
installed Agents, host agents/commands/plugins) are always preserved. In a
home with a vendored manifest, stale files from older releases are pruned; a
pre-manifest home has no history to consult, so it keeps every file the
release does not ship and the manifest written by the update resolves that
on the next one.

## Executor identity (declared-agent claims)

WorkOrder execution is bound to the declared agent. A step's order belongs
to the agent its workflow step declares; a direct order belongs to the
agent the owner named. Only that exact identity — matched as an exact
string, with no aliases or normalization — can claim, renew, fail, or
complete the order, and an expired lease can be re-claimed only by the same
declared agent. The documented flow is claim-then-complete
(`tasks <id> --claim <agent>`, then `tasks <id> --complete <agent>`):
completing an open order is refused until it is claimed, and work for an
undeclared executor is refused upfront — a workflow step (or direct
assignment) whose agent is neither installed, nor the goal owner, nor
declared by the owning Department escalates to the owner with a message
naming the agent, and no WorkOrder is created for it. The goal owner
completes a direct order whose declared agent is the owner by
construction, not by override.

## The DECIDE boundary

`DECIDE` is the reasoning seam: a Run the runtime cannot decide for the
owner parks a structured decision request instead of inventing work.

- A Goal whose owner is not a Department (or whose Department declares no
  workflow) parks a decision request: the Run stays at DECIDE/waiting, one
  owner notification carries what is needed, why, the candidate Departments
  and workflows that declare the metric, and the installed Agents. No
  Intervention and no WorkOrder is created — no content-free "choose
  bounded work" order can exist. The owner-facing fields are owner prose
  (goal name, human progress, named options); the exact answer syntax rides
  the payload for the Director, which records the owner's plain-words
  answer through the CLI itself.
- The owner answers with `goal decide`: `--kind execute_workflow` adopts
  one of the candidate workflows, or `--kind request_agent` assigns bounded
  direct work whose instruction is mandatory (an empty instruction is
  refused).
- Stalls park: when a Goal's evaluated metric holds the same value across
  `stall_threshold` runs (default 3) and a run produced no new evidence,
  the next run is created parked with a continue/adjust/pause ask; a
  `review_every` config parks at that cadence. `goal resume` opens the next
  run; a DECIDE park is refused until it is decided.
- Progressing runs chain automatically. There are no per-run owner gates:
  EVALUATE opens the next Run on its own, and the loop parks only at
  approval or authority boundaries, a decision request, a stall or review
  threshold, or when a goal-level decision keeps escalating.

## Memory taxonomy, retrieval, and causal injection

Memory has one classification: owner preferences, constraints, and
authority live in owner scope (written with `profile set`, no evidence
needed); operational lessons live in workflow scope (written with
`tasks <id> --complete --learning`, conditional on something genuinely
reusable being learned); owner strategic direction stated during tasks
lives in strategy scope (written with `memory add --scope strategy`, or
distilled at an evaluation boundary when the evidence genuinely changes
a future Goal-level choice). Completing work without a lesson writes no
memory, and the deterministic catalog never fabricates strategy claims.

Memory retrieval is topology-aware: `relevant(goal_id=B)` returns B's own
strategy claims plus the active strategy claims of Goals B is
structurally related to — siblings (same owner and metric), its parent,
its children, and both directions of a `supports` edge — never the
claims of unrelated Goals. Direct-work lessons are goal-keyed: a
workflow-scope claim with no workflow_id (written by `tasks <id>
--complete --learning` on a direct order) reaches future direct orders
of that same Goal exactly. Owner and workflow scoping are otherwise
unchanged.

Memory is causal, not decorative: every WorkOrder brief carries a
bounded `memory` list — the workflow's own recorded learning for
workflow orders, the goal-relevant claims (never owner profile claims)
for direct ones, including the goal's own direct-work lessons
(workflow_id NULL, written by `tasks --complete --learning`), so the
executor's next execution builds on what the last one learned. Parked
asks render the same learning as
`Workflow learning: <claims>` / `Relevant memory: <claims>` lines; with
no claims recorded the brief key and the line stay empty.

A retired claim (`memory retire <id>`) keeps its row and evidence but
leaves the active set, so retrieval and briefs stop carrying it.

`spielos observe` also surfaces a repetition signal: three or more
completed direct work orders with the same instruction on one goal appear
as a bounded `repetition` entry suggesting the work may merit a reusable
Workflow proposed through adoption.

## Context projection

`spielos context` focuses the Goal the scheduler would run next: the
focus follows `runs.ready()` priority order (deadline/priority aware —
the same order the loop schedules in), falling back to the most recently
updated active Goal when nothing is ready. For the focus goal the
projection renders a `Recent decisions` line (each of the last 3 runs in
plain words) and a `Departments that can move this goal` line, each only
when it has content.

## Owner voice

Owner-facing text is human narration from start to finish: goals render by
name with human progress ("0 of 1 customers per week"), evidence renders as
outcome sentences ("replies 2, sent 10000"), the loop position renders in
plain words ("next step: choosing the next move"), and parks ask in plain
words with named options. Raw ids, metric keys, stage/status enums,
payloads, and CLI answer syntax never enter owner-facing text: the
projection carries them in one compact `Machine reference` line at the
end, and parked asks carry them in their payload machine fields — the
Director quotes them only when the owner asks for technical detail. The
owner answers in plain words; the Director records the answer through the
CLI (`goal decide`, `goal resume`, `approve`).

## Owner-ask hygiene

`approve` answers the current intervention's pending ask — the
notification is acknowledged before the run resumes, so an approved ask
stops re-delivering while a new gate's own ask stays pending. Approving
`--scope step` without an active intervention is refused with the right
answer path: a DECIDE park is answered with `goal decide`, a stalled or
review-parked run with `goal resume`. Topology audits no longer flag a
healthy home with several independent root goals — the root ids are
reported, and only genuine defects (cycles, missing parents, missing
edge goals, abandoned blockers) count. Unknown comparison operators
raise instead of silently failing closed.

## Canonical layout

Company content lives in exactly these layers under `.agents/company/`:

| Layer | Contents |
|---|---|
| `departments/<id>/department.py` | one Department declaration per folder |
| `skills/<id>/SKILL.md` | one reusable Skill per folder |
| `capabilities/<id>/` | capability packages |
| `connections/` | connection registry and client modules |
| `strategy/` | canonical strategy documents |
| `agents/installed/` | installed worker Agents |

Never invent folders or files outside these layers; the Director is the host
agent prompt, not a Skill. Run `spielos layout` to audit drift.

## Vocabulary

| Concept | What it is |
|---|---|
| **GoalRuntime** | The one durable control loop |
| **Goal** | A measurable outcome with an owner, metric, and target |
| **Department** | A declarative, Agent-owned capability package |
| **Workflow** | Ordered steps with declared evidence and approval keys |
| **Agent** | Performs claimed work orders through a Host |
| **Evidence** | Immutable proof attached to Goals and Runs |
| **Memory** | owner / workflow / strategy scopes only |
| **Approval** | Explicit owner keys; external actions park first |

A new home starts with no bundled Departments. Add clean declarative packages
only when their Goal, Workflow, Agent, Evidence, and approval contracts are
ready.
