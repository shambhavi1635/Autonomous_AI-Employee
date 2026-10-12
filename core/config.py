"""Central configuration.

Everything environment-dependent -- API keys, model IDs, limits -- is
resolved here exactly once. No other module calls os.getenv() or builds a
path by hand. If a setting needs to change, it changes in .env, not in code.

This file grows as the project does: each new setting is added at the point
some code actually reads it, never in advance.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# Anchor paths to the repo root, derived from this file's own location, so
# the agent behaves identically regardless of which directory it is run from.
PROJECT_ROOT = Path(__file__).resolve().parent.parent

load_dotenv(PROJECT_ROOT / ".env")

# The only directory the agent's file tools may touch. Everything the agent
# reads or writes lives under here, so a wrong path, a confused plan or a
# malicious instruction inside a document cannot reach the rest of the disk.
# Widening this is a deliberate decision, made here, not in a tool.
SANDBOX_DIR = PROJECT_ROOT / "sandbox"


def _desktop() -> Path:
    """The user's Desktop, following a OneDrive redirect if there is one.

    Windows moves the Desktop under OneDrive when folder backup is on,
    and the literal ~/Desktop then does not exist. Checking both is the
    difference between the output folder appearing where the user can
    see it and appearing somewhere they never look.
    """
    home = Path.home()
    for candidate in (home / "OneDrive" / "Desktop", home / "Desktop"):
        if candidate.is_dir():
            return candidate
    return home


# Everything the agent produces goes here, separate from the material it
# was given and separate from the project. On the Desktop by default so
# it is somewhere the user actually looks -- a folder buried in a source
# tree is not a place anyone checks for their own documents, and it also
# meant the project directory filled up with generated files.
# Override with OUTPUTS_DIR in .env.
OUTPUTS_DIR = Path(
    os.getenv("OUTPUTS_DIR", "").strip() or _desktop() / "AI_colleage_outputs"
).expanduser()

# Where you put things for the agent to work on. The pair to the outputs
# folder: one for what you hand it, one for what it produces, both on the
# Desktop where you can see them.
#
# A nominated folder rather than the whole Desktop. Nothing technical
# stops the agent being given everything -- it is one line -- but
# write_file overwrites without warning, and "it can only touch the
# folders you chose" is a claim that holds even when the model is wrong
# about something. Widening it stays a deliberate decision, made here.
INBOX_DIR = Path(
    os.getenv("INBOX_DIR", "").strip() or _desktop() / "agent-inbox"
).expanduser()


def _parse_workspaces() -> dict[str, Path]:
    """The directories the agent's file tools may touch, by name.

    Always includes `sandbox`. More can be added in .env:

        WORKSPACES=work=C:/Users/you/Desktop/agent-files;docs=D:/reports

    Named rather than a bare list so the agent can refer to a place
    without knowing an absolute path -- "work/report.xlsx" rather than
    "C:/Users/you/Desktop/agent-files/report.xlsx". That keeps machine
    paths out of plans the human reads, and out of the model's context.

    An allowlist rather than free rein. Not because the agent could not
    be given the whole disk -- it is one line -- but because "it can only
    touch the folders you nominated" is a claim that holds even when the
    model misbehaves, and `write_file` overwrites without warning.
    """
    workspaces = {
        "sandbox": SANDBOX_DIR,
        "inbox": INBOX_DIR,
        "outputs": OUTPUTS_DIR,
    }

    for entry in os.getenv("WORKSPACES", "").split(";"):
        entry = entry.strip()
        if not entry or "=" not in entry:
            continue
        name, _, raw_path = entry.partition("=")
        name = name.strip().lower()
        path = Path(raw_path.strip()).expanduser()
        if name and name not in ("sandbox", "inbox", "outputs") and path.is_dir():
            workspaces[name] = path.resolve()

    return workspaces


WORKSPACES = _parse_workspaces()

# Generated at runtime, gitignored. The checkpoint database is what makes
# the approval gates durable: the graph writes its whole state here when it
# pauses, so a run can be resumed minutes later or after a restart.
DATA_DIR = PROJECT_ROOT / "data"
CHECKPOINT_DB = DATA_DIR / "checkpoints.sqlite"

# The stand-in for a company's internal system. A separate database from
# the checkpoints on purpose: one is the agent's own memory of a run, the
# other is business data the agent is acting on. Mixing them would make
# "clear the agent's state" also mean "delete the company's records".
LEDGER_DB = DATA_DIR / "ledger.sqlite"

# ---------------------------------------------------------------- secrets
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY", "").strip()

# ----------------------------------------------------------------- models
# Google's model IDs change often, so these are configuration, not
# constants. Run `scripts/smoke_test_llm.py --list` to see which IDs this
# key can actually use, then pin exact ones in .env.
# PLANNER_MODEL: the careful, expensive model (planning, reflection).
# FAST_MODEL:    the cheap model (verification, small classifications).
PLANNER_MODEL = os.getenv("PLANNER_MODEL", "gemini-pro-latest").strip()
FAST_MODEL = os.getenv("FAST_MODEL", "gemini-flash-latest").strip()

# ----------------------------------------------------------------- limits
LLM_MAX_RETRIES = int(os.getenv("LLM_MAX_RETRIES", "3"))

# Output token ceiling, set explicitly so a long plan has room. Note: a
# truncation theory for the "output did not match Plan" failures turned
# out to be wrong -- finish_reason was STOP and the JSON was complete.
# The real cause was an unquoted placeholder, handled in schemas.py. This
# stays as cheap insurance against real truncation on longer plans.
MAX_OUTPUT_TOKENS = int(os.getenv("MAX_OUTPUT_TOKENS", "8192"))

# A hard ceiling on how many steps one run may execute. Guards against the
# classic agent failure: looping forever while spending money.
MAX_STEPS = int(os.getenv("MAX_STEPS", "25"))

# How many times one step may be attempted, including the first try. 2 means
# one repair. Tools report *why* they failed ("not UTF-8 text, use read_pdf"),
# so the first repair fixes most things; a step still failing after being
# shown its own error usually needs a different plan, not another go.
MAX_STEP_ATTEMPTS = int(os.getenv("MAX_STEP_ATTEMPTS", "2"))

# How many times a run may throw away its plan and think again. 1, because
# the second plan is written knowing what went wrong; a third would mostly
# be the model circling. Every re-plan goes back to the human for approval,
# so this is also a limit on how often the agent may interrupt you.
MAX_REPLANS = int(os.getenv("MAX_REPLANS", "1"))


def ensure_dirs() -> None:
    """Create runtime directories. data/ is gitignored, so on a fresh
    clone it does not exist and the checkpointer would fail to open."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SANDBOX_DIR.mkdir(parents=True, exist_ok=True)
    INBOX_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)


def require_api_key() -> str:
    """Return the API key, or fail with an actionable message.

    Failing here, at the boundary, beats a confusing 401 surfacing from
    deep inside the SDK with no indication of what to do about it.
    """
    if not GOOGLE_API_KEY:
        raise RuntimeError(
            "GOOGLE_API_KEY is not set.\n"
            f"  1. Create {PROJECT_ROOT / '.env'}\n"
            "  2. Add the line: GOOGLE_API_KEY=your_key_here\n"
            "     (get a key at https://aistudio.google.com/apikey)\n"
        )
    return GOOGLE_API_KEY
