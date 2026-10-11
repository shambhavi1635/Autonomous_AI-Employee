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

# A hard ceiling on how many steps one run may execute. Guards against the
# classic agent failure: looping forever while spending money.
MAX_STEPS = int(os.getenv("MAX_STEPS", "25"))

# How many times one step may be attempted, including the first try. 2 means
# one repair. Tools report *why* they failed ("not UTF-8 text, use read_pdf"),
# so the first repair fixes most things; a step still failing after being
# shown its own error usually needs a different plan, not another go.
MAX_STEP_ATTEMPTS = int(os.getenv("MAX_STEP_ATTEMPTS", "2"))


def ensure_dirs() -> None:
    """Create runtime directories. data/ is gitignored, so on a fresh
    clone it does not exist and the checkpointer would fail to open."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SANDBOX_DIR.mkdir(parents=True, exist_ok=True)


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
