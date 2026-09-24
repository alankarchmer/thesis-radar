from datetime import date

import pytest

from thesis_radar.numbers import (
    BPS,
    COUNT,
    CURRENCY,
    DAYS,
    MULTIPLE,
    PERCENT,
    find_mentions,
    find_periods,
    last_completed_quarter,
    period_granularity,
    period_sort_key,
    to_unit,
    unit_kinds,
)

RELEASE = (
    "Revenue was $1.92 billion, up 4%. We now expect full-year gross margin of 20% to 21%, compared with prior "
    "guidance of 21% to 22%. North American dealer inventory was up 22% year over year at the end of the third "
    "quarter, about 41,000 units, or 112 days of supply. Freight was a 40 basis point tailwind. We expect fiscal "
    "2027 EPS of $7.0 to $7.4. The stock trades at 14x. In 2025 we sold 3 new models."
)


def test_mentions_carry_kind_value_range_and_sentence():
    mentions = find_mentions(RELEASE)
    found = [(m.text, m.kind, m.value, m.high) for m in mentions]
    assert found == [
        ("$1.92 billion", CURRENCY, 1.92e9, None),
        ("4%", PERCENT, 4.0, None),
        ("20% to 21%", PERCENT, 20.0, 21.0),
        ("21% to 22%", PERCENT, 21.0, 22.0),
        ("22%", PERCENT, 22.0, None),
        ("41,000 units", COUNT, 41000.0, None),
        ("112 days", DAYS, 112.0, None),
        ("40 basis point", BPS, 40.0, None),
        ("$7.0 to $7.4", CURRENCY, 7.0, 7.4),
        ("14x", MULTIPLE, 14.0, None),
    ]
    assert [m.id for m in mentions][:2] == ["m1", "m2"]
    assert mentions[2].sentence.startswith("We now expect full-year gross margin")


@pytest.mark.parametrize(
    "text, expected",
    [
        ("sales of $500 million", [(CURRENCY, 5e8, None)]),
        ("$7.0 to $7.4 billion", [(CURRENCY, 7e9, 7.4e9)]),
        ("a $18,999 price", [(CURRENCY, 18999.0, None)]),
        ("margin of 21.5 percent", [(PERCENT, 21.5, None)]),
        ("down 3-4%", [(PERCENT, 3.0, 4.0)]),
        ("200 bps", [(BPS, 200.0, None)]),
        ("in 2026 and 2027 revenue", []),
        ("we opened 5 stores", []),
    ],
)
def test_mention_edge_cases(text, expected):
    assert [(m.kind, m.value, m.high) for m in find_mentions(text)] == expected


def test_units_and_conversion():
    assert unit_kinds("%") == {PERCENT} and unit_kinds("$M") == {CURRENCY} and unit_kinds("days") == {DAYS}
    assert unit_kinds("pp") == {BPS, PERCENT} and unit_kinds("sleds") == {COUNT} and unit_kinds("x") == {MULTIPLE}
    [billions] = find_mentions("$1.92 billion")
    assert to_unit(billions, "$M") == (1920.0, None) and to_unit(billions, "$B") == (1.92, None)
    [bps] = find_mentions("40 bps")
    assert to_unit(bps, "pp") == (0.4, None) and to_unit(bps, "bps") == (40.0, None)


def test_periods_resolve_against_the_document_date():
    text = (RELEASE + " Q4 guidance; 3Q26 results; second half of 2026; in September; H1 2027; the back half; "
            "may improve; FY27 outlook")
    periods = find_periods(text, date(2026, 10, 20))
    assert [(p.key, p.label) for p in periods] == [
        ("FY2026", "FY 2026"), ("2026-Q3", "Q3 2026"), ("FY2027", "FY 2027"), ("2026-Q4", "Q4 2026"),
        ("2026-H2", "H2 2026"), ("2026-09", "Sep 2026"), ("2027-H1", "H1 2027"),
    ]
    assert periods[1].written == "third quarter"
    # Without a year, the instance nearest the document date wins: a January call's "fourth quarter" is last year's.
    assert find_periods("fourth quarter results", date(2026, 1, 25))[0].key == "2025-Q4"
    assert find_periods("fourth quarter outlook", date(2026, 10, 20))[0].key == "2026-Q4"


def test_period_helpers():
    assert last_completed_quarter(date(2026, 10, 20)).key == "2026-Q3"
    assert last_completed_quarter(date(2026, 1, 25)).key == "2025-Q4"
    keys = ["FY2026", "2026-Q4", "2026-H2", "2026-Q3", "2026-09", "2027-Q1"]
    assert sorted(keys, key=period_sort_key) == ["2026-09", "2026-Q3", "2026-Q4", "2026-H2", "FY2026", "2027-Q1"]
    assert [period_granularity(k) for k in ("FY2026", "2026-Q3", "2026-H2", "2026-09")] == ["year", "quarter", "half", "month"]


def test_calendar_dates_are_not_reporting_periods():
    text = "ACME preview September 18, 2026 We estimate third quarter gross margin of 21.0%. Sept. 3rd call."
    assert [p.key for p in find_periods(text, date(2026, 9, 18))] == ["2026-Q3"]
    assert [p.key for p in find_periods("sales in September 2026 rose", date(2026, 10, 5))] == ["2026-09"]
