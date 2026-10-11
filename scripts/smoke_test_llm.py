"""Phase 0 smoke test: prove we can talk to the model before building on it.

Usage (from the project root):

    venv/python.exe scripts/smoke_test_llm.py --list    # list usable model IDs
    venv/python.exe scripts/smoke_test_llm.py           # run all checks

A smoke test is not a unit test. It makes real API calls and answers one
question: "is the foundation alive?" We run it now so that when the planner
misbehaves in Phase 1, we already know the transport layer is sound.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Allow running this script directly from the project root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pydantic import BaseModel, Field  # noqa: E402

from core import config  # noqa: E402
from llm.client import LLMClient, LLMError  # noqa: E402


class InvoiceFields(BaseModel):
    """A deliberately invoice-shaped schema.

    Testing structured output on a realistic target tells us something the
    "return a number" test cannot: whether the model reliably fills the kind
    of nested, typed record the invoice -> ledger task will depend on.
    """

    vendor: str = Field(description="Company that issued the invoice")
    invoice_number: str
    total_amount: float
    currency: str = Field(description="Three-letter code, e.g. USD")
    line_item_count: int


SAMPLE_INVOICE = """
ACME CORPORATION
Invoice #: INV-2026-0417
Date: 2026-09-28

Description                 Qty    Rate      Amount
Consulting services          12    150.00    1800.00
Cloud hosting (Sept)          1    240.50     240.50
Support retainer              1    500.00     500.00

                                   TOTAL:   USD 2540.50
"""


def check_models(client: LLMClient) -> list[str]:
    print("\n[1/3] Listing models available to this API key...")
    models = client.list_models()
    for name in models:
        print(f"      - {name}")
    print(f"      {len(models)} model(s) support generateContent.")

    for label, configured in (
        ("PLANNER_MODEL", config.PLANNER_MODEL),
        ("FAST_MODEL", config.FAST_MODEL),
    ):
        status = "OK" if configured in models else "NOT FOUND -- fix in .env"
        print(f"      {label} = {configured}  [{status}]")
    return models


def check_text(client: LLMClient) -> None:
    print("\n[2/3] Plain text generation...")
    reply = client.generate_text(
        "Reply with exactly the word: READY",
        system="You are a terse test harness. Obey literally.",
        model=config.FAST_MODEL,
    )
    print(f"      model said: {reply!r}")


def check_structured(client: LLMClient) -> None:
    print("\n[3/3] Structured extraction (the capability the agent depends on)...")
    result = client.generate_structured(
        f"Extract the invoice fields from this document:\n{SAMPLE_INVOICE}",
        InvoiceFields,
        system="You extract structured data from business documents.",
    )
    print(f"      parsed -> {result.model_dump_json(indent=2)}")

    # Assert on the facts we can check, so a plausible-but-wrong extraction
    # fails the test instead of looking like a pass.
    assert result.total_amount == 2540.50, f"wrong total: {result.total_amount}"
    assert result.line_item_count == 3, f"wrong count: {result.line_item_count}"
    assert result.currency.upper() == "USD", f"wrong currency: {result.currency}"
    print("      assertions passed (total, line count, currency)")


def main() -> int:
    try:
        client = LLMClient()
    except RuntimeError as exc:
        print(f"\nFAILED: {exc}")
        return 1

    try:
        if "--list" in sys.argv:
            check_models(client)
            return 0

        check_models(client)
        check_text(client)
        check_structured(client)
    except (LLMError, AssertionError) as exc:
        print(f"\nFAILED: {exc}")
        return 1

    u = client.usage
    print(
        f"\nAll checks passed. "
        f"{u.calls} API calls, {u.prompt_tokens} in / {u.output_tokens} out tokens."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
