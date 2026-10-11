

from __future__ import annotations

import json
import uuid
from typing import Any

import pandas as pd
import streamlit as st
from langgraph.types import Command

import tools.documents  # noqa: F401  -- importing registers the tools
import tools.files  # noqa: F401
import tools.ledger  # noqa: F401
import tools.office  # noqa: F401
import tools.report  # noqa: F401
from core import config
from core.graph import build_graph, open_checkpointer
from core.schemas import Plan, Risk

st.set_page_config(page_title="AI Task Worker",
                   page_icon=":material/smart_toy:", layout="wide")

RISK_BADGE = {"read": ":green[read]", "write": ":orange[write]",
              "irreversible": ":red[**irreversible**]"}

EXAMPLES = [
    "File the Acme invoice from the invoices folder into the ledger",
    "Read notes.txt and save its third line to out/third.txt",
    "List every invoice in the ledger and write a summary to out/summary.txt",
]


@st.cache_resource
def get_graph():
    """Compile the graph once and keep it across reruns.

    cache_resource is the one piece of Streamlit machinery this file
    needs: without it, every click would reopen the database and
    recompile the graph.
    """
    saver, conn = open_checkpointer()
    return build_graph(saver), saver, conn


def pending_gate(snapshot) -> dict[str, Any] | None:
    """The interrupt this run is waiting on, if any.

    Read from the checkpointed snapshot rather than from whatever the
    last invoke() returned. That is what lets a refreshed browser, or a
    different browser tomorrow, pick the run up exactly where it paused.
    """
    for task in snapshot.tasks or ():
        for interrupt in task.interrupts or ():
            return interrupt.value
    return None


def plan_to_frame(plan: Plan) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "step": s.id,
                "risk": s.risk.value,
                "what it does": s.intent,
                "tool": s.tool,
                "arguments": s.arguments_json,
                "how we check it": s.postcondition,
            }
            for s in plan.steps
        ]
    )


def frame_to_plan(frame: pd.DataFrame, original: Plan) -> Plan:
    """Rebuild a Plan from the edited table.

    Validated exactly like a model-written plan. A human edit can break a
    plan just as easily -- a deleted step that a later one referred to, a
    tool name typed wrong -- and it should fail here, visibly, rather
    than halfway through execution.
    """
    steps = []
    for position, row in enumerate(frame.to_dict("records"), start=1):
        if not str(row.get("tool", "")).strip():
            continue  # a blank row the editor added
        steps.append(
            {
                "id": position,
                "intent": row["what it does"],
                "tool": row["tool"],
                "arguments_json": row["arguments"],
                "postcondition": row["how we check it"],
                "risk": row["risk"],
            }
        )
    return Plan(task=original.task, reasoning=original.reasoning, steps=steps)


def render_plan_gate(graph, cfg, gate: dict[str, Any]) -> None:
    """Gate 1: approve, edit or reject the whole plan."""
    plan = Plan.model_validate(gate["plan"])

    st.subheader("Proposed plan")
    st.info(plan.reasoning, icon=":material/lightbulb:")

    if plan.highest_risk is Risk.IRREVERSIBLE:
        st.warning(
            "This plan contains an irreversible step. You will be asked "
            "again, with the exact details, immediately before it runs.",
            icon=":material/warning:",
        )

    edited = st.data_editor(
        plan_to_frame(plan),
        use_container_width=True,
        num_rows="dynamic",
        disabled=["step"],
        column_config={
            "risk": st.column_config.SelectboxColumn(
                options=[r.value for r in Risk], width="small"
            ),
            "arguments": st.column_config.TextColumn(width="medium"),
        },
        key="plan_editor",
    )
    st.caption(
        "Edit any cell, drag to reorder, or delete rows. "
        "Changes take effect when you approve."
    )

    left, middle, right = st.columns([1, 1, 4])

    if left.button("Approve", type="primary", use_container_width=True):
        try:
            revised = frame_to_plan(edited, plan)
        except Exception as exc:
            st.error(f"That edit does not make a valid plan: {exc}")
            return

        unchanged = revised.model_dump(
            mode="json")["steps"] == gate["plan"]["steps"]
        decision = (
            {"action": "approve"}
            if unchanged
            else {"action": "edit", "plan": revised.model_dump(mode="json")}
        )
        advance(graph, cfg, Command(resume=decision))

    if middle.button("Reject", use_container_width=True):
        advance(graph, cfg, Command(resume={"action": "reject",
                                            "reason": "Rejected in the UI."}))


def render_action_gate(graph, cfg, gate: dict[str, Any]) -> None:
    """Gate 2: approve the exact payload of an irreversible step."""
    st.subheader(f"Step {gate['step_id']} is irreversible")
    st.error(gate["intent"], icon=":material/error:")

    st.caption(f"About to call `{gate['tool']}` with:")
    st.json(gate["arguments"])
    st.caption(
        "These are the real values, resolved from earlier steps -- not "
        "placeholders. This is what will actually happen."
    )

    left, middle, right, _ = st.columns([1, 1, 1, 3])
    if left.button("Run it", type="primary", use_container_width=True):
        advance(graph, cfg, Command(resume={"action": "approve"}))
    if middle.button("Skip", use_container_width=True):
        advance(graph, cfg, Command(resume={"action": "skip"}))
    if right.button("Abort", use_container_width=True):
        advance(graph, cfg, Command(resume={"action": "abort"}))


def advance(graph, cfg, payload) -> None:
    """Run the graph until it next pauses, narrating as it goes.

    stream() yields after each node, so the page fills in while the agent
    works instead of freezing until it finishes. That is the whole point
    of the live view: the user sees which step is running, what it
    observed, and whether the check passed -- rather than a spinner and
    a result.
    """
    narration = st.container()
    with narration, st.status("Working...", expanded=True) as status:
        try:
            for chunk in graph.stream(payload, cfg, stream_mode="updates"):
                for node, update in (chunk or {}).items():
                    describe(node, update)
        except Exception as exc:
            status.update(label="Something went wrong", state="error")
            st.exception(exc)
            return
        status.update(label="Paused", state="complete")
    st.rerun()


def describe(node: str, update: Any) -> None:
    """One line per node, as the graph passes through it."""
    if not isinstance(update, dict):
        return

    if node == "plan":
        st.write("Planned the work.")
    elif node == "step":
        for result in (update.get("results") or [])[-1:]:
            mark = "PASS" if result.get("verified") else "FAIL"
            st.write(f"**{mark}** step {result['step_id']}")
            detail = result.get("evidence") or result.get("error")
            if detail:
                st.caption(detail)
    elif node == "repair":
        st.write(f"Repairing: {update.get('message', '')}")
    elif node == "replan":
        st.write("That approach did not work. Planning again.")
    elif node == "finalise":
        st.write(update.get("message", ""))


def render_answer(values: dict[str, Any]) -> None:
    """Show the agent's answer, when the task was a question.

    A `report` step's output is the answer itself, so it belongs at the
    top in plain text -- not collapsed in an expander alongside the raw
    file contents it was extracted from.
    """
    plan = values.get("plan")
    outputs = values.get("outputs") or {}
    if not plan:
        return

    for step in plan.get("steps", []):
        if step.get("tool") == "report":
            answer = outputs.get(str(step["id"]))
            if answer:
                st.subheader("Answer")
                st.markdown(f"> {answer}")
            return


def render_outcome(values: dict[str, Any]) -> None:
    status = values.get("status", "unknown")
    message = values.get("message", "")

    if status == "done":
        st.success(message, icon=":material/check_circle:")
    elif status == "rejected":
        st.warning(message, icon=":material/block:")
    elif status == "failed":
        st.error(message, icon=":material/error:")

    render_answer(values)

    results = values.get("results") or []
    if results:
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "step": r["step_id"],
                        "ran": "yes" if r.get("ok") else "no",
                        "verified": {True: "yes", False: "no"}.get(r.get("verified"), "-"),
                        "evidence": r.get("evidence") or r.get("error") or "",
                    }
                    for r in results
                ]
            ),
            use_container_width=True,
            hide_index=True,
        )

    render_outputs(values)

    if values.get("replans"):
        st.caption(f"The plan was reconsidered {values['replans']} time(s).")


def render_outputs(values: dict[str, Any]) -> None:
    """Show what each step actually returned.

    Not the same as the evidence column. Evidence is what a read-only
    probe observed when checking a step; this is the step's own result.

    For a task phrased as a question -- "list every invoice", "what was
    the total?" -- the output IS the answer, and without this the UI
    reports success while showing the user nothing they asked for. There
    is no "reply to the user" tool, so the last step's output is the
    closest thing the agent has to an answer.
    """
    outputs = values.get("outputs") or {}
    if not outputs:
        return

    st.subheader("What the steps returned")
    ordered = sorted(outputs.items(), key=lambda kv: int(kv[0]))

    for step_id, output in ordered:
        last = step_id == ordered[-1][0]
        with st.expander(f"Step {step_id}", expanded=last):
            if isinstance(output, (list, dict)):
                # A list of records renders far better as a table than as
                # JSON, and "list every invoice" returns exactly that.
                if isinstance(output, list) and output and isinstance(output[0], dict):
                    st.dataframe(pd.DataFrame(output), use_container_width=True,
                                 hide_index=True)
                else:
                    st.json(output)
            else:
                text = str(output)
                st.code(text if len(text) <= 4000 else text[:4000] + "\n...")


def render_ledger() -> None:
    """Show the ledger's real contents.

    The agent's claim that it filed something is not evidence; this reads
    the database. Having it on screen means a demo can answer "where did
    it actually go?" without dropping to a terminal -- and it makes a
    wrongly filed record just as visible as a correct one.
    """
    rows = tools.ledger.list_invoices()
    with st.expander(f"Ledger ({len(rows)} invoice{'' if len(rows) == 1 else 's'})",
                     expanded=bool(rows)):
        if not rows:
            st.caption("Empty.")
            return
        st.dataframe(
            pd.DataFrame(rows)[
                ["vendor", "invoice_number", "invoice_date",
                    "total_amount", "currency"]
            ],
            use_container_width=True,
            hide_index=True,
        )
        st.caption(f"`{config.LEDGER_DB.relative_to(config.PROJECT_ROOT)}`")


def render_memory() -> None:
    """Show what the agent has learned, and let a human delete it.

    This panel is not a convenience. Everywhere else, this project
    refuses to take the model's word for anything -- a step is not done
    because it says so, the task is not fulfilled because the steps
    passed. Memory should get the same treatment, and it is the one
    place where a wrong belief is invisible: a bad lesson does not throw
    an error, it quietly steers every future plan.

    It has already happened here. A lesson was learned from a run that a
    then-buggy goal verifier had wrongly failed, so the agent "learned"
    something untrue about its own correct behaviour. The bug was fixed;
    the lesson would have outlived it. Hence the delete buttons, and
    hence showing which run each lesson came from.
    """
    from memory import store

    counts = store.stats()
    with st.expander(
        f"Memory ({counts['lessons']} lesson{'' if counts['lessons'] == 1 else 's'}, "
        f"{counts['episodes']} run{'' if counts['episodes'] == 1 else 's'})",
        expanded=False,
    ):
        lessons = store.active_lessons_with_source()
        if not lessons and not counts["facts"]:
            st.caption("Nothing learned yet. Memory is written after each run.")
            return

        if lessons:
            st.caption("**Lessons** — shown to the planner before every task")
            for row in lessons:
                source = row["source_status"]
                flag = " :red[from a failed run]" if source == "failed" else ""
                left, right = st.columns([8, 1])
                left.markdown(f"- {row['text']}{flag}")
                left.caption(f"used {row['uses']}x")
                if right.button("✕", key=f"del-lesson-{row['id']}",
                                help="Forget this lesson"):
                    store.delete_lesson(row["id"])
                    st.rerun()

        facts = store.all_facts()
        if facts:
            st.caption("**Facts**")
            for row in facts:
                left, right = st.columns([8, 1])
                left.markdown(
                    f"- **{row['subject']}** · {row['predicate']} = {row['value']}"
                )
                if right.button("✕", key=f"del-fact-{row['id']}",
                                help="Forget this fact"):
                    store.delete_fact(row["id"])
                    st.rerun()

        st.caption(f"`{store.MEMORY_DB.relative_to(config.PROJECT_ROOT)}`")


def sidebar(graph, saver) -> None:
    with st.sidebar:
        st.header("Runs")

        if st.button("New run", use_container_width=True):
            st.session_state.pop("thread_id", None)
            st.rerun()

        seen: dict[str, str] = {}
        for checkpoint in saver.list(None, limit=200):
            thread = checkpoint.config["configurable"]["thread_id"]
            seen.setdefault(
                thread, (checkpoint.checkpoint.get("ts") or "")[:16])

        for thread, when in list(seen.items())[:15]:
            label = f"{thread}  ·  {when}"
            if st.button(label, key=f"open-{thread}", use_container_width=True):
                st.session_state["thread_id"] = thread
                st.rerun()

        st.divider()
        render_memory()
        render_ledger()

        st.divider()
        st.caption(
            f"planner: `{config.PLANNER_MODEL}`\n\n"
            f"fast: `{config.FAST_MODEL}`\n\n"
            f"sandbox: `{config.SANDBOX_DIR.name}/`"
        )


def main() -> None:
    graph, saver, _conn = get_graph()
    sidebar(graph, saver)

    st.title("Autonomous AI Task Worker")

    thread_id = st.session_state.get("thread_id")

    if not thread_id:
        st.caption(
            "Describe a task. The agent will propose a plan and wait for "
            "your approval before doing anything."
        )
        task = st.text_area("Task", placeholder=EXAMPLES[0], height=90)
        st.caption("Examples: " + "  ·  ".join(f"*{e}*" for e in EXAMPLES))

        # Not disabled=not task.strip(). Streamlit only re-evaluates that
        # on the rerun your typing triggers, so the first click after
        # typing lands on a still-disabled button and does nothing.
        # Checking the value on click instead has no such lag.
        if st.button("Plan it", type="primary"):
            if not task.strip():
                st.warning("Describe a task first.")
                return
            new_id = uuid.uuid4().hex[:8]
            st.session_state["thread_id"] = new_id
            cfg = {"configurable": {"thread_id": new_id}}
            advance(graph, cfg, {"task": task.strip()})
        return

    cfg = {"configurable": {"thread_id": thread_id}}
    snapshot = graph.get_state(cfg)

    if not snapshot.values:
        st.warning("That run no longer exists.")
        return

    st.caption(f"Run `{thread_id}`")
    st.write(f"**Task:** {snapshot.values.get('task', '')}")

    gate = pending_gate(snapshot)
    if gate is None and snapshot.next:
        # No gate waiting, but the graph has not finished either: the run
        # was abandoned mid-execution, which happens whenever the browser
        # navigates away while a step is in flight. Streamlit runs the
        # graph inside the page request, so closing the page ends the
        # request -- but not the run, because its state is checkpointed.
        # Nothing is lost; it just needs picking up again.
        st.info(
            f"This run stopped part-way through (next: {snapshot.next[0]}). "
            "Nothing was lost -- its state is saved.",
            icon=":material/pause_circle:",
        )
        render_outcome(snapshot.values)
        if st.button("Continue this run", type="primary"):
            advance(graph, cfg, None)
    elif gate is None:
        render_outcome(snapshot.values)
    elif gate.get("kind") == "plan_approval":
        render_plan_gate(graph, cfg, gate)
    elif gate.get("kind") == "action_approval":
        render_action_gate(graph, cfg, gate)
    else:
        st.error(f"Unknown gate: {gate.get('kind')}")


main()
