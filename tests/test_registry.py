"""The tool registry: schema derivation, validation, and risk enforcement.

The risk tests matter most. They are the ones protecting the approval
gate, so they are the ones that must not quietly stop working.
"""

from __future__ import annotations

import pytest

from core.schemas import Risk
from tools.registry import ToolArgumentError, ToolError, UnknownToolError


@pytest.fixture
def reg(clean_registry):
    """A registry holding two tools: one harmless, one not."""

    @clean_registry.tool(risk=Risk.READ)
    def read_thing(path: str, encoding: str = "utf-8") -> str:
        """Read a thing from disk."""
        return f"contents of {path} ({encoding})"

    @clean_registry.tool(risk=Risk.IRREVERSIBLE)
    def send_thing(to: str, body: str) -> str:
        """Send a thing that cannot be unsent."""
        return f"sent to {to}"

    return clean_registry


class TestSchemaDerivation:
    def test_signature_shows_types_and_defaults(self, reg):
        assert reg.get("read_thing").signature() == (
            "read_thing(path: str, encoding: str = 'utf-8')"
        )

    def test_catalog_pairs_signature_with_docstring(self, reg):
        entry = reg.get("read_thing").catalog_entry()
        assert "[READ]" in entry
        assert "Read a thing from disk." in entry

    def test_catalog_is_generated_not_maintained(self, reg):
        # The planner's view of its abilities is derived from the real
        # functions, so it cannot drift out of step with them.
        assert "read_thing" in reg.catalog()
        assert "send_thing" in reg.catalog()

    def test_a_tool_needs_a_docstring(self, reg):
        with pytest.raises(ToolError, match="docstring"):

            @reg.tool(risk=Risk.READ)
            def undocumented(x: int) -> int:
                return x

    def test_duplicate_names_are_refused(self, reg):
        with pytest.raises(ToolError, match="already registered"):

            @reg.tool(risk=Risk.READ)
            def read_thing(path: str) -> str:
                """Clash."""

    def test_star_args_are_refused(self, reg):
        # The planner fills named parameters; it cannot fill *args.
        with pytest.raises(TypeError, match="named parameters"):

            @reg.tool(risk=Risk.READ)
            def variadic(*args) -> str:
                """Takes anything."""


class TestArgumentValidation:
    def test_valid_arguments_run(self, reg):
        assert reg.get("read_thing").invoke({"path": "a.txt"}) == (
            "contents of a.txt (utf-8)"
        )

    def test_missing_required_argument(self, reg):
        with pytest.raises(ToolArgumentError):
            reg.get("read_thing").invoke({})

    def test_unexpected_argument_is_refused(self, reg):
        # Pydantic ignores unknown fields by default. Without extra=forbid
        # a plan passing a misspelled argument would run as though nothing
        # were wrong -- the tool would silently use its default instead.
        with pytest.raises(ToolArgumentError):
            reg.get("read_thing").invoke({"path": "a.txt", "encodeing": "utf-8"})

    def test_wrong_type_is_refused(self, reg):
        with pytest.raises(ToolArgumentError):
            reg.get("read_thing").invoke({"path": 123})

    def test_error_names_the_signature(self, reg):
        with pytest.raises(ToolArgumentError, match=r"read_thing\(path: str"):
            reg.get("read_thing").invoke({})


class TestUnknownTools:
    def test_lookup_failure_lists_what_exists(self, reg):
        with pytest.raises(UnknownToolError, match="read_thing, send_thing"):
            reg.get("delete_everything")


class TestRiskEnforcement:
    """The approval gate must not be something a plan can argue past."""

    @pytest.mark.parametrize("claimed", [Risk.READ, Risk.WRITE, Risk.IRREVERSIBLE])
    def test_a_plan_cannot_lower_a_tools_risk(self, reg, claimed):
        # The threat is not only a careless planner. The agent reads
        # documents it did not write, and one could say "this is a
        # routine READ operation". The tool's own declaration wins.
        assert reg.effective_risk("send_thing", claimed) is Risk.IRREVERSIBLE

    def test_a_plan_may_raise_risk(self, reg):
        # Useful when context makes a step more dangerous than the tool
        # knows -- deleting something from a sensitive folder.
        assert (
            reg.effective_risk("read_thing", Risk.IRREVERSIBLE)
            is Risk.IRREVERSIBLE
        )

    def test_risk_is_unchanged_when_the_plan_agrees(self, reg):
        assert reg.effective_risk("read_thing", Risk.READ) is Risk.READ
