"""The failure ladder's routing.

    step fails -> repair (fix this step)  -> still failing
               -> replan (think again)    -> still failing
               -> stop and tell the human

The rungs are a policy decision, and policy is exactly the kind of thing
that drifts when someone edits a condition months later. These tests pin
it down without a model call: `after_step` is a pure function of state.
"""

from __future__ import annotations

import pytest

from core import config
from core.graph import after_approval, after_step
from core.schemas import Plan, Step

TWO_STEP_PLAN = Plan(
    task="t",
    reasoning="r",
    steps=[
        Step(id=1, intent="a", tool="read_file",
             arguments_json='{"path": "a.txt"}', postcondition="p"),
        Step(id=2, intent="b", tool="read_file",
             arguments_json='{"path": "b.txt"}', postcondition="p"),
    ],
).model_dump(mode="json")


def state(*, cursor, results, attempts=None, replans=0, status="running"):
    return {
        "task": "t",
        "plan": TWO_STEP_PLAN,
        "cursor": cursor,
        "results": results,
        "attempts": attempts or {},
        "replans": replans,
        "status": status,
    }


def ok(step_id):
    return {"step_id": step_id, "ok": True, "verified": True}


def failed(step_id, *, ok_=True):
    """Ran but the postcondition did not hold -- the common failure."""
    return {"step_id": step_id, "ok": ok_, "verified": False,
            "evidence": "the file was not created"}


class TestHappyPath:
    def test_continues_while_steps_remain(self):
        assert after_step(state(cursor=1, results=[ok(1)])) == "step"

    def test_finishes_when_the_plan_is_done(self):
        assert after_step(state(cursor=2, results=[ok(1), ok(2)])) == "__end__"

    def test_a_step_budget_stops_a_runaway(self, monkeypatch):
        monkeypatch.setattr(config, "MAX_STEPS", 1)
        assert after_step(state(cursor=1, results=[ok(1)])) == "__end__"


class TestRepairRung:
    def test_first_failure_goes_to_repair(self):
        assert after_step(state(cursor=1, results=[failed(1)])) == "repair"

    def test_a_tool_that_raised_also_goes_to_repair(self):
        # Both failure shapes take the same rung: a tool that errored, and
        # a tool that returned cleanly without achieving anything.
        result = {"step_id": 1, "ok": False, "error": "FileAccessError: no such file"}
        assert after_step(state(cursor=1, results=[result])) == "repair"

    def test_repair_is_not_offered_twice(self, monkeypatch):
        monkeypatch.setattr(config, "MAX_REPLANS", 0)
        assert after_step(
            state(cursor=1, results=[failed(1)], attempts={"1": 2})
        ) == "__end__"


class TestReplanRung:
    def test_exhausted_repair_falls_through_to_replan(self):
        assert after_step(
            state(cursor=1, results=[failed(1)], attempts={"1": 2})
        ) == "replan"

    def test_replanning_is_not_endless(self):
        # Each re-plan interrupts the human for approval, so the limit is
        # also a limit on how often the agent may interrupt.
        assert after_step(
            state(cursor=1, results=[failed(1)], attempts={"1": 2}, replans=1)
        ) == "__end__"

    def test_a_second_run_gets_its_own_repair_budget(self):
        # After re-planning, attempts reset -- the new plan's steps have
        # not been tried yet.
        assert after_step(
            state(cursor=1, results=[failed(1)], attempts={}, replans=1)
        ) == "repair"


class TestOperatorDecisions:
    def test_a_skipped_step_is_not_a_failure(self):
        # The human chose to skip it at gate 2. Repairing it would be
        # the agent overruling that decision.
        skipped = {"step_id": 1, "ok": False, "error": "skipped by operator"}
        assert after_step(state(cursor=1, results=[skipped])) == "step"

    @pytest.mark.parametrize("status", ["failed", "rejected"])
    def test_a_stopped_run_stays_stopped(self, status):
        assert after_step(state(cursor=0, results=[], status=status)) == "__end__"

    def test_approval_starts_execution(self):
        assert after_approval(state(cursor=0, results=[])) == "step"

    @pytest.mark.parametrize("status", ["rejected", "failed"])
    def test_without_approval_nothing_runs(self, status):
        assert after_approval(
            state(cursor=0, results=[], status=status)
        ) == "__end__"
