"""The data model: what a plan is, and what happens when you run one.

This is the contract every other component agrees on. The planner produces
a Plan, the UI renders one, the human edits one, the executor runs one, and
the verifier checks each Step. Getting this shape right matters more than
any single piece of logic, because changing it later means changing
everything that touches it.

Two families of objects, kept deliberately separate:

  PLANNED  (Plan, Step)        -- what the model proposes. Untrusted input.
  OBSERVED (StepResult, RunLog) -- what actually happened. Produced by our
                                   own code, never by the model.

That split is the whole point. If the model could write its own results,
"did it work?" would be answered by the thing we are trying to check.
"""

from __future__ import annotations

import json
import re
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator


# An unquoted {{step_N.output}} sitting where a JSON value belongs.
# Matches only after a colon, so placeholders already quoted are untouched.
_BARE_PLACEHOLDER = re.compile(r"(:\s*)(\{\{step_\d+\.output\}\})")


class Risk(str, Enum):
    """How much damage a step can do.

    The executor reads this to decide whether to stop and ask the human.
    It is declared per step by the planner, but the tool's own declared
    risk always wins -- a planner cannot talk its way past a gate by
    labelling a send_email step as READ.

    READ          nothing changes. List files, read a cell, search an inbox.
    WRITE         something changes but can be undone. Write a file, insert
                  a ledger row.
    IRREVERSIBLE  cannot be taken back. Send an email, submit a form,
                  delete a file. Always requires explicit human approval of
                  the exact payload, immediately before it runs.
    """

    READ = "read"
    WRITE = "write"
    IRREVERSIBLE = "irreversible"


class Step(BaseModel):
    """One action in a plan.

    The two fields people forget are `postcondition` and `risk`, and they
    are the two that make this project work.
    """

    id: int = Field(description="Position in the plan, starting at 1")

    intent: str = Field(
        description=(
            "What this step achieves, in plain language, for a human "
            "reading the plan. Not the tool call -- the purpose."
        )
    )

    tool: str = Field(description="Name of the tool to invoke, from the catalog")

    arguments_json: str = Field(
        default="{}",
        description=(
            'Arguments for the tool as a JSON object string, for example '
            '{"path": "sandbox/notes.txt"}. Use {} if the tool takes none.'
        ),
    )

    postcondition: str = Field(
        description=(
            "How we will know this step actually worked, stated as a fact "
            "we can check by looking at the world afterwards. "
            "Good: 'data/ledger.db contains a row with invoice_number "
            "INV-2026-0417'. Bad: 'the row was inserted successfully'."
        )
    )

    risk: Risk = Field(
        default=Risk.READ,
        description="How much damage this step can do if it goes wrong",
    )

    @field_validator("intent", "tool", "postcondition")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        """Reject empty strings the model may emit to satisfy the schema.

        A schema-constrained model will fill every required field, but
        nothing stops it filling one with "". An empty postcondition would
        silently disable verification for that step, so we refuse it here
        rather than discovering it at run time.
        """
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("must not be empty")
        return cleaned

    @field_validator("arguments_json")
    @classmethod
    def _parses_to_object(cls, value: str) -> str:
        """Reject argument strings that are not a JSON object.

        Caught here, at plan time, the human sees a clean error before
        approving. Caught at run time, it is a crash mid-execution.
        """
        text = value or "{}"
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            # One known malformation, repaired rather than rejected.
            # Told that placeholders may fill numeric fields, models write
            #     "total_amount": {{step_2.output}}
            # with no quotes, which is not valid JSON. The intent is
            # unambiguous and the repair is mechanical, so fixing it beats
            # failing a plan that is otherwise correct -- and beats asking
            # the prompt more firmly, which only works most of the time.
            repaired = _BARE_PLACEHOLDER.sub(r'\1"\2"', text)
            try:
                parsed = json.loads(repaired)
            except json.JSONDecodeError as exc:
                raise ValueError(f"not valid JSON: {exc}") from exc
            text = repaired

        if not isinstance(parsed, dict):
            raise ValueError(f"must be a JSON object, got {type(parsed).__name__}")
        return text

    @property
    def arguments(self) -> dict[str, Any]:
        """The parsed arguments.

        Stored as a string rather than a dict because the Gemini Developer
        API rejects open-ended objects: a dict[str, Any] compiles to
        `additionalProperties: true`, which it refuses outright.

        Nothing is lost by this. These arguments are validated against the
        *tool's own* Pydantic schema before the call -- which is stricter
        than anything the plan schema could express, since the plan schema
        would have to allow every tool's arguments at once.
        """
        return json.loads(self.arguments_json or "{}")

    @property
    def needs_approval(self) -> bool:
        """Whether the executor must pause for the human before running."""
        return self.risk is Risk.IRREVERSIBLE


class Plan(BaseModel):
    """A proposed sequence of steps to accomplish a task.

    A plan is a hypothesis, not a script. Executing step 2 may reveal that
    step 3 was based on a wrong assumption, in which case the executor can
    re-plan -- and because that is a material change, the human approves
    again.
    """

    task: str = Field(description="The user's original request, verbatim")

    reasoning: str = Field(
        description=(
            "Why this approach, in two or three sentences. Shown to the "
            "human so they can judge the plan rather than just the steps."
        )
    )

    steps: list[Step] = Field(min_length=1, description="Steps, in order")

    @model_validator(mode="after")
    def _renumber(self) -> Plan:
        """Force step ids to be 1..n in order.

        Models are unreliable at sequential numbering, especially after a
        human edits the plan. Rather than validating and rejecting, we fix
        it -- the ids are ours to assign, and a wrong number is never worth
        failing a plan over.
        """
        for position, step in enumerate(self.steps, start=1):
            step.id = position
        return self

    @property
    def highest_risk(self) -> Risk:
        """The riskiest thing this plan will do. Shown in the approval UI."""
        order = [Risk.READ, Risk.WRITE, Risk.IRREVERSIBLE]
        return max((s.risk for s in self.steps), key=order.index)

    def step_by_id(self, step_id: int) -> Step | None:
        return next((s for s in self.steps if s.id == step_id), None)


class StepResult(BaseModel):
    """What actually happened when a step ran.

    Produced by our executor and verifier, never by the model. The model is
    allowed to propose; only our code may report.
    """

    step_id: int

    ok: bool = Field(description="Did the tool call complete without error?")

    output: Any = Field(
        default=None, description="Whatever the tool returned, unmodified"
    )

    error: str | None = Field(
        default=None, description="Exception text, if the call failed"
    )

    verified: bool | None = Field(
        default=None,
        description=(
            "Did the postcondition hold afterwards? None means not yet "
            "checked. This is deliberately separate from `ok`: a tool can "
            "return successfully and still not have achieved anything."
        ),
    )

    evidence: str | None = Field(
        default=None,
        description="What we observed when checking the postcondition",
    )

    attempts: int = Field(default=1, description="Including retries")

    @property
    def succeeded(self) -> bool:
        """A step counts as done only when it ran AND the world agrees."""
        return self.ok and self.verified is True
