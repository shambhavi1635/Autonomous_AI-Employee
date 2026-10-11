"""The planner: turn a task into a validated, executable Plan.

Three things happen here, in order:

  1. Build the prompt -- the task plus a tool catalog generated from the
     registry, so the model's view of its abilities is always current.
  2. Ask the model for a Plan, constrained to the Pydantic schema.
  3. Check the plan against the real tools before anyone sees it.

Step 3 is the part that is easy to skip and expensive to skip. A
schema-valid plan can still be nonsense: it can name a tool that does not
exist, or pass `filename` to a tool whose parameter is `path`. Catching
that here means the human reviews a plan that can actually run, and the
executor never has to handle a malformed one.

When validation fails we show the model its own errors and ask once more.
A single repair attempt fixes most of these; looping on it would just burn
tokens on a model that has misunderstood something more fundamental.
"""

from __future__ import annotations

import json
from pathlib import Path

from core import config
from core.schemas import Plan, Step
from llm.client import LLMClient, LLMError
from tools import registry

PROMPT_PATH = config.PROJECT_ROOT / "llm" / "prompts" / "planner.md"

# One repair round. If the model cannot produce a runnable plan after being
# shown exactly what was wrong, the problem is not something another attempt
# will fix -- better to surface it than to spend money discovering that.
MAX_REPAIR_ATTEMPTS = 1


class PlanningError(RuntimeError):
    """The model could not produce a usable plan."""


def recall(task: str) -> str:
    """What the agent knows that is relevant to this task.

    Three things, in increasing generality: plans that worked for similar
    tasks, lessons from past runs, and facts about named things.

    Everything here is capped. The temptation is to pass all of memory
    on the grounds that the model can ignore what is irrelevant -- but it
    does not ignore it, it tries to use it. A planner shown three
    unrelated past plans will try to follow them, so `most_similar`
    applies a threshold and returns nothing when nothing is close
    enough. No memory is better than misleading memory.
    """
    from memory import store
    from memory.semantic import get_embedder, most_similar

    sections: list[str] = []

    # Only episodes embedded by the model currently loaded: vectors
    # from different embedders are not comparable.
    episodes = store.episodes_with_embeddings(get_embedder().name)
    similar = most_similar(task, [(e, v) for e, v in episodes], top_k=2)
    if similar:
        lines = []
        for episode, score in similar:
            outcome = "worked" if episode.succeeded else f"FAILED: {episode.outcome}"
            tools_used = " -> ".join(
                s.get("tool", "?") for s in episode.plan.get("steps", [])
            )
            lines.append(
                f'- "{episode.summary}" ({outcome})\n    {tools_used}'
            )
        sections.append("Similar tasks you have done before:\n" + "\n".join(lines))

    lessons = store.active_lessons()
    if lessons:
        store.mark_lessons_used([row["id"] for row in lessons])
        sections.append(
            "Things you have learned:\n"
            + "\n".join(f"- {row['text']}" for row in lessons)
        )

    facts = store.all_facts()
    if facts:
        sections.append(
            "Facts you have recorded:\n"
            + "\n".join(
                f"- {row['subject']}: {row['predicate']} = {row['value']}"
                for row in facts[:15]
            )
        )

    if not sections:
        return ""

    return (
        "\n# What you already know\n\n"
        + "\n\n".join(sections)
        + "\n\nThis comes from earlier runs and may be out of date. Prefer what\n"
        "you can observe now -- if a lesson conflicts with what list_files\n"
        "or read_file actually shows you, trust the observation.\n\n"
    )


def build_prompt(task: str, *, use_memory: bool = True) -> str:
    """Compose the planner prompt from the template, catalog and memory."""
    template = PROMPT_PATH.read_text(encoding="utf-8")
    # Deliberately not str.format(). The prompt is full of literal braces --
    # JSON examples, and the {{step_N.output}} placeholder syntax itself --
    # and format() would silently eat them, teaching the model a syntax our
    # own parser does not recognise. Plain substitution has no such trap.
    memory = recall(task) if use_memory else ""
    return (
        template.replace("{catalog}", registry.catalog())
        .replace("{memory}", memory)
        .replace("{task}", task)
    )


def validate_plan(plan: Plan) -> list[str]:
    """Return a list of problems with this plan. Empty means runnable.

    Checks what the schema cannot: that the plan refers to tools that
    exist, and passes them arguments they accept. Arguments containing a
    {{step_N.output}} placeholder are checked for shape but not value,
    since the real value does not exist until that step has run.
    """
    problems: list[str] = []

    for step in plan.steps:
        try:
            spec = registry.get(step.tool)
        except registry.UnknownToolError as exc:
            problems.append(f"step {step.id}: {exc}")
            continue

        arguments = step.arguments

        # A placeholder stands in for a value we do not have yet. Validate
        # that it points at an earlier step -- a forward or self reference
        # can never be resolved.
        for name, value in arguments.items():
            for referenced in _placeholder_steps(value):
                if referenced >= step.id:
                    problems.append(
                        f"step {step.id}: argument {name!r} refers to "
                        f"{{{{step_{referenced}.output}}}}, which has not run yet"
                    )
                elif plan.step_by_id(referenced) is None:
                    problems.append(
                        f"step {step.id}: argument {name!r} refers to "
                        f"step {referenced}, which is not in the plan"
                    )

        # Type-check the arguments, with placeholders stubbed out so a
        # "{{step_1.output}}" string does not fail an int field for the
        # wrong reason.
        try:
            spec.args_model(**_stub_placeholders(arguments, spec.args_model))
        except Exception as exc:  # pydantic ValidationError or TypeError
            first = str(exc).splitlines()
            detail = first[1].strip() if len(first) > 1 else str(exc)
            problems.append(
                f"step {step.id}: bad arguments for {spec.signature()} -- {detail}"
            )

    return problems


def make_plan(
    task: str,
    *,
    prior_failure: str | None = None,
    client: LLMClient | None = None,
) -> Plan:
    """Produce a validated Plan for a task, or raise PlanningError.

    `prior_failure` is set when an earlier plan for this same task failed
    and the agent is thinking again. Passing it is what distinguishes a
    second plan from a repeat of the first: without it the model has no
    reason not to propose exactly the approach that already did not work.
    """
    if not task.strip():
        raise PlanningError("Task is empty.")
    if not registry.all_tools():
        raise PlanningError(
            "No tools are registered. Import a tool module (for example "
            "`import tools.files`) before planning."
        )

    client = client or LLMClient()
    prompt = build_prompt(task)
    system = "You are a careful planner. Follow the rules exactly."

    if prior_failure:
        prompt += (
            "\n\n# An earlier attempt at this task failed\n\n"
            f"{prior_failure}\n\n"
            "Plan a different approach. Do not repeat the step that "
            "failed in the same form -- if a tool, path or assumption was "
            "wrong, change it. If the task cannot be done with the tools "
            "available, say so plainly in `reasoning` rather than "
            "producing a plan you expect to fail again.\n"
        )

    attempt = 0
    while True:
        try:
            plan = client.generate_structured(prompt, Plan, system=system)
        except LLMError as exc:
            raise PlanningError(f"Model could not produce a plan: {exc}") from exc

        # The model sometimes paraphrases the task into `task`. Keep the
        # user's own words -- the human is approving a plan for what they
        # asked, not for the model's summary of it.
        plan.task = task

        problems = validate_plan(plan)
        if not problems:
            return plan

        if attempt >= MAX_REPAIR_ATTEMPTS:
            raise PlanningError(
                "Plan does not match the available tools:\n  "
                + "\n  ".join(problems)
            )

        attempt += 1
        prompt = (
            f"{build_prompt(task)}\n\n"
            "# Your previous attempt was rejected\n\n"
            "These problems were found. Fix them and produce the plan again.\n"
            + "\n".join(f"- {p}" for p in problems)
        )


def _placeholder_steps(value: object) -> list[int]:
    """Step numbers referenced by {{step_N.output}} inside a value."""
    if not isinstance(value, str):
        return []
    found = []
    for fragment in value.split("{{step_")[1:]:
        number = fragment.split(".")[0].split("}")[0].strip()
        if number.isdigit():
            found.append(int(number))
    return found


def _stub_placeholders(arguments: dict, model: type) -> dict:
    """Replace placeholders with a stand-in of the right type.

    A placeholder is a string standing in for a value that does not exist
    yet, and it may fill a field of any type -- `total_amount` is a float,
    but at plan time all the planner can say is {{step_2.output}}.

    Substituting a plain string here would fail that float field and
    reject a correct plan. Worse, the repair round would then push the
    model to replace the placeholder with a literal it invents -- a
    hardcoded 0.0 instead of the real total. That happened, and it is why
    the stand-in has to match the declared type.
    """
    stubs: dict[str, object] = {}
    for name, value in arguments.items():
        if not _placeholder_steps(value):
            stubs[name] = value
            continue
        annotation = getattr(model.model_fields.get(name), "annotation", str)
        stubs[name] = {float: 1.0, int: 1, bool: True, list: [], dict: {}}.get(
            annotation, "placeholder"
        )
    return stubs


def render(plan: Plan) -> str:
    """Format a plan as text, for the terminal approval gate."""
    lines = [
        f"TASK: {plan.task}",
        "",
        f"APPROACH: {plan.reasoning}",
        "",
        f"{len(plan.steps)} steps, highest risk: {plan.highest_risk.value.upper()}",
        "",
    ]
    for step in plan.steps:
        gate = "   *** NEEDS YOUR APPROVAL ***" if step.needs_approval else ""
        lines.append(f"  {step.id}. [{step.risk.value:12}] {step.intent}{gate}")
        lines.append(f"       call : {step.tool}({json.dumps(step.arguments)})")
        lines.append(f"       check: {step.postcondition}")
    return "\n".join(lines)
