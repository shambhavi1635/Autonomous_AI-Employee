"""Running one step: resolve its arguments, call the tool, check the result.

The graph in core/graph.py decides *which* step runs and what happens next.
This file does the work of a single step, and it is kept separate so each
piece can be tested on its own without a graph, a checkpointer or a UI.

Three jobs, in order:

  resolve_arguments  turn {{step_N.output}} placeholders into real values
  run_tool           validate and invoke
  verify             check the postcondition by re-inspecting the world

The third is the one that makes this an agent rather than a script runner.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field

from core import config
from core.schemas import Risk, Step, StepResult
from llm.client import LLMClient, LLMError
from tools import registry

# These two prompts live here rather than in llm/prompts/ on purpose. They
# are short, structural, and meaningless apart from the Pydantic schemas
# defined beside them -- unlike the planner prompt, which is prose we will
# iterate on and want to diff on its own.

_RESOLVE_PROMPT = """\
A step in an approved plan is about to run, but its arguments contain
placeholders that stand for earlier steps' results. Produce the real
arguments.

Tool:      {signature}
Intent:    {intent}
Arguments as planned: {planned}

Results of earlier steps:
{outputs}
{lessons}
Return the concrete arguments as a JSON object, following the step's
intent exactly. The intent tells you which of two jobs this is.

**Extracting a value.** "the third line", "the invoice total", "the file
path". Pull out exactly that part and copy it verbatim -- do not
summarise, reformat or improve it, and do not pass the whole result
through when one field was asked for. Identifiers, numbers, dates,
names and paths must match the source character for character, except
where a lesson above says a particular format is required.

**Composing text.** "a one-paragraph summary", "an email explaining
X", "a report of the findings". Here you must write the text yourself,
using the earlier output as source material. Pasting that output
through unchanged is a failure, not a safe default: a step asked to
summarise a spreadsheet and handed back the raw rows has not done its
job. Write it as a person would -- prose, appropriate length, no
column dumps or markup.

When unsure which job it is, look at the verb in the intent: read,
find, get and extract mean copy; write, summarise, draft, compose,
explain and report mean compose.
"""

_PROBE_PROMPT = """\
A step has just run. Decide how to check whether it actually worked, by
choosing ONE read-only tool call that would reveal the truth.

Step intent:   {intent}
Postcondition: {postcondition}

The call that just ran, with its real arguments:
    {call}

Read-only tools available:
{catalog}

Choose the call that most directly observes the postcondition. Prefer
looking at the thing that was supposed to change.

Use the exact paths, names and identifiers shown in the call above. Never
invent a filename or guess at one that looks plausible -- a probe that
reads the wrong thing produces a verdict about the wrong thing.
"""

_JUDGE_PROMPT = """\
Decide whether a step's postcondition holds.

Postcondition: {postcondition}
Step intent:   {intent}

Results of earlier steps, for comparison:
{context}

What the step returned:
{output}

What we observed afterwards by running {probe}:
{evidence}

Did the postcondition hold? Judge only on what is shown above.

If the evidence does not let you tell either way, answer false and say in
`evidence` exactly what you would have needed to see. Do not guess, and do
not assert a fact you cannot read directly from the material above -- a
confident wrong verdict is worse than an honest "cannot tell", because it
stops a correct step or passes a broken one.
"""


_REPAIR_PROMPT = """\
A step in an approved plan failed. Propose a corrected version of that one
step. Everything before it succeeded and will not be run again.

What the step was meant to achieve: {intent}

What was tried:
    {call}
Its postcondition: {postcondition}

What went wrong:
    {failure}

Tools available:
{catalog}

Results of earlier steps:
{outputs}

Rules:
- Keep the same intent. You are fixing how, not what.
- You may change the tool, the arguments, or the postcondition.
- Read the error literally. If it names the tool you should have used, or
  the argument that was wrong, that is the fix -- do not look past it for
  a cleverer explanation.
- If the postcondition was unobservable rather than unmet, restate it as
  something a read-only tool can actually check.
- Placeholders like {{step_N.output}} still work and still refer to
  earlier steps.
"""


_GOAL_PROMPT = """\
Every step of a plan reported success. Decide whether the user actually
got what they asked for.

What the user asked for:
    {task}

What was done -- the tool each step actually ran, and what was observed
afterwards when checking it:
{trace}

Note that the "observed afterwards" line describes a read-only check, not
the action. A step that filed a record will be confirmed by a lookup; do
not read that lookup as meaning nothing was filed.

Steps can all succeed while the wrong thing is done. Answer false if:

- the work acted on a different file, record, vendor or entity than the
  one the request names;
- the request named something that does not exist, and instead of saying
  so, something similar was used in its place;
- part of the request was quietly dropped.

Being unable to find what was asked for is a legitimate outcome and
should be reported as one. Substituting the nearest available thing is
not, however well the individual steps went.
"""


class GoalVerdict(BaseModel):
    fulfilled: bool = Field(description="Did the user get what they asked for?")
    explanation: str = Field(
        description="One or two sentences. If not fulfilled, say what was "
        "asked for and what was actually done instead."
    )


def verify_goal(
    task: str,
    results: list[dict[str, Any]],
    steps: list[Step] | None = None,
    *,
    client: LLMClient,
) -> GoalVerdict:
    """Check the whole run against the original request.

    The third level of verification, and the one that is easy to skip.

      1. did the tool call return?           -- `ok`
      2. did each postcondition hold?        -- `verified`
      3. did the user get what they asked for?  -- here

    Levels 1 and 2 are per step, and a plan can pass both while missing
    the point entirely. Asked for the Wayne Enterprises invoice, an agent
    listed the folder, read the only invoice it found, filed ACME, and
    every step verified -- because each postcondition asked whether *an*
    invoice had been filed, not whether *that* one had.

    No step can catch this, because no step is wrong. Only something
    looking at the request and the whole trace together can.
    """
    # The trace must say what each step DID, not only what was observed
    # afterwards. Evidence lines describe the verification probe -- and a
    # probe is deliberately read-only, so a trace of evidence alone reads
    # as a sequence of lookups. That exact gap produced a false failure:
    # an invoice was filed correctly, step 3's evidence said "find_invoice
    # returned the record" (the probe), and the judge concluded the agent
    # had only looked it up and never filed anything.
    by_id = {s.id: s for s in (steps or [])}
    lines = []
    for result in results:
        step = by_id.get(result["step_id"])
        # Resolved arguments where we have them. Showing the plan's
        # arguments_json instead puts "{{step_2.output}}" in front of the
        # judge, which reads as raw text having been dumped into every
        # field -- and it failed a correct run for exactly that reason.
        called_with = result.get("arguments")
        if step and called_with is not None:
            action = f"ran {step.tool}({json.dumps(called_with)}) -- {step.intent}"
        elif step:
            action = f"ran {step.tool}({step.arguments_json}) -- {step.intent}"
        else:
            action = "ran (step details unavailable)"
        observed = result.get("evidence") or result.get("error") or "no evidence"
        lines.append(
            f"  step {result['step_id']}: {action}\n"
            f"      observed afterwards: {observed}"
        )
    trace = "\n".join(lines) or "  (nothing ran)"

    try:
        return client.generate_structured(
            _GOAL_PROMPT.format(task=task, trace=trace),
            GoalVerdict,
            model=config.FAST_MODEL,
        )
    except LLMError as exc:
        # Unable to check is not the same as checked and fine. Say so
        # rather than reporting a success nobody confirmed.
        return GoalVerdict(
            fulfilled=False,
            explanation=f"Could not check the result against the request: {exc}",
        )


class RepairedStep(BaseModel):
    tool: str = Field(description="Tool to use this time")
    arguments_json: str = Field(description="Its arguments, as a JSON object string")
    postcondition: str = Field(description="How to check it worked this time")
    why: str = Field(description="One sentence: what was wrong and what changed")


class ResolvedArguments(BaseModel):
    arguments_json: str = Field(
        description='The concrete arguments as a JSON object string'
    )


class Probe(BaseModel):
    tool: str = Field(description="A read-only tool that would check this")
    arguments_json: str = Field(default="{}", description="Its arguments, as JSON")


class Verdict(BaseModel):
    passed: bool = Field(description="Did the postcondition hold?")
    evidence: str = Field(
        description="One sentence: what was observed, and why that settles it"
    )


class ExecutionError(RuntimeError):
    """A step could not be run at all."""


def has_placeholders(step: Step) -> bool:
    return "{{step_" in step.arguments_json


def resolve_arguments(
    step: Step,
    prior_outputs: dict[int, Any],
    *,
    client: LLMClient,
) -> dict[str, Any]:
    """Turn a step's planned arguments into ones that can actually run.

    A plan is written before anything has been observed, so arguments that
    depend on earlier results can only be placeholders. Substituting them
    textually is not enough: step 1 may return a list of ten filenames when
    the next step needs one, or a whole document when the intent asks for a
    single line.

    So resolution is a model call, not a string replace. The model is given
    the step's intent, the planned arguments and the real outputs, and
    produces concrete arguments -- which are then validated against the
    tool's own schema like any others.

    This is where a static plan meets reality, and it is why the plan the
    human approved can stay readable: "write the third line to out/third.txt"
    rather than something full of indexing expressions.
    """
    if not has_placeholders(step):
        return step.arguments

    spec = registry.get(step.tool)
    outputs = "\n".join(
        f"--- step {n} output ---\n{_truncate(value)}"
        for n, value in sorted(prior_outputs.items())
    ) or "(no earlier steps produced output)"

    prompt = _RESOLVE_PROMPT.format(
        signature=spec.signature(),
        intent=step.intent,
        planned=step.arguments_json,
        outputs=outputs,
        lessons=_lessons_block(),
    )

    try:
        # FAST_MODEL is enough here, and that was measured rather than
        # assumed. One run extracted line 2 when the intent said the third
        # line, which looked like the cheap model being bad at counting --
        # but re-running the same resolution five times gave 5/5 correct.
        # The failure was nondeterminism, not capability, so the answer is
        # the retry ladder, not a more expensive model on every step.
        resolved = client.generate_structured(
            prompt, ResolvedArguments, model=config.FAST_MODEL
        )
        arguments = json.loads(resolved.arguments_json)
    except (LLMError, json.JSONDecodeError) as exc:
        raise ExecutionError(
            f"step {step.id}: could not resolve placeholder arguments: {exc}"
        ) from exc

    if not isinstance(arguments, dict):
        raise ExecutionError(
            f"step {step.id}: resolved arguments were not a JSON object"
        )
    return arguments


def run_tool(step: Step, arguments: dict[str, Any]) -> StepResult:
    """Invoke the step's tool and record what happened.

    Tool failures are captured as a result, not raised. A missing file or a
    rejected path is information the agent can act on -- it may retry with
    a different path, or ask the human. Only a failure that makes the step
    unrunnable at all (an unknown tool) is raised.
    """
    try:
        spec = registry.get(step.tool)
    except registry.UnknownToolError as exc:
        raise ExecutionError(f"step {step.id}: {exc}") from exc

    try:
        output = spec.invoke(arguments)
    except Exception as exc:
        return StepResult(
            step_id=step.id,
            ok=False,
            error=f"{type(exc).__name__}: {exc}",
            arguments=arguments,
        )

    return StepResult(step_id=step.id, ok=True, output=output, arguments=arguments)


def verify(
    step: Step,
    result: StepResult,
    prior_outputs: dict[int, Any] | None = None,
    arguments: dict[str, Any] | None = None,
    *,
    client: LLMClient,
) -> StepResult:
    """Check the step's postcondition by looking at the world again.

    Two model calls, both on the cheap model:

      1. Which read-only tool call would reveal whether this held?
      2. Given what that call returned, did it hold?

    The separation matters. Asking "did it work?" with only the tool's own
    return value invites the model to agree with itself -- the thing we are
    trying to check would be judged on its own report. Running a fresh
    read first means the verdict rests on independently observed state.

    The judge is also shown earlier steps' outputs. Without them it can
    only see the thing that changed, not what it was supposed to change
    *to* -- and a model asked to judge on insufficient evidence will invent
    a reason rather than admit it cannot tell.

    Only READ tools may be used to probe. A verification step must never
    change anything, and that is enforced here rather than trusted to the
    model: a probe that proposes a WRITE is refused.
    """
    if not result.ok:
        result.verified = False
        result.evidence = f"The tool call itself failed: {result.error}"
        return result

    read_tools = [t for t in registry.all_tools() if t.risk is Risk.READ]
    catalog = "\n".join(t.catalog_entry() for t in read_tools)

    try:
        probe = client.generate_structured(
            _PROBE_PROMPT.format(
                intent=step.intent,
                postcondition=step.postcondition,
                call=f"{step.tool}({json.dumps(arguments or step.arguments)})",
                catalog=catalog,
            ),
            Probe,
            model=config.FAST_MODEL,
        )
    except LLMError as exc:
        result.verified = False
        result.evidence = f"Could not decide how to verify this step: {exc}"
        return result

    try:
        spec = registry.get(probe.tool)
        if spec.risk is not Risk.READ:
            raise ExecutionError(
                f"refused: {probe.tool} is {spec.risk.value}, "
                "and verification must not change anything"
            )
        observed = spec.invoke(json.loads(probe.arguments_json or "{}"))
        probe_label = f"{probe.tool}({probe.arguments_json})"
    except Exception as exc:
        result.verified = False
        result.evidence = f"Verification probe failed: {type(exc).__name__}: {exc}"
        return result

    try:
        verdict = client.generate_structured(
            _JUDGE_PROMPT.format(
                postcondition=step.postcondition,
                intent=step.intent,
                context=_format_outputs(prior_outputs or {}),
                output=_truncate(result.output),
                probe=probe_label,
                evidence=_truncate(observed),
            ),
            Verdict,
            model=config.FAST_MODEL,
        )
    except LLMError as exc:
        result.verified = False
        result.evidence = f"Could not judge the evidence: {exc}"
        return result

    result.verified = verdict.passed
    result.evidence = f"{verdict.evidence} (checked with {probe_label})"
    return result


def execute_step(
    step: Step,
    prior_outputs: dict[int, Any],
    *,
    client: LLMClient,
) -> StepResult:
    """Resolve, run and verify one step. The whole job, in order."""
    arguments = resolve_arguments(step, prior_outputs, client=client)
    result = run_tool(step, arguments)
    return verify(step, result, prior_outputs, arguments, client=client)


def repair_step(
    step: Step,
    result: StepResult,
    prior_outputs: dict[int, Any],
    *,
    client: LLMClient,
) -> Step | None:
    """Propose a corrected version of a step that failed.

    This is the rung of the failure ladder above a blind retry. A blind
    retry only helps with genuinely transient faults; most agent failures
    are the agent having been wrong about something, and repeating the
    same call repeats the same mistake.

    What makes repair work is that tools explain themselves. "'x.pdf' is
    not UTF-8 text -- use read_pdf" contains the fix. Showing the model
    its own error and letting it choose a different tool or different
    arguments turns a dead end into a correction.

    Returns None if the model cannot produce a valid replacement, which
    the caller treats as "escalate to the human".
    """
    failure = result.error or result.evidence or "unknown failure"
    catalog = registry.catalog()

    try:
        proposal = client.generate_structured(
            _REPAIR_PROMPT.format(
                intent=step.intent,
                call=f"{step.tool}({step.arguments_json})",
                postcondition=step.postcondition,
                failure=failure,
                catalog=catalog,
                outputs=_format_outputs(prior_outputs),
            ),
            RepairedStep,
            model=config.FAST_MODEL,
        )
    except LLMError:
        return None

    try:
        spec = registry.get(proposal.tool)
    except registry.UnknownToolError:
        return None

    try:
        repaired = Step(
            id=step.id,
            intent=step.intent,
            tool=proposal.tool,
            arguments_json=proposal.arguments_json,
            postcondition=proposal.postcondition,
            # Risk is re-derived from the tool that will actually run. A
            # repair may swap in a more dangerous tool, and that must put
            # the step behind the approval gate even though the original
            # was waved through.
            risk=registry.effective_risk(proposal.tool, spec.risk),
        )
    except Exception:
        return None

    # A repair proposing the identical call would just fail identically.
    if (repaired.tool, repaired.arguments_json) == (step.tool, step.arguments_json):
        return None

    return repaired


def _lessons_block() -> str:
    """Past lessons, for the component that actually picks the values.

    Memory was originally injected only into the planner prompt, which
    turned out to miss the point for a whole class of lesson. "Convert
    dates to ISO before calling add_invoice" is not advice about which
    steps to run -- it is advice about what to *put in an argument*, and
    that decision is made here, during resolution. The planner read the
    lesson, wrote a correct plan, and the resolver still produced
    02/10/2026, because it had never been told.

    The general point: memory has to reach the component that makes the
    decision, not just the one that looks like it should know.
    """
    try:
        from memory import store

        lessons = store.active_lessons(limit=6)
    except Exception:
        return ""

    if not lessons:
        return ""
    return (
        "\nThings learned from earlier runs:\n"
        + "\n".join(f"- {row['text']}" for row in lessons)
        + "\n"
    )


def _format_outputs(outputs: dict[int, Any]) -> str:
    """Earlier steps' results, labelled, for inclusion in a prompt."""
    if not outputs:
        return "(no earlier steps produced output)"
    return "\n".join(
        f"--- step {n} output ---\n{_truncate(value)}"
        for n, value in sorted(outputs.items())
    )


def _truncate(value: Any, limit: int = 4000) -> str:
    """Keep tool output from overflowing a prompt."""
    text = value if isinstance(value, str) else repr(value)
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n... [{len(text) - limit} more characters]"
