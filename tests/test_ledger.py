"""The ledger: business rules that must hold whatever the model says.

The duplicate rule and the date rule are the two that matter. Both exist
because an agent retries, and a retry must not corrupt records.
"""

from __future__ import annotations

import pytest

ACME = ("ACME CORPORATION", "INV-2026-0417", "2026-09-28", 2540.50, "USD")


class TestFiling:
    def test_an_invoice_can_be_read_back(self, ledger):
        ledger.add_invoice(*ACME)
        record = ledger.find_invoice("INV-2026-0417")
        assert record["vendor"] == "ACME CORPORATION"
        assert record["total_amount"] == 2540.50
        assert record["currency"] == "USD"

    def test_confirmation_names_what_was_filed(self, ledger):
        message = ledger.add_invoice(*ACME)
        assert "INV-2026-0417" in message
        assert "2,540.50" in message

    def test_values_are_normalised(self, ledger):
        ledger.add_invoice("  Globex Ltd  ", " GLX-77 ", "2026-10-01", 980.0, "inr")
        record = ledger.find_invoice("GLX-77")
        assert record["vendor"] == "Globex Ltd"
        assert record["currency"] == "INR"


class TestDuplicates:
    def test_the_same_invoice_cannot_be_filed_twice(self, ledger):
        # An agent retries. Without this, a retry after a timeout would
        # quietly create a second payment record.
        ledger.add_invoice(*ACME)
        with pytest.raises(ledger.LedgerError, match="already in the ledger"):
            ledger.add_invoice(*ACME)

    def test_the_refusal_shows_the_existing_record(self, ledger):
        # So the agent can report "already filed, here it is" instead of
        # retrying the same insert.
        ledger.add_invoice(*ACME)
        with pytest.raises(ledger.LedgerError, match="ACME CORPORATION"):
            ledger.add_invoice(*ACME)

    def test_a_failed_duplicate_does_not_add_a_row(self, ledger):
        ledger.add_invoice(*ACME)
        with pytest.raises(ledger.LedgerError):
            ledger.add_invoice(*ACME)
        assert len(ledger.list_invoices()) == 1


class TestDateFormat:
    @pytest.mark.parametrize(
        "ambiguous",
        ["02/10/2026", "10-02-2026", "2 October 2026", "2026/10/02", ""],
    )
    def test_non_iso_dates_are_refused(self, ledger, ambiguous):
        # '02/10/2026' is 2 October or 10 February depending on where the
        # document came from. A tool that guessed would file wrong records
        # that look perfectly fine.
        with pytest.raises(ledger.LedgerError, match="ISO format"):
            ledger.add_invoice("X", "N-1", ambiguous, 10.0)

    def test_impossible_dates_are_refused(self, ledger):
        with pytest.raises(ledger.LedgerError, match="ISO format"):
            ledger.add_invoice("X", "N-2", "2026-13-45", 10.0)

    def test_the_error_explains_the_ambiguity(self, ledger):
        # The message is what the repair ladder reads, so it has to say
        # what to do, not just that something is wrong.
        with pytest.raises(ledger.LedgerError, match="2026-10-02"):
            ledger.add_invoice("X", "N-3", "02/10/2026", 10.0)

    def test_iso_is_accepted(self, ledger):
        ledger.add_invoice("X", "N-4", "2026-10-02", 10.0)
        assert ledger.find_invoice("N-4")["invoice_date"] == "2026-10-02"


class TestBadInput:
    @pytest.mark.parametrize("amount", [0.0, -5.0, -0.01])
    def test_amount_must_be_positive(self, ledger, amount):
        with pytest.raises(ledger.LedgerError, match="must be positive"):
            ledger.add_invoice("X", "N-5", "2026-01-01", amount)

    @pytest.mark.parametrize("number", ["", "   "])
    def test_invoice_number_cannot_be_blank(self, ledger, number):
        with pytest.raises(ledger.LedgerError, match="cannot be empty"):
            ledger.add_invoice("X", number, "2026-01-01", 10.0)


class TestLookup:
    def test_missing_invoice_returns_a_message_not_an_error(self, ledger):
        # The agent uses this to check whether something is already filed,
        # so "not there" is an answer, not a failure.
        assert "No invoice" in ledger.find_invoice("NOPE-1")

    def test_listing_is_newest_first(self, ledger):
        ledger.add_invoice("A Ltd", "A-1", "2026-01-01", 1.0)
        ledger.add_invoice("B Ltd", "B-1", "2026-01-02", 2.0)
        assert [r["invoice_number"] for r in ledger.list_invoices()] == ["B-1", "A-1"]

    def test_vendor_filter_is_partial_and_case_insensitive(self, ledger):
        ledger.add_invoice("ACME CORPORATION", "A-1", "2026-01-01", 1.0)
        ledger.add_invoice("Globex Ltd", "G-1", "2026-01-02", 2.0)
        assert [r["invoice_number"] for r in ledger.list_invoices("acme")] == ["A-1"]

    def test_empty_filter_lists_everything(self, ledger):
        ledger.add_invoice("A Ltd", "A-1", "2026-01-01", 1.0)
        ledger.add_invoice("B Ltd", "B-1", "2026-01-02", 2.0)
        assert len(ledger.list_invoices("")) == 2
