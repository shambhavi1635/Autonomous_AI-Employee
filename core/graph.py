"""The agent as a state machine: plan, ask, execute, verify, repeat.

Everything until now has been pieces. This is the machine that runs them,
and the place the human-in-the-loop requirement is actually met.

    plan ---> approve? ---[reject]---> stop
                 |
             [approve]
                 v
         +-> execute step --+
         |   (irreversible? |
         |    ask first)    |
         |       |          |
         |    verify        |
         |       |          |
         +--[more steps]----+
                 |
              [done]
                 v
               stop

Why LangGraph rather than a while loop:

`interrupt()` writes the entire graph state to SQLite and stops the
process. Later -- a minute, or after a reboot -- `Command(resume=...)`
continues from exactly that point, and the value passed becomes the return
value of the `interrupt()` call. That makes an approval gate *durable*. A
while loop blocking on an in-memory event loses the pending approval the
moment the process dies, and would need a persistence layer written by
hand to do what the checkpointer does for free.

State is stored as plain dicts rather than Pydantic objects. The
checkpointer serialises state on every pause, and primitives survive that
round trip without surprises; the models are rebuilt at the edges where
they are used.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Literal, TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from core import config
from core.executor import (
    ExecutionError,
    repair_step,
    resolve_arguments,
    run_tool,
    verify,
)
from core.planner import PlanningError, make_plan, render
from core.schemas import Plan, StepResult
from llm.client import LLMClient
from tools import registry


class AgentState(TypedDict, total=False):
    """Everything one run needs to survive being interrupted and resumed."""

    task: str
    plan: dict[str, Any] | None
    cursor: int                      # index into plan["steps"] of the next step
    outputs: dict[str, Any]          # step id (as str) -> that step's output
    results: list[dict[str, Any]]    # StepResult dicts, in order
    attempts: dict[str, int]         # step id (as str) -> tries so far
    status: Literal[
        "planning", "awaiting_approval", "running", "done", "failed", "rejected"
    ]
    message: str                     # why it stopped, for the human


def plan_node(state: AgentState) -> AgentState:
    """Produce a plan for the task."""
    try:
        plan = make_plan(state["task"])
    except PlanningError as exc:
        return {"status": "failed", "message": str(exc)}

    return {
        "plan": plan.model_dump(mode="json"),
        "cursor": 0,
        "outputs": {},
        "results": [],
        "attempts": {},
        "status": "awaiting_approval",
        "message": "",
    }


def approval_node(state: AgentState) -> AgentState:
    """Gate 1: the human approves, edits or rejects the whole plan.

    `interrupt()` does not return here. It raises, the graph state is
    checkpointed, and this function is re-entered from the top when the run
    is resumed -- at which point the same `interrupt()` call returns the
    value the human supplied. So anything before it runs twice, which is
    why nothing before it has side effects.
    """
    if state.get("status") == "failed":
        return {}

    plan = Plan.model_validate(state["plan"])

    decision = interrupt(
        {
            "kind": "plan_approval",
            "task": state["task"],
            "plan": state["plan"],
            "rendered": render(plan),
            "prompt": "Approve this plan? (approve / reject / edit)",
        }
    )

    action = (decision or {}).get("action", "reject")

    if action == "approve":
        return {"status": "running", "message": ""}

    if action == "edit":
        # The human hands back a modified plan. Validate it the same way a
        # model-written one would be: an edit can break a plan too.
        edited = Plan.model_validate(decision["plan"])
        return {
            "plan": edited.model_dump(mode="json"),
            "status": "running",
            "message": "Plan edited by the operator before execution.",
        }

    return {
        "status": "rejected",
        "message": (decision or {}).get("reason", "Rejected by the operator."),
    }


def step_node(state: AgentState) -> AgentState:
    """Run the step at the cursor: approve if needed, execute, verify."""
    plan = Plan.model_validate(state["plan"])
    cursor = state["cursor"]
    step = plan.steps[cursor]

    outputs = {int(k): v for k, v in state.get("outputs", {}).items()}

    # Arguments are resolved BEFORE the gate, not after. Gate 1 approved an
    # intention; gate 2 must approve the specific thing about to happen --
    # and "send an email with body {{step_1.output}}" is not something a
    # human can meaningfully approve. They need to see the actual text.
    #
    # Resolution runs again when the graph is resumed, because a node is
    # re-entered from the top. That costs one cheap model call and is safe:
    # resolving reads earlier outputs and changes nothing.
    try:
        arguments = resolve_arguments(step, outputs, client=LLMClient())
    except ExecutionError as exc:
        return {"status": "failed", "message": str(exc)}

    # The risk tier is taken from the tool registry, never from the plan.
    # A plan is untrusted input -- it can be wrong, and it can be influenced
    # by text the agent read along the way -- so it must not be able to
    # lower its own risk label and walk past this gate.
    enforced = registry.effective_risk(step.tool, step.risk)
    if enforced.value == "irreversible":
        decision = interrupt(
            {
                "kind": "action_approval",
                "step_id": step.id,
                "intent": step.intent,
                "tool": step.tool,
                "arguments": arguments,
                "prompt": f"Step {step.id} is irreversible. Run it? (approve / skip / abort)",
            }
        )
        action = (decision or {}).get("action", "abort")
        if action == "abort":
            return {"status": "rejected", "message": f"Aborted at step {step.id}."}
        if action == "skip":
            return {
                "cursor": cursor + 1,
                "results": state["results"]
                + [{"step_id": step.id, "ok": False, "error": "skipped by operator"}],
                "message": f"Step {step.id} skipped.",
            }

    client = LLMClient()
    result = run_tool(step, arguments)
    result = verify(step, result, outputs, arguments, client=client)

    new_outputs = dict(state.get("outputs", {}))
    new_outputs[str(step.id)] = result.output

    return {
        "cursor": cursor + 1,
        "outputs": new_outputs,
        "results": state["results"] + [result.model_dump(mode="json")],
    }


def repair_node(state: AgentState) -> AgentState:
    """Ask the model to fix the step that just failed, then try again.

    The repaired step replaces the failed one in the plan, so what the
    human sees afterwards is what actually ran -- not the version that
    was approved. `message` records the substitution, because a plan that
    silently rewrote itself would undermine the point of approving one.
    """
    plan = Plan.model_validate(state["plan"])
    cursor = state["cursor"] - 1          # the step that just failed
    step = plan.steps[cursor]

    failed = StepResult.model_validate(state["results"][-1])
    outputs = {int(k): v for k, v in state.get("outputs", {}).items()}

    repaired = repair_step(step, failed, outputs, client=LLMClient())
    if repaired is None:
        return {
            "status": "failed",
            "message": (
                f"Step {step.id} failed and could not be repaired: "
                f"{failed.error or failed.evidence}"
            ),
        }

    plan.steps[cursor] = repaired
    attempts = dict(state.get("attempts", {}))
    attempts[str(step.id)] = attempts.get(str(step.id), 1) + 1

    return {
        "plan": plan.model_dump(mode="json"),
        "cursor": cursor,                 # rewind to re-run this step
        "results": state["results"][:-1],  # drop the failed attempt
        "attempts": attempts,
        "message": (
            f"Step {step.id} failed ({failed.error or failed.evidence}); "
            f"retrying as {repaired.tool}."
        ),
    }


def after_approval(state: AgentState) -> str:
    """Leave the approval gate: either start executing, or stop."""
    return "step" if state.get("status") == "running" else END


def after_step(state: AgentState) -> str:
    """Decide what happens after a step: continue, or stop and say why.

    Phase 1 stops on the first unverified step. The retry-and-repair ladder
    from the design goes here, and this is the function it goes in -- which
    is the argument for a graph over a while loop: the routing decisions
    are one readable function instead of nested breaks.
    """
    if state.get("status") in ("failed", "rejected"):
        return END

    plan = Plan.model_validate(state["plan"])
    results = state.get("results", [])

    # The failure ladder. A step that did not succeed gets one repair
    # attempt before the run stops; a step that has already been repaired
    # and failed again is escalated to the human rather than retried in a
    # loop, because the second failure is evidence the approach is wrong,
    # not that the attempt was unlucky.
    if results:
        last = results[-1]
        skipped = "skipped" in (last.get("error") or "")
        succeeded = last.get("ok") and last.get("verified")
        if not succeeded and not skipped:
            attempts = state.get("attempts", {}).get(str(last["step_id"]), 1)
            return "repair" if attempts < config.MAX_STEP_ATTEMPTS else END

    if state["cursor"] >= len(plan.steps):
        return END
    if state["cursor"] >= config.MAX_STEPS:
        return END

    return "step"


def finalise(state: AgentState) -> AgentState:
    """Summarise the run for the human. Runs once, at the end."""
    if state.get("status") in ("failed", "rejected"):
        return {}

    results = state.get("results", [])
    failed = [r for r in results if not (r.get("ok") and r.get("verified"))]

    if failed:
        first = failed[0]
        return {
            "status": "failed",
            "message": (
                f"Stopped at step {first['step_id']}: "
                f"{first.get('evidence') or first.get('error')}"
            ),
        }

    return {
        "status": "done",
        "message": f"All {len(results)} steps completed and verified.",
    }


def build_graph(checkpointer: SqliteSaver):
    """Wire the nodes together and compile."""
    builder = StateGraph(AgentState)

    builder.add_node("plan", plan_node)
    builder.add_node("approve", approval_node)
    builder.add_node("step", step_node)
    builder.add_node("repair", repair_node)
    builder.add_node("finalise", finalise)

    builder.add_edge(START, "plan")
    builder.add_edge("plan", "approve")
    builder.add_conditional_edges(
        "approve", after_approval, {"step": "step", END: "finalise"}
    )
    builder.add_conditional_edges(
        "step", after_step, {"step": "step", "repair": "repair", END: "finalise"}
    )
    # Repair rewinds the cursor and sends the step back round.
    builder.add_conditional_edges(
        "repair", lambda s: END if s.get("status") == "failed" else "step",
        {"step": "step", END: "finalise"},
    )
    builder.add_edge("finalise", END)

    return builder.compile(checkpointer=checkpointer)


def open_checkpointer() -> tuple[SqliteSaver, sqlite3.Connection]:
    """Open the checkpoint database.

    check_same_thread=False because a UI will later drive the graph from a
    different thread than the one that opened it. The caller closes the
    connection; it is returned rather than hidden so a CLI can keep it open
    for the life of the process.
    """
    config.ensure_dirs()
    conn = sqlite3.connect(config.CHECKPOINT_DB, check_same_thread=False)
    return SqliteSaver(conn), conn
