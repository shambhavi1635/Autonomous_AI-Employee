"""Answering the user.

Every other tool changes or inspects the world. This one exists purely so
the agent can say something back.

Without it, a question like "show me the second entry of expenses.csv"
ends on a `read_file` step whose output is the entire file. The answer is
technically in there, buried, and the agent has no way to single it out.
A task can be perfectly executed and still leave the person who asked it
no better off.

`report` makes the answer an explicit artifact: a step, with a value,
that verification and the UI can both point at. The work it does is
nothing -- it returns what it was given. The value is in *where* that
value comes from: the executor's argument resolution fills `answer` from
earlier steps' outputs, guided by the step's intent, so "the second
entry" is extracted rather than the whole file passed through.
"""

from __future__ import annotations

from core.schemas import Risk
from tools.registry import tool


@tool(risk=Risk.READ)
def report(answer: str) -> str:
    """Give the user a direct answer. Use this as the final step whenever the task asks a question.

    The answer should be the actual information requested -- the value,
    the line, the total -- written out in full, not a description of
    where to find it or a statement that the work was done.

    Good:  "The second entry is 2026-09-07, Software, Design tool licence, 2990, approved."
    Bad:   "The second entry was found in the file."
    """
    return answer
