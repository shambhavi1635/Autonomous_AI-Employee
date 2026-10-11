"""The plan data model: what it accepts, and what it must refuse."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from core.schemas import Plan, Risk, Step, StepResult


def make_step(**overrides) -> Step:
    defaults = dict(
        id=1,
        intent="read the notes",
        tool="read_file",
        arguments_json='{"path": "notes.txt"}',
        postcondition="read_file returns the notes",
    )
    return Step(**{**defaults, **overrides})


class TestStep:
    def test_arguments_parse_from_json(self):
        assert make_step().arguments == {"path": "notes.txt"}

    def test_no_arguments_defaults_to_empty(self):
        assert make_step(arguments_json="{}").arguments == {}

    @pytest.mark.parametrize("bad", ["not json", "[1, 2]", '"a string"', "42"])
    def test_arguments_must_be_a_json_object(self, bad):
        # Caught at plan time the human sees a clean error before
        # approving; caught at run time it is a crash mid-execution.
        with pytest.raises(ValidationError):
            make_step(arguments_json=bad)

    @pytest.mark.parametrize("field", ["intent", "tool", "postcondition"])
    @pytest.mark.parametrize("blank", ["", "   ", "\n"])
    def test_required_text_cannot_be_blank(self, field, blank):
        # A schema-constrained model fills every field, but nothing stops
        # it filling one with "". An empty postcondition would silently
        # disable verification for that step.
        with pytest.raises(ValidationError):
            make_step(**{field: blank})

    def test_only_irreversible_steps_need_approval(self):
        assert make_step(risk=Risk.READ).needs_approval is False
        assert make_step(risk=Risk.WRITE).needs_approval is False
        assert make_step(risk=Risk.IRREVERSIBLE).needs_approval is True


class TestPlan:
    def test_steps_are_renumbered_in_order(self):
        # Models number steps unreliably, and a human editing a plan will
        # not renumber after deleting one. The ids are ours to assign.
        plan = Plan(
            task="t",
            reasoning="r",
            steps=[make_step(id=99), make_step(id=4), make_step(id=50)],
        )
        assert [s.id for s in plan.steps] == [1, 2, 3]

    def test_a_plan_needs_at_least_one_step(self):
        with pytest.raises(ValidationError):
            Plan(task="t", reasoning="r", steps=[])

    def test_highest_risk_is_what_the_human_is_warned_about(self):
        plan = Plan(
            task="t",
            reasoning="r",
            steps=[
                make_step(risk=Risk.READ),
                make_step(risk=Risk.IRREVERSIBLE),
                make_step(risk=Risk.WRITE),
            ],
        )
        assert plan.highest_risk is Risk.IRREVERSIBLE

    def test_step_lookup_by_id(self):
        plan = Plan(task="t", reasoning="r", steps=[make_step(), make_step()])
        assert plan.step_by_id(2).id == 2
        assert plan.step_by_id(99) is None


class TestStepResult:
    """`ok` and `verified` are separate facts, and both must hold."""

    @pytest.mark.parametrize(
        "ok, verified, expected",
        [
            (True, True, True),     # ran, and the world agrees
            (True, False, False),   # ran cleanly, achieved nothing
            (True, None, False),    # ran, not checked yet
            (False, None, False),   # did not run
            (False, True, False),   # nonsense, but must not pass
        ],
    )
    def test_success_requires_both(self, ok, verified, expected):
        result = StepResult(step_id=1, ok=ok, verified=verified)
        assert result.succeeded is expected
