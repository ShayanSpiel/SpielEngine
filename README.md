# SpielOS

**The operating system for AI-run companies.**

[spielos.xyz](https://spielos.xyz) · one durable Goal loop, Agent-owned
Departments, approvals, evidence, and memory — all local, all on disk.

```text
GOAL → OBSERVE → DECIDE → ACT → EVALUATE
```

Turn business intent into measurable Goals. The Director Agent (in OpenCode or
Codex) owns the conversation; SpielOS owns the durable loop behind it: every
run, work order, approval, evidence record, and memory item is persisted in a
local SQLite database — never lost when a chat session ends.

## Install

One line with [pipx](https://pipx.pypa.io) (requires Python 3.11+):

```sh
pipx install spielos && spielos init --dir my-company -y
```

One line with Homebrew:

```sh
brew install shayanspiel/spielos/spielos && spielos init --dir my-company -y
```

One line with npm (thin launcher; needs the Python package too):

```sh
npm i -g spielos && pipx install spielos && spielos init --dir my-company -y
```

One line with curl:

```sh
curl -fsSL https://raw.githubusercontent.com/ShayanSpiel/SpielOS/main/install.sh | sh
```

The chosen folder becomes a self-contained **SpielOS home**: the runtime
spine, Director agent + host adapters for OpenCode and Codex, and empty
private state. The `spielos` command itself is a global CLI — your company
data always lives in the folder you chose.

## Talk to your Director

```sh
cd my-company
opencode    # or: codex
```

In **OpenCode**: run `/agents`, select the **Director** agent, and talk to
it — it already sees your company state.

In **Codex**: talk to the **Director** agent — it already sees your company
state.

No manual probing, no `status` commands to start: the host injects a fresh,
read-only company projection into every model request.

## Update

```sh
pipx upgrade spielos && spielos update --dir /path/to/your/home
```

`update` refreshes only the vendored spine and host adapters; your private
`.spielos/` state, owner profile, memory, and user Departments are always
preserved. Stale files from older releases are pruned so the home matches a
fresh install exactly.

## What's inside

| Concept | What it is |
|---|---|
| **GoalRuntime** | The one durable control loop: observe, decide, act, evaluate |
| **Goal** | A measurable outcome with an owner, metric, and target |
| **Department** | A declarative, Agent-owned capability package |
| **Workflow** | Ordered steps with declared evidence and approval keys |
| **Agent** | Performs claimed work orders through a Host |
| **Evidence** | Immutable proof attached to Goals and Runs |
| **Memory** | Three scopes: owner profile, workflow learning, strategy learning |
| **Approval** | Explicit owner keys — external actions always park first |

Fresh homes start with **zero** Departments — add clean declarative packages
only when their Goal, Workflow, Agent, Evidence, and approval contracts are
ready.

Executor identity is enforced: a WorkOrder executes only for its declared
agent (exact string, no aliases), claims are claim-then-complete, and work
for an agent that is neither installed, nor the goal owner, nor declared by
the owning Department is refused upfront and escalated to the owner instead.

Memory retrieval is topology-aware: a Goal's relevant memory carries the
strategy claims of Goals it is structurally related to (same owner and
metric, parent, child, or supports edge), parked asks append the workflow
or goal-relevant learning they should build on, and every WorkOrder brief
carries that bounded `memory` list so the next execution differs from the
last — memory is causal, not decorative. Strategy memory stays selective —
completing a goal writes no new strategy claims; owners and evidence-backed
host distillation are the only strategy writers, operational lessons are
the only per-completion writes, and stale claims retire with
`memory retire` without losing their evidence. Approvals acknowledge
their answered asks, and unknown comparison operators raise instead of
silently failing closed.

Useful commands:

```sh
spielos status
spielos overview
spielos context
spielos memory summary
spielos memory retire <memory_id>
spielos profile list
spielos notifications list
spielos runner tick
```

## Development

```sh
git clone https://github.com/ShayanSpiel/SpielOS.git
cd SpielOS
PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest company.tests.test_clean_core_acceptance
```

- [Architecture](company/ARCHITECTURE.md) — the clean-core contract
- [Website](https://spielos.xyz) · [GitHub](https://github.com/ShayanSpiel/SpielOS)
- MIT licensed
