"""Verifying recorded values against their source document.

These tests exist because of one bug: an invoice dated 10/07/2026 "(US
format)" was filed as 2026-07-10 -- July instead of October -- and every
check passed, because each check asked whether a record existed rather
than whether it was right. Three prompt rules failed to fix it. This is
the deterministic version, and these are its regression tests.
"""

from __future__ import annotations

import pytest

from tools.source_check import (
    SourceMismatch,
    check_amount,
    check_date,
    check_text,
    dates_in,
    numbers_in,
)

UMBRELLA = (
    "UMBRELLA SUPPLIES Unit 7, Dockside Estate, Manchester "
    "Doc: DOC-55120 Raised: 10/07/2026 (US format) "
    "Account: NW-0042 Credit limit: GBP 25,000.00 Net payable GBP 640.00"
)
GLOBEX = (
    "Globex Ltd Plot 44, Whitefield, Bengaluru 560066 GSTIN: 29AABCG1234F1Z5 "
    "Invoice No. GLX/2026/118 Invoice Date 02/10/2026 "
    "Subtotal 83389.83 GST @ 18% 15010.17 Amount Due (INR) 98,400.00"
)


class TestTheOriginalBug:
    def test_the_wrong_reading_is_refused(self):
        with pytest.raises(SourceMismatch, match="month-first"):
            check_date("2026-07-10", UMBRELLA)

    def test_the_right_reading_is_accepted(self):
        check_date("2026-10-07", UMBRELLA)

    def test_an_explicit_format_note_beats_a_city_name(self):
        # Manchester suggests day-first; "(US format)" says otherwise.
        # Weighing them equally cancels them out and refuses a date the
        # document had spelled out.
        check_date("2026-10-07", UMBRELLA)

    def test_country_is_used_when_nothing_explicit_is_said(self):
        check_date("2026-10-02", GLOBEX)          # GSTIN, Bengaluru -> day first
        with pytest.raises(SourceMismatch, match="day-first"):
            check_date("2026-02-10", GLOBEX)


class TestAmbiguityIsRefusedNotGuessed:
    PLAIN = "Invoice 123 dated 05/06/2026 for 100.00"

    def test_neither_reading_is_accepted_without_evidence(self):
        for value in ("2026-06-05", "2026-05-06"):
            with pytest.raises(SourceMismatch, match="ambiguous"):
                check_date(value, self.PLAIN)

    def test_the_refusal_names_both_readings(self):
        with pytest.raises(SourceMismatch, match="2026-06-05.*2026-05-06"):
            check_date("2026-06-05", self.PLAIN)

    def test_an_unambiguous_numeric_date_needs_no_hint(self):
        check_date("2026-06-25", "dated 25/06/2026")   # 25 cannot be a month


class TestDateParsing:
    @pytest.mark.parametrize(
        "text, expected",
        [
            ("dated 2026-09-28", "2026-09-28"),
            ("Issued: 15 September 2026", "2026-09-15"),
            ("Date: Sep 15, 2026", "2026-09-15"),
            ("on 25/12/2026", "2026-12-25"),
        ],
    )
    def test_unambiguous_forms(self, text, expected):
        certain, _ = dates_in(text)
        assert expected in certain

    def test_ambiguous_forms_are_kept_separate(self):
        certain, ambiguous = dates_in("dated 03/04/2026")
        assert certain == set()
        assert ambiguous == [("03/04/2026", "2026-04-03", "2026-03-04")]


class TestNumbers:
    def test_both_decimal_conventions_are_read(self):
        # 1.875,60 from Germany and 98,400.00 from India mean different
        # things by the same punctuation. Guessing would reintroduce the
        # bug this module exists to prevent.
        assert 1875.60 in numbers_in("Grand Total EUR 1.875,60")
        assert 98400.00 in numbers_in("Amount Due (INR) 98,400.00")

    def test_a_stated_amount_passes(self):
        check_amount(640.00, UMBRELLA)
        check_amount(98400.00, GLOBEX)

    def test_an_invented_amount_is_refused(self):
        with pytest.raises(SourceMismatch, match="does not appear"):
            check_amount(999.99, UMBRELLA)

    def test_a_distractor_figure_still_passes(self):
        # Corroboration, not judgement: the credit limit is in the
        # document, so the check cannot rule it out. Picking the right
        # figure is the agent's job; this only catches invented ones.
        check_amount(25000.00, UMBRELLA)


class TestText:
    def test_a_matching_value_passes(self):
        check_text("vendor", "Umbrella Supplies", UMBRELLA)
        check_text("invoice_number", "DOC-55120", UMBRELLA)

    def test_a_value_not_in_the_document_is_refused(self):
        with pytest.raises(SourceMismatch, match="does not appear"):
            check_text("vendor", "Acme Corporation", UMBRELLA)

    def test_matching_ignores_case_and_spacing(self):
        check_text("vendor", "umbrella   supplies", UMBRELLA)

    def test_an_empty_value_is_not_checked(self):
        check_text("vendor", "", UMBRELLA)


class TestNoSource:
    def test_a_document_with_no_dates_cannot_contradict_one(self):
        # Corroboration against nothing is not a failure.
        check_date("2026-01-01", "This document contains no dates at all.")

    def test_a_document_with_no_numbers_cannot_contradict_one(self):
        check_amount(42.0, "No figures here.")
