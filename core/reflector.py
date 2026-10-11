"""Turning a finished run into something worth remembering.

Reflection runs once, after a run ends, and decides what to keep. That
decision is the whole difficulty. Keeping everything is the obvious
approach and the wrong one: the planner prompt has finite room, and
advice that was specific to one afternoon crowds out advice that is
generally true. A memory that grows without judgement makes the agent
worse, slowly, in a way that is hard to notice.

So the prompt asks for a narrow thing -- what would change a future plan
-- and the store refuses duplicates and allows superseding.

Three kinds of output:

    summary   one line describing the KIND of task, used for matching
    lessons   short imperative statements that would help next time
    facts     subject/predicate/value about named things

Failures are reflected on too. "Tried X, it did not work, because Y" is
at least as useful as a success, because it rules something out.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field

from core import config
from core.schemas import Plan
from llm.client import LLMClient, LLMError
from memory import store

_REFLECT_PROMPT = """\
A task has finished. Record only what is worth remembering for next time.

The request:
    {task}

What was planned and run:
{trace}

How it ended: {status}
{outcome}
{repairs}{edits}

Produce three things.

**summary** -- one line describing the KIND of task, not this specific
instance. It is used to recognise similar requests later, so write what
would match a future task: "Filing a vendor invoice from a PDF into the
ledger", not "Filed GLX/2026/118".

**lessons** -- short imperative statements that would change how a
similar task is planned. Each must be useful on a *different* task.

    GOOD: "add_invoice refuses non-ISO dates; convert before calling"
    GOOD: "Invoices live in sandbox/invoices/, not the sandbox root"
    GOOD: "A vendor named in a request may have no invoice present"
    BAD:  "The Globex invoice totalled 98400"   <- a fact, not a lesson
    BAD:  "The task succeeded"                   <- teaches nothing
    BAD:  "Be careful when filing invoices"      <- too vague to act on

Return an empty list if the run taught nothing new. That is a normal
outcome for a routine run, and padding the list makes the memory worse.

**facts** -- durable properties of named things: a vendor, a file, a
folder. subject / predicate / value. Only things likely to still be true
next month.

Two things outrank everything else as sources of lessons.

A **repair** means the agent was wrong about something and found out.
Whatever the error message said is a real constraint of this system, and
writing it down means the next plan gets it right first time. Prefer the
specific form -- "add_invoice refuses non-ISO dates; convert first" --
over the general one -- "be careful with dates".

A **human edit** is a person explicitly correcting the planner, which is
the most reliable signal available. Work out what they were fixing.

A run with neither usually taught nothing. Say so with an empty list
rather than restating what the planner already does; advice like "verify
the file exists first" is already in its instructions, and repeating it
back crowds out lessons that are actually new.
"""


class Lesson(BaseModel):
    text: str = Field(description="One short imperative sentence")
    kind: str = Field(
        description="A one-word category: constraint, location, caution, "
        "technique or preference"
    )


class Fact(BaseModel):
    subject: str = Field(description="The named thing, e.g. 'Globex Ltd'")
    predicate: str = Field(description="The property, e.g. 'currency'")
    value: str = Field(description="The value, e.g. 'INR'")


class Reflection(BaseModel):
    summary: str = Field(description="One line describing the kind of task")
    lessons: list[Lesson] = Field(default_factory=list)
    facts: list[Fact] = Field(default_factory=list)


def reflect(
    task: str,
    plan: Plan,
    results: list[dict[str, Any]],
    status: str,
    outcome: str,
    *,
    original_plan: Plan | None = None,
    repairs: list[dict[str, Any]] | None = None,
    client: LLMClient | None = None,
) -> Reflection | None:
    """Work out what this run taught. Returns None if reflection failed.

    `original_plan` is the plan as the model first wrote it, when the
    human edited it before approving. The difference between the two is
    the single highest-quality signal available -- a person deliberately
    correcting the planner -- and it is worth more than anything inferred
    from the run itself.
    """
    client = client or LLMClient()

    trace_lines = []
    by_id = {r["step_id"]: r for r in results}
    for step in plan.steps:
        result = by_id.get(step.id, {})
        mark = "ok" if result.get("verified") else "FAILED"
        detail = result.get("evidence") or result.get("error") or "did not run"
        trace_lines.append(
            f"  {step.id}. [{mark}] {step.tool}({step.arguments_json})\n"
            f"       {detail}"
        )

    try:
        reflection = client.generate_structured(
            _REFLECT_PROMPT.format(
                task=task,
                trace="\n".join(trace_lines) or "  (nothing ran)",
                status=status,
                outcome=outcome or "",
                edits=_describe_edits(original_plan, plan),
                repairs=_describe_repairs(repairs or []),
            ),
            Reflection,
            model=config.FAST_MODEL,
        )
    except LLMError:
        # Reflection failing must not fail the run. The work is already
        # done and verified; memory is an enhancement.
        return None

    return reflection


def save(reflection: Reflection, task: str, plan: Plan, status: str, outcome: str) -> int:
    """Write a reflection to the memory store. Returns the episode id."""
    from memory.semantic import get_embedder

    # The summary is embedded, not the raw task. Two requests worded
    # differently for the same kind of work should match, and the
    # summary is written to generalise where the task is specific.
    embedder = get_embedder()
    embedding = embedder.embed(reflection.summary)

    episode_id = store.record_episode(
        task=task,
        summary=reflection.summary,
        plan=plan.model_dump(mode="json"),
        status=status,
        outcome=outcome,
        embedding=embedding,
        embedder=embedder.name,
    )

    for lesson in reflection.lessons:
        store.add_lesson(lesson.text, lesson.kind, episode_id)
    for fact in reflection.facts:
        store.remember_fact(fact.subject, fact.predicate, fact.value)

    return episode_id


def _describe_repairs(repairs: list[dict[str, Any]]) -> str:
    """Describe what failed mid-run and what fixed it.

    Without this, a repair leaves only a counter behind -- the failed
    attempt is dropped from the results so they reflect what actually
    happened. But the error that caused it is the single most
    informative thing in the run, and it is the difference between
    learning "add_invoice refuses non-ISO dates" and learning nothing.
    """
    if not repairs:
        return ""

    lines = ["\nSteps that failed and had to be repaired:"]
    for repair in repairs:
        lines.append(
            f"  step {repair['step_id']} ({repair['intent']})\n"
            f"    tried   : {repair['tried']}\n"
            f"    failed  : {repair['failed_because']}\n"
            f"    fixed by: {repair['replaced_with']}"
        )
    return "\n".join(lines) + "\n"


def _describe_edits(original: Plan | None, final: Plan) -> str:
    """Describe what the human changed, if anything."""
    if original is None:
        return ""

    before = original.model_dump(mode="json")["steps"]
    after = final.model_dump(mode="json")["steps"]
    if before == after:
        return ""

    return (
        "\nA human edited the plan before approving it.\n"
        f"  they were shown : {json.dumps([_brief(s) for s in before])}\n"
        f"  they approved   : {json.dumps([_brief(s) for s in after])}\n"
        "Work out what they were correcting."
    )


def _brief(step: dict[str, Any]) -> str:
    return f"{step['tool']}({step['arguments_json']})"
