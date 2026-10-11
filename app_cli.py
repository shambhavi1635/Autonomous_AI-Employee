"""Terminal front end: give the agent a task, approve its plan, watch it run.

    venv\\python.exe app_cli.py "read notes.txt and save its third line to out/third.txt"
    venv\\python.exe app_cli.py --list
    venv\\python.exe app_cli.py --resume <run-id>

This file contains no agent logic. It renders what the graph reports and
collects answers for the gates -- nothing more. That boundary is deliberate:
core/ must stay runnable without a UI, so the same graph can be driven by
Streamlit in Phase 3 without changing a line of it.

The run id matters. Every run is a thread in the checkpoint database, so a
run paused at an approval gate can be resumed in a later session, after a
crash, or on another day.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import uuid
from typing import Any

from langgraph.types import Command
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

import tools.files  # noqa: F401  -- importing registers the tools
import tools.documents  # noqa: F401
import tools.ledger  # noqa: F401
import tools.office  # noqa: F401
import tools.report  # noqa: F401
from core.graph import build_graph, open_checkpointer
from core.schemas import Plan

console = Console()

RISK_COLOUR = {"read": "green", "write": "yellow", "irreversible": "bold red"}


def show_plan(plan: Plan) -> None:
    console.print()
    console.print(Panel(plan.reasoning, title="Approach", border_style="cyan"))

    table = Table(show_lines=True, header_style="bold")
    table.add_column("#", width=3)
    table.add_column("Risk", width=13)
    table.add_column("What it does")
    table.add_column("Call / check")

    for step in plan.steps:
        risk = step.risk.value
        gate = "\n[bold red]NEEDS APPROVAL[/]" if step.needs_approval else ""
        table.add_row(
            str(step.id),
            f"[{RISK_COLOUR[risk]}]{risk}[/]{gate}",
            step.intent,
            f"[dim]{step.tool}({json.dumps(step.arguments)})[/]\n"
            f"[italic]check: {step.postcondition}[/]",
        )

    console.print(table)


def ask(prompt: str, allowed: set[str]) -> str:
    """Read one of a fixed set of answers, re-asking until it is one."""
    while True:
        try:
            answer = console.input(f"[bold]{prompt}[/] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return "abort" if "abort" in allowed else "reject"
        if answer in allowed:
            return answer
        console.print(f"[dim]Please answer one of: {', '.join(sorted(allowed))}[/]")


def edit_plan(plan: Plan) -> Plan | None:
    """Open the plan as JSON in an editor and read back what the human saved.

    An editor rather than a series of prompts: it supports every edit --
    reordering, deleting, rewriting arguments -- without a bespoke UI for
    each. The result is validated exactly like a model-written plan,
    because a human edit can break a plan just as easily.
    """
    editor = os.environ.get("EDITOR") or ("notepad" if os.name == "nt" else "nano")

    with tempfile.NamedTemporaryFile(
        "w", suffix=".json", delete=False, encoding="utf-8"
    ) as handle:
        json.dump(plan.model_dump(mode="json"), handle, indent=2)
        path = handle.name

    console.print(f"[dim]Opening {editor}. Save and close when done.[/]")
    try:
        subprocess.run([editor, path], check=False)
        with open(path, encoding="utf-8") as handle:
            edited = Plan.model_validate(json.load(handle))
    except Exception as exc:
        console.print(f"[red]Could not use the edited plan:[/] {exc}")
        return None
    finally:
        os.unlink(path)

    return edited


def handle_plan_gate(payload: dict[str, Any]) -> dict[str, Any]:
    plan = Plan.model_validate(payload["plan"])
    console.rule(f"[bold]Task:[/] {payload['task']}")
    show_plan(plan)

    while True:
        answer = ask("approve / edit / reject ?", {"approve", "edit", "reject", "a", "e", "r"})
        answer = {"a": "approve", "e": "edit", "r": "reject"}.get(answer, answer)

        if answer == "approve":
            return {"action": "approve"}
        if answer == "reject":
            reason = console.input("[dim]Reason (optional): [/]").strip()
            return {"action": "reject", "reason": reason or "Rejected by the operator."}

        edited = edit_plan(plan)
        if edited is None:
            continue
        plan = edited
        show_plan(plan)
        if ask("use this edited plan? (yes / no)", {"yes", "no", "y", "n"}) in ("yes", "y"):
            return {"action": "edit", "plan": plan.model_dump(mode="json")}


def handle_action_gate(payload: dict[str, Any]) -> dict[str, Any]:
    """Gate 2. The arguments shown here are the real ones, already resolved."""
    body = "\n".join(
        f"[bold]{name}[/]: {value!r}" for name, value in payload["arguments"].items()
    )
    console.print()
    console.print(
        Panel(
            f"{payload['intent']}\n\n[dim]{payload['tool']}[/]\n{body}",
            title=f"[bold red]Step {payload['step_id']} is IRREVERSIBLE[/]",
            border_style="red",
        )
    )
    answer = ask("approve / skip / abort ?", {"approve", "skip", "abort", "a", "s"})
    answer = {"a": "approve", "s": "skip"}.get(answer, answer)
    return {"action": answer}


def show_results(state: dict[str, Any]) -> None:
    results = state.get("results") or []
    if results:
        console.print()
        for result in results:
            mark = "[green]PASS[/]" if result.get("verified") else "[red]FAIL[/]"
            console.print(f"  {mark} step {result['step_id']}")
            detail = result.get("evidence") or result.get("error") or ""
            if detail:
                console.print(f"       [dim]{detail}[/]")

    status = state.get("status", "unknown")
    colour = {"done": "green", "failed": "red", "rejected": "yellow"}.get(status, "white")
    console.print()
    console.print(f"[{colour}]{status.upper()}[/]  {state.get('message', '')}")


def drive(graph, config: dict[str, Any], first_input: Any) -> None:
    """Run the graph, answering each gate until it finishes or stops."""
    handlers = {"plan_approval": handle_plan_gate, "action_approval": handle_action_gate}

    state = graph.invoke(first_input, config)
    while "__interrupt__" in state:
        payload = state["__interrupt__"][0].value
        handler = handlers.get(payload.get("kind"))
        if handler is None:
            console.print(f"[red]Unknown gate: {payload.get('kind')}[/]")
            return
        state = graph.invoke(Command(resume=handler(payload)), config)

    show_results(state)


def list_runs(saver) -> None:
    seen: dict[str, str] = {}
    for checkpoint in saver.list(None, limit=200):
        thread = checkpoint.config["configurable"]["thread_id"]
        seen.setdefault(thread, (checkpoint.checkpoint.get("ts") or "")[:19])

    if not seen:
        console.print("[dim]No runs yet.[/]")
        return

    table = Table(title="Runs", header_style="bold")
    table.add_column("Run id")
    table.add_column("Last activity")
    for thread, timestamp in seen.items():
        table.add_row(thread, timestamp)
    console.print(table)
    console.print("[dim]Resume one with:  app_cli.py --resume <run-id>[/]")


def main() -> int:
    parser = argparse.ArgumentParser(description="Autonomous AI task worker")
    parser.add_argument("task", nargs="?", help="What you want done, in plain English")
    parser.add_argument("--resume", metavar="RUN_ID", help="Continue a paused run")
    parser.add_argument("--list", action="store_true", help="Show paused and past runs")
    args = parser.parse_args()

    saver, conn = open_checkpointer()
    try:
        if args.list:
            list_runs(saver)
            return 0

        if not args.task and not args.resume:
            parser.print_help()
            return 1

        graph = build_graph(saver)

        if args.resume:
            run_id = args.resume
            config = {"configurable": {"thread_id": run_id}}
            snapshot = graph.get_state(config)
            if not snapshot.values:
                console.print(f"[red]No run with id {run_id}.[/]")
                return 1
            console.print(f"[dim]Resuming {run_id} — {snapshot.values.get('task')}[/]")
            # Re-entering a paused thread replays the pending interrupt, so
            # the gate is presented again exactly as it was left.
            first_input: Any = None
        else:
            run_id = uuid.uuid4().hex[:8]
            config = {"configurable": {"thread_id": run_id}}
            first_input = {"task": args.task}

        console.print(f"[dim]run id: {run_id}[/]")

        try:
            drive(graph, config, first_input)
        except KeyboardInterrupt:
            console.print(
                f"\n[yellow]Stopped.[/] Nothing was left half-done — the run is "
                f"checkpointed.\nResume with:  app_cli.py --resume {run_id}"
            )
            return 130

        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
