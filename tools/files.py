"""File tools: the agent's first real capabilities.

Three tools, chosen because together they are enough to plan and run a
genuine multi-step task while staying completely safe:

    list_files   find what is there
    read_file    look at it
    write_file   change it

Every path passes through _resolve() first, which confines the agent to
the sandbox directory. That check lives here, in one function, rather than
being repeated in each tool -- a safety rule copied three times is a safety
rule that will eventually be copied wrong.

Note what this module does NOT import: nothing from `llm/`, nothing from
`core/` except config and the Risk enum. These are ordinary functions. You
can call read_file() in a REPL with no API key, no agent and no network.
"""

from __future__ import annotations

from pathlib import Path

from core.config import SANDBOX_DIR, WORKSPACES
from core.schemas import Risk
from tools.registry import tool

# Reading a 2GB file would blow up the process and, if its contents reached
# a prompt, the token budget too. Fail with a clear message instead.
MAX_READ_BYTES = 1_000_000


class FileAccessError(RuntimeError):
    """A path was rejected, or a file operation could not be completed."""


def _workspaces() -> dict[str, Path]:
    """Current workspaces, resolved. Read at call time so tests can patch."""
    return {name: Path(path).resolve() for name, path in WORKSPACES.items()}


def _split_workspace(raw_path: str) -> tuple[str, Path, str]:
    """Work out which workspace a path refers to.

    The first segment names a workspace when it matches one:
    "work/report.xlsx" means report.xlsx in the workspace called `work`.
    Anything else is interpreted in `sandbox`, so "invoices/acme.pdf"
    keeps working and single-workspace setups need no prefix at all.
    """
    spaces = _workspaces()
    cleaned = raw_path.strip().replace("\\", "/").lstrip("/")
    head, _, rest = cleaned.partition("/")

    if head.lower() in spaces and head.lower() != "sandbox":
        return head.lower(), spaces[head.lower()], rest
    if head.lower() == "sandbox":
        return "sandbox", spaces["sandbox"], rest
    return "sandbox", spaces["sandbox"], cleaned


def _resolve(raw_path: str, *, must_exist: bool) -> Path:
    """Turn an agent-supplied path into a safe absolute path.

    The agent supplies paths as plain strings, and those strings may come
    from a model that misread a task, or from text inside a document it
    was asked to process. Neither is trustworthy, so:

      - the path is interpreted inside a named workspace
      - the result is resolved (following '..' and symlinks)
      - the resolved path must still be inside that workspace

    Resolving *before* checking is what makes this work. A naive check on
    the raw string would pass "notes/../../../Windows/System32/config",
    because the traversal only becomes visible once the path is normalised.
    """
    cleaned = raw_path.strip()

    # Absolute paths are refused outright rather than reinterpreted.
    # Stripping the leading separator and treating "/etc/passwd" as a
    # relative path inside a workspace would silently turn a request for
    # a system file into a request for a file that happens not to exist
    # -- a confusing answer to a question that deserves a clear "no".
    if cleaned.startswith(("/", "\\")) or (len(cleaned) > 1 and cleaned[1] == ":"):
        allowed = ", ".join(sorted(_workspaces()))
        raise FileAccessError(
            f"Path {raw_path!r} is absolute and was refused -- it is outside "
            f"every allowed workspace. Use a workspace-relative path such as "
            f"'notes.txt'. Available workspaces: {allowed}."
        )

    _, root, relative = _split_workspace(cleaned)

    candidate = Path(relative)
    resolved = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()

    if not resolved.is_relative_to(root):
        allowed = ", ".join(sorted(_workspaces()))
        raise FileAccessError(
            f"Path {raw_path!r} is outside every allowed workspace and was "
            f"refused. Available workspaces: {allowed}."
        )

    if must_exist and not resolved.exists():
        raise FileAccessError(f"No such file: {raw_path!r}")

    return resolved


def _display(path: Path) -> str:
    """Show a path the way the agent should refer back to it.

    The workspace prefix appears only when there is more than one, so a
    plain setup reads as "notes.txt" rather than "sandbox/notes.txt".
    """
    spaces = _workspaces()
    for name, root in spaces.items():
        if path.is_relative_to(root):
            relative = path.relative_to(root).as_posix()
            return relative if len(spaces) == 1 else f"{name}/{relative}"
    return path.as_posix()


@tool(risk=Risk.READ)
def list_files(pattern: str = "**/*") -> list[str]:
    """List files in the sandbox, optionally filtered by a glob pattern.

    Use this to discover what exists before assuming a filename. The
    default lists everything, including inside subfolders. Narrow it with
    patterns like '*.txt' (top level only) or '**/*.pdf' (any depth).
    Returns paths relative to the sandbox.
    """
    # The default is recursive on purpose. With a plain '*', a file the
    # agent had just written into a subfolder would not appear, and the
    # agent would reasonably conclude the write had failed.
    found: list[str] = []
    for root in _workspaces().values():
        if root.exists():
            found.extend(_display(p) for p in root.glob(pattern) if p.is_file())
    return sorted(found)


@tool(risk=Risk.READ)
def read_file(path: str) -> str:
    """Read a plain text file (.txt, .csv, .md, .json) -- NOT a PDF, use read_pdf for those.

    The path is relative to the sandbox, for example 'notes.txt' or
    'invoices/summary.csv'. PDFs are binary and will be refused.

    Note the first line: the registry puts only a docstring's opening
    paragraph in the catalog, so anything the planner must know when
    *choosing* this tool belongs there, not further down.
    """
    resolved = _resolve(path, must_exist=True)

    if resolved.is_dir():
        raise FileAccessError(f"{path!r} is a directory, not a file.")

    size = resolved.stat().st_size
    if size > MAX_READ_BYTES:
        raise FileAccessError(
            f"{path!r} is {size:,} bytes, over the {MAX_READ_BYTES:,} byte "
            "limit. Reading it would overflow the model's context."
        )

    try:
        return resolved.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise FileAccessError(
            f"{path!r} is not UTF-8 text -- it looks like a binary file."
        ) from exc


@tool(risk=Risk.WRITE)
def write_file(path: str, content: str) -> str:
    """Write text to a file in the sandbox, replacing it if it exists.

    Creates parent directories as needed. Returns a short confirmation
    naming the file and how many characters were written. Overwrites
    without warning, so read the file first if its current contents matter.
    """
    resolved = _resolve(path, must_exist=False)

    if resolved.is_dir():
        raise FileAccessError(f"{path!r} is a directory, not a file.")

    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(content, encoding="utf-8")
    return f"Wrote {len(content)} characters to {_display(resolved)}"
