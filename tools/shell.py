"""The last resort: running code and commands directly.

Every other tool in this project is narrow on purpose. `excel_update`
can only change cells; `read_file` can only read inside a nominated
folder. That narrowness is what makes the approval gate mean something:
when you approve "click Send in Gmail", you know what you approved.

This file gives that up. `run_python` and `run_command` can do anything
the person running the agent can do, which is the point -- an AI
colleague that cannot reach an unanticipated tool is not much of a
colleague -- and also the whole risk.

Four things hold the line.

**Always IRREVERSIBLE.** Gate 2 fires every time, and the human sees the
exact code before it runs. Not "a shell step": the text.

**A refusal list in code.** Disk formatting, recursive deletes of system
paths, registry writes, user and firewall changes. These are refused
whatever the plan says and whatever the human approves, because a plan
that asks for them is either broken or hostile, and the cost of being
wrong is not recoverable.

**Last resort by description.** The docstrings say so plainly, so the
planner reaches for excel_update before it reaches for a shell. A tool
catalogue is a prompt; what it says changes what gets planned.

**A timeout and captured output.** A command that hangs stops the run
rather than the machine.

What none of this fixes: the agent reads documents it did not write, and
a document can contain instructions. Every other tool is safe against
that because the tool itself is narrow. Here, the only thing between a
malicious invoice and your machine is a person reading the gate. Treat
an unexpected shell step in a plan as a reason to reject it.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from pathlib import Path

from core.config import PROJECT_ROOT
from core.schemas import Risk
from tools.registry import tool

TIMEOUT_SECONDS = 120
MAX_OUTPUT = 8000

# Refused however the plan words it and whatever the human approves.
# Each one is something with no plausible place in an office task and no
# way back: a wiped disk, a broken boot, a changed account.
_NEVER = (
    (r"\bformat\s+[a-z]:", "formatting a drive"),
    (r"\b(rmdir|rd)\b.*\/s", "a recursive directory delete"),
    (r"\brm\s+-rf?\s+[/~]", "a recursive delete from the filesystem root"),
    (r"\bdel\b.*\\windows\\", "deleting from the Windows directory"),
    (r"\breg\s+(add|delete|import)\b", "a registry write"),
    (r"\bwinreg\b|\bimport\s+winreg", "a registry write"),
    (r"\bnet\s+user\b", "a change to user accounts"),
    (r"\bnetsh\s+(advfirewall|firewall)", "a firewall change"),
    (r"\bbcdedit\b|\bdiskpart\b|\bvssadmin\b", "a change to boot or volumes"),
    (r"\bcipher\s+/w", "wiping free space"),
    (r"\bshutdown\b|\bRestart-Computer\b", "shutting the machine down"),
    (r"\bsc\s+(config|delete)\b|\bStop-Service\b", "a change to Windows services"),
    (r"\btakeown\b|\bicacls\b.*\/grant", "a change to file permissions"),
    (r"\bcurl\b.*\|\s*(sh|bash|powershell)", "piping a download into a shell"),
    (r"Invoke-WebRequest.*\|\s*(iex|Invoke-Expression)", "running a downloaded script"),
    (r"\bshutil\.rmtree\s*\(\s*['\"][A-Za-z]:[\\/]{1,2}['\"]", "deleting a whole drive"),
)


class ShellError(RuntimeError):
    """A command was refused, or could not be run."""


def _refuse_if_catastrophic(text: str) -> None:
    """Block the handful of things that cannot be undone.

    Deliberately short. A long denylist invites the belief that anything
    not on it is safe, which is false -- this is a backstop against the
    unrecoverable, not a security boundary. The real protection is a
    person reading the code at the gate.
    """
    flattened = " ".join(text.split())
    for pattern, description in _NEVER:
        if re.search(pattern, flattened, re.IGNORECASE):
            raise ShellError(
                f"Refused: this looks like {description}, which the agent "
                "will not do under any circumstances. If it is genuinely "
                "needed, a person should do it themselves."
            )


def _truncate(text: str) -> str:
    text = text.strip()
    if len(text) <= MAX_OUTPUT:
        return text
    return text[:MAX_OUTPUT] + f"\n... [{len(text) - MAX_OUTPUT} more characters]"


def _report(result: subprocess.CompletedProcess, label: str) -> str:
    parts = [f"{label} finished with exit code {result.returncode}."]
    if result.stdout and result.stdout.strip():
        parts.append(f"\n--- output ---\n{_truncate(result.stdout)}")
    if result.stderr and result.stderr.strip():
        parts.append(f"\n--- errors ---\n{_truncate(result.stderr)}")
    if len(parts) == 1:
        parts.append("It produced no output.")
    return "\n".join(parts)


@tool(risk=Risk.IRREVERSIBLE)
def run_python(code: str) -> str:
    """Run Python code. A LAST RESORT -- use a dedicated tool whenever one exists.

    This can do anything on the machine, so it always needs human
    approval and the person sees the exact code first. Reach for it only
    when no other tool can do the job: use excel_update for
    spreadsheets, write_file for files, add_invoice for the ledger, and
    the browser or desktop tools for applications.

    Print what you want to see -- stdout is returned. The code runs in
    its own process with the project folder as the working directory,
    and is stopped after two minutes.
    """
    if not code.strip():
        raise ShellError("No code given.")
    _refuse_if_catastrophic(code)

    with tempfile.NamedTemporaryFile(
        "w", suffix=".py", delete=False, encoding="utf-8"
    ) as handle:
        handle.write(code)
        script = handle.name

    try:
        result = subprocess.run(
            [sys.executable, script],
            capture_output=True, text=True,
            timeout=TIMEOUT_SECONDS, cwd=str(PROJECT_ROOT),
        )
    except subprocess.TimeoutExpired:
        raise ShellError(
            f"The code was still running after {TIMEOUT_SECONDS} seconds and "
            "was stopped. Something is waiting for input, or looping."
        ) from None
    finally:
        Path(script).unlink(missing_ok=True)

    return _report(result, "The code")


@tool(risk=Risk.IRREVERSIBLE)
def run_command(command: str) -> str:
    """Run a Windows shell command. A LAST RESORT -- prefer any other tool.

    This can do anything on the machine, so it always needs human
    approval and the person sees the exact command first. Use it only
    for things no other tool covers -- a command-line program, a system
    query. Do not use it to read or write files, edit spreadsheets or
    drive applications; there are safer tools for all of those.

    Output is returned, and the command is stopped after two minutes.
    """
    if not command.strip():
        raise ShellError("No command given.")
    _refuse_if_catastrophic(command)

    try:
        result = subprocess.run(
            command, shell=True, capture_output=True, text=True,
            timeout=TIMEOUT_SECONDS, cwd=str(PROJECT_ROOT),
        )
    except subprocess.TimeoutExpired:
        raise ShellError(
            f"The command was still running after {TIMEOUT_SECONDS} seconds "
            "and was stopped. It may be waiting for input."
        ) from None

    return _report(result, "The command")
