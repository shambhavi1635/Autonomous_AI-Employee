"""Shared fixtures.

Every test in this suite runs offline. No API key, no network, no model
calls. That is a deliberate property, not an accident of what was easy to
test: a suite that needs credentials gets skipped, and a suite that costs
money per run gets run less often than it should.

What that leaves out is the model's judgement -- whether it picks a
sensible tool, or extracts the right total. Those are checked by hand
against the sample invoices. What these tests cover is everything that
must hold regardless of what the model says: validation, the sandbox
boundary, risk enforcement, business rules.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Point the file tools at a throwaway directory.

    Tests must never touch the real sandbox/ -- a test that deletes a
    sample invoice would be a test that breaks the demo.
    """
    import tools.files

    box = tmp_path / "sandbox"
    box.mkdir()
    monkeypatch.setattr(tools.files, "SANDBOX_DIR", box)
    return box


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    """Point the ledger at a throwaway database."""
    import tools.ledger

    monkeypatch.setattr(tools.ledger, "LEDGER_DB", tmp_path / "ledger.sqlite")
    monkeypatch.setattr(tools.ledger, "ensure_dirs", lambda: None)
    return tools.ledger


@pytest.fixture
def clean_registry(monkeypatch):
    """Give a test its own empty tool registry.

    The registry is module-level state populated at import time. Without
    isolation, a test registering a tool would leak into every later test
    and the order tests ran in would change the result.
    """
    from tools import registry

    monkeypatch.setattr(registry, "_REGISTRY", {})
    return registry
