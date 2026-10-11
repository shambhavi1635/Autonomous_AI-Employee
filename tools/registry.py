"""The tool registry: how a Python function becomes a capability.

The agent generalises over *composition*, not over capabilities. It can
invent new sequences freely, but it can only use tools that exist. This
file is where a tool starts existing.

Decorate a function and four things happen at once, from one source:

    @tool(risk=Risk.WRITE)
    def write_file(path: str, text: str) -> bool:
        '''Overwrite a text file with new content.'''

  1. It appears in the catalog the planner is shown.
  2. An argument schema is derived from its type hints.
  3. Those arguments are validated before the function is ever called.
  4. Its risk tier is recorded, which decides whether the executor must
     stop and ask the human first.

One decorator, no registration list to maintain, nothing to keep in sync.
That single source of truth is the point: a catalog that drifts out of step
with the real tools produces plans that cannot run.

This module deliberately imports nothing from `llm/`. Tools must be
testable without an API key or a network connection.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any, Callable, get_type_hints

from pydantic import BaseModel, ConfigDict, ValidationError, create_model

from core.schemas import Risk


class ToolError(RuntimeError):
    """Base class for registry failures."""


class UnknownToolError(ToolError):
    """The plan named a tool that does not exist."""


class ToolArgumentError(ToolError):
    """The plan supplied arguments that do not fit the tool's signature."""


@dataclass(frozen=True)
class Tool:
    """A callable capability, with everything the agent needs to know."""

    name: str
    description: str
    risk: Risk
    args_model: type[BaseModel]
    fn: Callable[..., Any]

    def signature(self) -> str:
        """One-line signature for the planner's catalog."""
        parts = []
        for field_name, field in self.args_model.model_fields.items():
            type_name = getattr(field.annotation, "__name__", str(field.annotation))
            if field.is_required():
                parts.append(f"{field_name}: {type_name}")
            else:
                parts.append(f"{field_name}: {type_name} = {field.default!r}")
        return f"{self.name}({', '.join(parts)})"

    def catalog_entry(self) -> str:
        """How this tool is presented to the planner."""
        return (
            f"{self.signature()}  [{self.risk.value.upper()}]\n"
            f"    {self.description}"
        )

    def invoke(self, arguments: dict[str, Any]) -> Any:
        """Validate arguments, then call the function.

        Validation happens here rather than inside each tool so that every
        tool gets it for free, and so a bad plan fails with a readable
        message naming the tool and the offending field -- rather than a
        TypeError from somewhere inside the function body.
        """
        try:
            validated = self.args_model(**arguments)
        except ValidationError as exc:
            raise ToolArgumentError(
                f"Invalid arguments for {self.signature()}:\n{exc}"
            ) from exc
        except TypeError as exc:
            raise ToolArgumentError(
                f"Invalid arguments for {self.signature()}: {exc}"
            ) from exc

        return self.fn(**validated.model_dump())


# The one place every tool lives. Populated by the decorator at import time,
# which means a tool module must be imported for its tools to be available.
_REGISTRY: dict[str, Tool] = {}


def _args_model_from_signature(fn: Callable[..., Any]) -> type[BaseModel]:
    """Build a Pydantic model describing the function's parameters.

    Deriving the schema from the real signature, rather than asking the
    author to write it twice, is what keeps the catalog honest: the
    description the planner sees cannot disagree with the function it
    will actually call.
    """
    signature = inspect.signature(fn)
    # get_type_hints resolves string annotations, which `from __future__
    # import annotations` in a tool module would otherwise leave unresolved.
    hints = get_type_hints(fn)

    fields: dict[str, Any] = {}
    for param_name, param in signature.parameters.items():
        if param_name in ("self", "cls"):
            continue
        if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
            raise TypeError(
                f"{fn.__name__}: *args/**kwargs are not supported in tools -- "
                "the planner needs named parameters it can fill in."
            )
        annotation = hints.get(param_name, Any)
        default = ... if param.default is inspect.Parameter.empty else param.default
        fields[param_name] = (annotation, default)

    # extra="forbid" matters. Pydantic ignores unknown fields by default,
    # which would let a plan pass {"path": "a.txt", "colour": "blue"} and
    # run as if nothing were wrong. An unexpected argument means the planner
    # misunderstood the tool, and that is worth failing on while a human is
    # still looking at the plan.
    return create_model(
        f"{fn.__name__}_Args",
        __config__=ConfigDict(extra="forbid"),
        **fields,
    )


def tool(*, risk: Risk) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Register a function as a tool the agent may use.

    `risk` is required and has no default on purpose. Classifying how much
    damage a capability can do is a decision the author must make
    consciously, every time -- a forgotten default would quietly let an
    irreversible action run without a human seeing it.

    The docstring becomes the planner's description of the tool, so it is
    documentation and prompt at the same time. Write it for a reader who
    must decide whether this tool fits a task.
    """

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        name = fn.__name__
        if name in _REGISTRY:
            raise ToolError(f"A tool named {name!r} is already registered.")

        description = inspect.getdoc(fn)
        if not description:
            raise ToolError(
                f"{name}: tools need a docstring -- it is what the planner "
                "reads to decide whether to use this tool."
            )

        _REGISTRY[name] = Tool(
            name=name,
            description=description.strip().split("\n\n")[0].replace("\n", " "),
            risk=risk,
            args_model=_args_model_from_signature(fn),
            fn=fn,
        )
        # Return the function untouched: tools stay ordinary functions that
        # can be imported and unit-tested directly, with no agent involved.
        return fn

    return decorator


def get(name: str) -> Tool:
    """Look up a tool, or fail with the list of what does exist."""
    try:
        return _REGISTRY[name]
    except KeyError:
        available = ", ".join(sorted(_REGISTRY)) or "(none registered)"
        raise UnknownToolError(
            f"No tool named {name!r}. Available: {available}"
        ) from None


def all_tools() -> list[Tool]:
    return [_REGISTRY[name] for name in sorted(_REGISTRY)]


def catalog() -> str:
    """The tool list, formatted for the planner prompt.

    Generated from the registry every time rather than written by hand, so
    the planner's view of its own abilities is always current.
    """
    if not _REGISTRY:
        return "(no tools registered)"
    return "\n".join(t.catalog_entry() for t in all_tools())


def effective_risk(tool_name: str, proposed: Risk) -> Risk:
    """Reconcile the planner's risk label with the tool's own.

    The tool's declared risk always wins when it is higher. The planner is
    an untrusted source: it could label a send_email step as READ, by
    mistake or because a prompt-injected document told it to. The gate must
    not be something the plan can argue its way past.

    A planner may raise the risk of a step (useful: deleting a file in a
    sensitive folder), but never lower it.
    """
    order = [Risk.READ, Risk.WRITE, Risk.IRREVERSIBLE]
    declared = get(tool_name).risk
    return max(declared, proposed, key=order.index)
