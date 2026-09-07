"""The single clean-core SpielOS command surface."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .commands import CleanCommandRuntime
from .runtime.config import VERSION
from .runtime.paths import find_project_root
from .runtime.service import RunnerService

PROJECT_ROOT = find_project_root()
DEFAULT_DB = PROJECT_ROOT / ".spielos" / "state" / "company.sqlite"


def _json(value: str):
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def build_parser():
    parser = argparse.ArgumentParser(prog="spielos")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--version", action="store_true")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="create a clean SpielOS home")
    init.add_argument("--dir", default="."); init.add_argument("--force", action="store_true")
    init.add_argument("-y", "--yes", action="store_true"); init.add_argument("--json", action="store_true")
    for name in ("catalog", "departments"):
        commands.add_parser(name).add_argument("--json", action="store_true")
    for name in ("status", "overview"):
        item = commands.add_parser(name)
        if name == "status": item.add_argument("goal_id", nargs="?")
        item.add_argument("--json", action="store_true")
    layout = commands.add_parser("layout", help="audit the canonical home layout")
    layout.add_argument("--json", action="store_true")
    observe = commands.add_parser("observe", help="read-only company dashboard")
    observe.add_argument("--goal", help="render the causal trace for one Goal")
    observe.add_argument("--health", action="store_true",
                         help="compact health counters only")
    observe.add_argument("--json", action="store_true")
    update = commands.add_parser("update", help="refresh the vendored home in place")
    update.add_argument("--dir", default=".")
    update.add_argument("--json", action="store_true")
    context = commands.add_parser("context")
    context.add_argument("--prompt", default=""); context.add_argument("--owner")
    context.add_argument("--workflow"); context.add_argument("--token-budget", type=int)
    context.add_argument("--json", action="store_true")
    memory = commands.add_parser("memory")
    memory_commands = memory.add_subparsers(dest="memory_command", required=True)
    for name in ("summary", "owner", "workflows", "strategy"):
        memory_commands.add_parser(name).add_argument("--json", action="store_true")
    memory_retire = memory_commands.add_parser("retire")
    memory_retire.add_argument("memory_id")
    memory_retire.add_argument("--json", action="store_true")
    memory_add = memory_commands.add_parser("add")
    memory_add.add_argument("--scope", required=True, choices=("workflow", "strategy"))
    memory_add.add_argument("--claim", required=True)
    memory_add.add_argument("--evidence", action="append", default=[],
                            help="evidence id; repeatable (required, must share lineage)")
    memory_add.add_argument("--goal"); memory_add.add_argument("--run")
    memory_add.add_argument("--intervention"); memory_add.add_argument("--workflow")
    memory_add.add_argument("--json", action="store_true")
    profile = commands.add_parser("profile")
    profile_commands = profile.add_subparsers(dest="profile_command", required=True)
    profile_commands.add_parser("list").add_argument("--json", action="store_true")
    profile_set = profile_commands.add_parser("set")
    profile_set.add_argument("--namespace", required=True); profile_set.add_argument("--key", required=True)
    profile_set.add_argument("--value", required=True); profile_set.add_argument("--json", action="store_true")
    goal = commands.add_parser("goal")
    goal_commands = goal.add_subparsers(dest="goal_command", required=True)
    create = goal_commands.add_parser("create")
    create.add_argument("--name", required=True); create.add_argument("--owner", required=True)
    create.add_argument("--metric", required=True); create.add_argument("--target", required=True)
    create.add_argument("--operator", default="ge", choices=("ge", "gt", "eq", "le", "lt"))
    create.add_argument("--aggregation", default="latest", choices=("count", "sum", "latest", "max", "min", "boolean_all", "boolean_any"))
    create.add_argument("--deadline"); create.add_argument("--parent"); create.add_argument("--priority", choices=("critical", "high", "normal", "low", "deferred"))
    create.add_argument("--config", default="{}"); create.add_argument("--id"); create.add_argument("--json", action="store_true")
    for name in ("list", "topology"):
        goal_commands.add_parser(name).add_argument("--json", action="store_true")
    show = goal_commands.add_parser("show"); show.add_argument("goal_id"); show.add_argument("--json", action="store_true")
    decide = goal_commands.add_parser("decide", help="answer a parked decision_request with one bounded step")
    decide.add_argument("goal_id")
    decide.add_argument("--kind", required=True, choices=("execute_workflow", "request_agent"))
    decide.add_argument("--workflow", help="candidate workflow id: <department_id>:<workflow_id>")
    decide.add_argument("--agent", help="bounded direct work executor (goal owner or installed Agent)")
    decide.add_argument("--instruction", help="bounded, concrete instruction for request_agent (required)")
    decide.add_argument("--evidence-kind", dest="evidence_kind", help="evidence kind that proves the work (defaults to the goal metric)")
    decide.add_argument("--json", action="store_true")
    resume = goal_commands.add_parser("resume", help="open the next run of a stalled or review-parked goal")
    resume.add_argument("goal_id"); resume.add_argument("--json", action="store_true")
    evidence = commands.add_parser("evidence")
    evidence_commands = evidence.add_subparsers(dest="evidence_command", required=True)
    add = evidence_commands.add_parser("add")
    add.add_argument("goal_id"); add.add_argument("--kind", required=True); add.add_argument("--source", required=True)
    add.add_argument("--payload", default="{}"); add.add_argument("--validity"); add.add_argument("--json", action="store_true")
    approve = commands.add_parser("approve"); approve.add_argument("goal_id"); approve.add_argument("--note", default=""); approve.add_argument("--key", action="append", default=[]); approve.add_argument("--scope", default="step", choices=("step", "run")); approve.add_argument("--json", action="store_true")
    notifications = commands.add_parser("notifications")
    notification_commands = notifications.add_subparsers(dest="notification_command", required=True)
    notification_list = notification_commands.add_parser("list")
    notification_list.add_argument("--status", default="pending"); notification_list.add_argument("--goal")
    notification_list.add_argument("--limit", type=int, default=100); notification_list.add_argument("--json", action="store_true")
    notification_ack = notification_commands.add_parser("ack"); notification_ack.add_argument("notification_id"); notification_ack.add_argument("--json", action="store_true")
    tasks = commands.add_parser("tasks")
    tasks.add_argument("work_order_id", nargs="?"); tasks.add_argument("--status", default="active", choices=("active", "open", "claimed"))
    tasks.add_argument("--goal"); tasks.add_argument("--limit", type=int, default=50); tasks.add_argument("--claim"); tasks.add_argument("--complete")
    tasks.add_argument("--evidence", default="[]")
    tasks.add_argument("--learning", help="workflow memory claim grounded in the completion evidence")
    tasks.add_argument("--json", action="store_true")
    runner = commands.add_parser("runner")
    runner_commands = runner.add_subparsers(dest="runner_command", required=True)
    tick = runner_commands.add_parser("tick"); tick.add_argument("--max-advances", type=int, default=100); tick.add_argument("--json", action="store_true")
    watch = runner_commands.add_parser("watch"); watch.add_argument("--interval", type=float, default=2.0); watch.add_argument("--max-ticks", type=int)
    for name in ("start", "stop", "status", "enable"):
        item = runner_commands.add_parser(name)
        if name == "start": item.add_argument("--interval", type=float, default=2.0)
        item.add_argument("--json", action="store_true")
    return parser


def _readonly(path: str) -> bool:
    return Path(path).exists()


def _render(value):
    return json.dumps(value, indent=2, ensure_ascii=False, default=str)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--version" in argv or "-V" in argv:
        print(f"spielos {VERSION}"); return 0
    if not argv:
        if not (PROJECT_ROOT / ".agents" / "company").is_dir():
            from .runtime.onboard import run_first_use
            return run_first_use(PROJECT_ROOT)
        runtime = CleanCommandRuntime(DEFAULT_DB, readonly=_readonly(str(DEFAULT_DB)))
        print(_render(runtime.company_snapshot())); return 0
    args = build_parser().parse_args(argv)
    try:
        if args.command == "init":
            from .runtime.onboard import run_init
            return run_init(dir=args.dir, force=args.force, assume_yes=args.yes, as_json=args.json)
        if args.command == "update":
            # Home lifecycle: refresh the vendored spine from the installed
            # distribution's templates. State (.spielos/), owner config,
            # and every user layer are always preserved.
            from .runtime.onboard import run_update
            return run_update(dir=args.dir, as_json=args.json)
        if args.command in {"catalog", "departments"}:
            from .runtime.registry import departments
            output = [{"id": item.id, "version": item.version, "workflows": [flow.id for flow in item.workflows]} for item in departments().values()]
        elif args.command == "layout":
            from .layout import audit
            output = audit(PROJECT_ROOT)
        elif args.command == "observe":
            readonly = _readonly(args.db)
            runtime = CleanCommandRuntime(args.db, readonly=readonly)
            output = runtime.observe(goal_id=args.goal, health=args.health)
        elif args.command == "runner" and args.runner_command in {"start", "stop", "status", "enable"}:
            service = RunnerService(PROJECT_ROOT, Path(args.db))
            output = service.start(args.interval) if args.runner_command == "start" else getattr(service, args.runner_command)()
        else:
            readonly = (args.command in {"status", "overview", "context",
                                         "observe", "catalog", "departments",
                                         "layout"}
                        or (args.command == "memory"
                            and args.memory_command not in {"add", "retire"})
                        or (args.command == "profile"
                            and args.profile_command == "list")
                        or (args.command == "notifications"
                            and args.notification_command == "list")) and _readonly(args.db)
            runtime = CleanCommandRuntime(args.db, readonly=readonly)
            if args.command == "status": output = runtime.status(args.goal_id) if args.goal_id else runtime.company_snapshot()
            elif args.command == "overview": output = runtime.company_snapshot()
            elif args.command == "context": output = runtime.assemble_context(prompt=args.prompt, owner_id=args.owner, workflow_id=args.workflow, token_budget=args.token_budget)
            elif args.command == "memory":
                if args.memory_command == "add":
                    output = runtime.add_memory(
                        args.scope, args.claim, evidence_ids=args.evidence,
                        goal_id=args.goal, run_id=args.run,
                        intervention_id=args.intervention,
                        workflow_id=args.workflow)
                elif args.memory_command == "retire":
                    output = runtime.retire_memory(args.memory_id)
                else:
                    summary = runtime.clean_memory_summary()
                    output = summary if args.memory_command == "summary" else summary["durable_memory"][args.memory_command.rstrip("s")]
            elif args.command == "profile": output = runtime.owner_memory() if args.profile_command == "list" else runtime.set_profile_claim(namespace=args.namespace, claim_key=args.key, value=_json(args.value))
            elif args.command == "goal":
                if args.goal_command == "create":
                    config = _json(args.config)
                    if not isinstance(config, dict): raise ValueError("--config must be a JSON object")
                    if args.priority: config["priority"] = args.priority
                    config["aggregation"] = args.aggregation
                    output = runtime.create_goal(name=args.name, owner_id=args.owner, metric=args.metric, operator=args.operator, target=_json(args.target), deadline=args.deadline, parent_id=args.parent, config=config, goal_id=args.id)
                elif args.goal_command == "list": output = runtime.goal_summaries()
                elif args.goal_command == "topology": output = runtime.topology_audit()
                elif args.goal_command == "decide":
                    output = runtime.decide_goal(
                        args.goal_id, args.kind, workflow=args.workflow,
                        agent=args.agent, instruction=args.instruction,
                        evidence_kind=args.evidence_kind)
                elif args.goal_command == "resume":
                    output = runtime.resume_goal(args.goal_id)
                else: output = runtime.status(args.goal_id)
            elif args.command == "evidence":
                payload = _json(args.payload)
                if not isinstance(payload, dict): raise ValueError("--payload must be a JSON object")
                output = runtime.add_evidence(args.goal_id, kind=args.kind, source=args.source, payload=payload, validity=args.validity)
            elif args.command == "approve": output = runtime.approve(args.goal_id, args.note, args.key, scope=args.scope)
            elif args.command == "notifications":
                if args.notification_command == "list":
                    output = runtime.notifications(
                        status=args.status, limit=args.limit, goal_id=args.goal)
                else:
                    output = runtime.acknowledge_notification(args.notification_id)
            elif args.command == "tasks":
                if args.claim: output = runtime.claim_work_order(args.work_order_id, args.claim)
                elif args.complete: output = runtime.complete_work_order(args.work_order_id, args.complete, _json(args.evidence), learning=args.learning)
                else: output = runtime.work_order(args.work_order_id) if args.work_order_id else runtime.work_orders(args.status, args.goal, args.limit)
            elif args.command == "runner":
                if args.runner_command == "tick": output = runtime.tick(args.max_advances)
                else:
                    for result in runtime.watch(args.interval, max_ticks=args.max_ticks): print(_render(result), flush=True)
                    return 0
            else: raise ValueError(f"unsupported command: {args.command}")
        print(_render(output)); return 0
    except (KeyError, RuntimeError, ValueError, json.JSONDecodeError) as error:
        print(f"spielos: {error}", file=sys.stderr); return 1


if __name__ == "__main__":
    raise SystemExit(main())
