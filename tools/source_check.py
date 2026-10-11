"""Checking recorded values against the document they came from.

This exists because of a specific failure. An invoice dated 10/07/2026
"(US format)" was filed as 2026-07-10 -- July instead of October -- and
every check passed. The step verified, the goal check passed, because
the postcondition asked whether a record existed and it did.

Three prompt rules were tried: postconditions must be checkable,
postconditions must name the entity, postconditions must compare values.
Each improved the wording of the postcondition. None made the judge
actually compare, because an LLM asked "does this match?" about work it
just did tends to agree with itself.

So this does it in Python. The functions here cannot be talked round,
do not cost a model call, and give the same answer every time.

The date logic is the interesting part. An ambiguous date is *refused*
rather than guessed: 10/07/2026 is either 10 July or 7 October, and an
accounting system should not pick one on a hunch. The refusal names both
readings and says what to look for, which is exactly what the repair
ladder needs to fix it on the next attempt.
"""

from __future__ import annotations

import re

_MONTHS = {
    form: number
    for number, name in enumerate(
        ["january", "february", "march", "april", "may", "june", "july",
         "august", "september", "october", "november", "december"],
        start=1,
    )
    for form in (name, name[:3])
}

# Evidence about how a document writes dates, in two strengths.
#
# Strong: the document says so outright. Weak: a country or city that
# suggests a convention. Strength matters because they conflict -- the
# invoice that prompted all this is addressed in Manchester and then
# states "(US format)" explicitly. Weighing those equally cancels them
# out and refuses a date the document had actually spelled out.
_STRONG_DAY_FIRST = ("dd/mm", "dd-mm", "day first", "uk format", "eu format")
_STRONG_MONTH_FIRST = ("us format", "u.s. format", "mm/dd", "month first")

_WEAK_DAY_FIRST = (
    "gstin", "bengaluru", "mumbai", "delhi", "india", "united kingdom",
    "london", "manchester", "gmbh", "europe",
)
_WEAK_MONTH_FIRST = ("usa", "united states")


def _format_hint(text: str) -> str | None:
    """'day', 'month', or None -- how this document writes numeric dates."""
    if any(h in text for h in _STRONG_MONTH_FIRST):
        return "month"
    if any(h in text for h in _STRONG_DAY_FIRST):
        return "day"

    weak_day = any(h in text for h in _WEAK_DAY_FIRST)
    weak_month = any(h in text for h in _WEAK_MONTH_FIRST)
    if weak_day and not weak_month:
        return "day"
    if weak_month and not weak_day:
        return "month"
    return None


class SourceMismatch(ValueError):
    """A value is not supported by the document it supposedly came from."""


def dates_in(text: str) -> tuple[set[str], list[tuple[str, str, str]]]:
    """Every date the text could be stating.

    Returns (unambiguous ISO dates, ambiguous numeric ones). Each
    ambiguous entry is (as written, day-first reading, month-first
    reading).
    """
    certain: set[str] = set()
    ambiguous: list[tuple[str, str, str]] = []

    for year, month, day in re.findall(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", text):
        certain.add(f"{int(year):04d}-{int(month):02d}-{int(day):02d}")

    for first_raw, second_raw, year_raw in re.findall(
        r"\b(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})\b", text
    ):
        first, second, year = int(first_raw), int(second_raw), int(year_raw)
        day_first = f"{year:04d}-{second:02d}-{first:02d}" if second <= 12 else None
        month_first = f"{year:04d}-{first:02d}-{second:02d}" if first <= 12 else None

        if day_first and month_first and day_first != month_first:
            ambiguous.append((f"{first_raw}/{second_raw}/{year_raw}",
                              day_first, month_first))
        elif day_first or month_first:
            certain.add(day_first or month_first)

    for day, name, year in re.findall(
        r"\b(\d{1,2})\s+([A-Za-z]{3,9})\.?,?\s+(\d{4})\b", text
    ):
        if name.lower() in _MONTHS:
            certain.add(f"{int(year):04d}-{_MONTHS[name.lower()]:02d}-{int(day):02d}")

    for name, day, year in re.findall(
        r"\b([A-Za-z]{3,9})\.?\s+(\d{1,2}),?\s+(\d{4})\b", text
    ):
        if name.lower() in _MONTHS:
            certain.add(f"{int(year):04d}-{_MONTHS[name.lower()]:02d}-{int(day):02d}")

    return certain, ambiguous


def numbers_in(text: str) -> set[float]:
    """Every number the text states, read both ways.

    An invoice from Germany writes 1.875,60 where one from India writes
    98,400.00. Both readings of each token are kept, because guessing
    the convention here would reintroduce the problem this module exists
    to solve.
    """
    found: set[float] = set()
    for raw in re.findall(r"\d[\d.,]*\d|\d", text):
        anglo = raw.replace(",", "")
        european = raw.replace(".", "").replace(",", ".")
        for candidate in (anglo, european):
            try:
                found.add(float(candidate))
            except ValueError:
                continue
    return found


def check_text(field: str, value: str, source: str) -> None:
    """Refuse a string that does not appear in the source document."""
    if not value.strip():
        return
    # Both sides are whitespace-normalised. PDF extraction collapses and
    # inserts spacing unpredictably, so comparing raw strings fails on
    # values that are plainly the same text.
    needle = " ".join(value.split()).lower()
    haystack = " ".join(source.split()).lower()
    if needle not in haystack:
        raise SourceMismatch(
            f"{field} {value!r} does not appear in the source document. "
            "Copy it exactly as the document writes it."
        )


def check_amount(value: float, source: str) -> None:
    """Refuse an amount the document never states."""
    stated = numbers_in(" ".join(source.split()))
    if stated and not any(abs(value - number) < 0.01 for number in stated):
        raise SourceMismatch(
            f"total_amount {value} does not appear anywhere in the source "
            "document. Use a figure the document actually states."
        )


def check_date(value: str, source: str) -> None:
    """Refuse a date the document does not support, or cannot decide.

    Passing silently when the document contains no date at all is
    deliberate: this is a corroboration check, and there is nothing to
    corroborate against.
    """
    flat = " ".join(source.split())
    lowered = flat.lower()
    certain, ambiguous = dates_in(flat)

    if value in certain:
        return

    for written, day_first, month_first in ambiguous:
        if value not in (day_first, month_first):
            continue

        hint = _format_hint(lowered)

        if hint == "month":
            if value == month_first:
                return
            raise SourceMismatch(
                f"The document writes the date as {written} and indicates "
                f"month-first format, so it means {month_first}, not {value}."
            )
        if hint == "day":
            if value == day_first:
                return
            raise SourceMismatch(
                f"The document writes the date as {written} and appears to use "
                f"day-first format, so it means {day_first}, not {value}."
            )

        raise SourceMismatch(
            f"The date {written} in this document is ambiguous: it is either "
            f"{day_first} (day first) or {month_first} (month first), and "
            f"{value} assumes one of them. Re-read the document for a clue "
            "about where it came from -- a country, address, currency or a "
            "note about the format -- and use that reading."
        )

    if certain or ambiguous:
        known = ", ".join(sorted(certain)) or "none unambiguous"
        raise SourceMismatch(
            f"invoice_date {value!r} is not a date this document states. "
            f"Dates found: {known}."
        )
