"""Plan validation and the LLM client's retry policy.

Both are pure logic sitting between the model and the rest of the system,
and both have already been wrong once in ways that were hard to see:
the typed-placeholder bug silently rejected correct plans, and the retry
classifier spent seven seconds retrying a sixteen-hour quota error.
"""

from __future__ import annotations

import pytest

from core.planner import validate_plan, _placeholder_steps
from core.schemas import Plan, Risk, Step
from llm.client import _is_transient, _retry_delay_seconds, _strip_code_fence


@pytest.fixture
def reg(clean_registry):
    @clean_registry.tool(risk=Risk.READ)
    def read_doc(path: str) -> str:
        """Read a document."""
        return path

    @clean_registry.tool(risk=Risk.WRITE)
    def file_record(name: str, amount: float) -> str:
        """File a record with a numeric amount."""
        return name

    return clean_registry


def plan_with(*steps: Step) -> Plan:
    return Plan(task="t", reasoning="r", steps=list(steps))


class TestPlanValidation:
    def test_a_runnable_plan_has_no_problems(self, reg):
        plan = plan_with(
            Step(id=1, intent="read", tool="read_doc",
                 arguments_json='{"path": "a.pdf"}', postcondition="text returned"),
        )
        assert validate_plan(plan) == []

    def test_unknown_tool_is_caught(self, reg):
        plan = plan_with(
            Step(id=1, intent="x", tool="delete_everything",
                 arguments_json="{}", postcondition="p"),
        )
        assert "No tool named" in validate_plan(plan)[0]

    def test_misspelled_argument_is_caught(self, reg):
        # Passes the schema, cannot possibly run.
        plan = plan_with(
            Step(id=1, intent="x", tool="read_doc",
                 arguments_json='{"filename": "a.pdf"}', postcondition="p"),
        )
        assert "bad arguments" in validate_plan(plan)[0]

    def test_forward_reference_is_caught(self, reg):
        plan = plan_with(
            Step(id=1, intent="x", tool="read_doc",
                 arguments_json='{"path": "{{step_9.output}}"}', postcondition="p"),
        )
        assert "has not run yet" in validate_plan(plan)[0]

    def test_self_reference_is_caught(self, reg):
        plan = plan_with(
            Step(id=1, intent="x", tool="read_doc",
                 arguments_json='{"path": "{{step_1.output}}"}', postcondition="p"),
        )
        assert "has not run yet" in validate_plan(plan)[0]

    def test_backward_reference_is_fine(self, reg):
        plan = plan_with(
            Step(id=1, intent="read", tool="read_doc",
                 arguments_json='{"path": "a.pdf"}', postcondition="p"),
            Step(id=2, intent="file", tool="file_record", risk=Risk.WRITE,
                 arguments_json='{"name": "{{step_1.output}}", "amount": 1.0}',
                 postcondition="p"),
        )
        assert validate_plan(plan) == []

    def test_placeholder_is_allowed_in_a_numeric_field(self, reg):
        # The regression that mattered: substituting a plain string stub
        # failed `amount: float`, so a CORRECT plan was rejected -- and
        # the repair round then pushed the model to replace the
        # placeholder with a literal it invented.
        plan = plan_with(
            Step(id=1, intent="read", tool="read_doc",
                 arguments_json='{"path": "a.pdf"}', postcondition="p"),
            Step(id=2, intent="file", tool="file_record", risk=Risk.WRITE,
                 arguments_json='{"name": "{{step_1.output}}",'
                                ' "amount": "{{step_1.output}}"}',
                 postcondition="p"),
        )
        assert validate_plan(plan) == []

    def test_every_problem_is_reported_not_just_the_first(self, reg):
        plan = plan_with(
            Step(id=1, intent="x", tool="nope", arguments_json="{}", postcondition="p"),
            Step(id=2, intent="y", tool="read_doc",
                 arguments_json='{"wrong": 1}', postcondition="p"),
        )
        assert len(validate_plan(plan)) == 2


class TestPlaceholderParsing:
    @pytest.mark.parametrize(
        "value, expected",
        [
            ("{{step_1.output}}", [1]),
            ("prefix {{step_12.output}} suffix", [12]),
            ("{{step_1.output}} and {{step_2.output}}", [1, 2]),
            ("no placeholder here", []),
            ("{{not_a_step}}", []),
            (42, []),
            (None, []),
        ],
    )
    def test_extraction(self, value, expected):
        assert _placeholder_steps(value) == expected


class TestRetryPolicy:
    """A 429 means two opposite things. The body says which."""

    QUOTA = (
        "429 RESOURCE_EXHAUSTED {'error': {'code': 429, 'message': 'You "
        "exceeded your current quota', 'details': [{'@type': "
        "'type.googleapis.com/google.rpc.RetryInfo', 'retryDelay': '57464s'}]}}"
    )

    def test_exhausted_quota_is_not_retried(self):
        # Retrying a sixteen-hour wait burns time and buries Google's own
        # message under "failed after 3 attempts".
        assert _is_transient(Exception(self.QUOTA)) is False

    def test_the_real_delay_is_parsed(self):
        assert _retry_delay_seconds(Exception(self.QUOTA)) == 57464.0

    @pytest.mark.parametrize(
        "text, expected",
        [
            ("429 Too Many Requests 'retryDelay': '7s'", True),
            ("429 rate limit 'retryDelay': '29s'", True),
            ("429 rate limit 'retryDelay': '31s'", False),
            ("429 Too Many Requests", True),      # no delay given, assume short
            ("503 Service Unavailable", True),
            ("504 deadline exceeded", True),
            ("401 invalid api key", False),
            ("400 malformed request", False),
            ("403 permission denied", False),
        ],
    )
    def test_classification(self, text, expected):
        assert _is_transient(Exception(text)) is expected


class TestCodeFenceStripping:
    @pytest.mark.parametrize(
        "raw, expected",
        [
            ('```json\n{"a": 1}\n```', '{"a": 1}'),
            ('```\n{"a": 1}\n```', '{"a": 1}'),
            ('{"a": 1}', '{"a": 1}'),
        ],
    )
    def test_fences_are_removed(self, raw, expected):
        # Gemini 3 preview models sometimes fence their JSON despite a
        # response schema being set.
        assert _strip_code_fence(raw) == expected
