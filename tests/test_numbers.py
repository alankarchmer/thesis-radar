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
        ("down 3-4%", [(PERCENT, -4.0, -3.0)]),
        ("200 bps", [(BPS, 200.0, None)]),
        ("in 2026 and 2027 revenue", []),
        ("we opened 5 stores", []),
    ],
)
def test_mention_edge_cases(text, expected):
    assert [(m.kind, m.value, m.high) for m in find_mentions(text)] == expected


@pytest.mark.parametrize(
    "text, expected",
    [
        # Minus signs, attached: hyphen, the minus sign, and an en dash as PDFs set it.
        ("gross margin was -5% in the quarter", [("-5%", -5.0, None)]),
        ("gross margin was −5.2%", [("−5.2%", -5.2, None)]),
        ("sales of 1,850 million, –4.5% y/y", [("–4.5%", -4.5, None)]),
        ("operating income was -$10 million", [("-$10 million", -1e7, None)]),
        ("operating income was $-10 million", [("$-10 million", -1e7, None)]),
        ("guidance of -1% to 1%", [("-1% to 1%", -1.0, 1.0)]),
        ("guidance of −3% to −1%", [("−3% to −1%", -3.0, -1.0)]),
        # Accounting parentheses around the number alone.
        ("operating income was $(10.5) million", [("$(10.5) million", -1.05e7, None)]),
        ("change in net sales (8)%", [("(8)%", -8.0, None)]),
        ("margin contracted (120) bps", [("(120) bps", -120.0, None)]),
        # Parentheses around number and unit: negative in a table, an aside in prose.
        ("| Net income | 12.1 | ($10.0 million) |", [("($10.0 million)", -1e7, None)]),
        ("Gross margin 20.6% 21.8% (1.2%)", [("20.6%", 20.6, None), ("21.8%", 21.8, None), ("(1.2%)", -1.2, None)]),
        ("our largest markets are Europe (25%) and Asia (12%)", [("25%", 25.0, None), ("12%", 12.0, None)]),
        ("a one-time charge ($3 million)", [("$3 million", 3e6, None)]),
        # Words of decline next to the number; a level after "to" or "from" stays positive.
        ("retail sales declined 6%, while off-road was flat", [("6%", -6.0, None)]),
        ("revenue was down by approximately $120 million", [("$120 million", -1.2e8, None)]),
        ("margin was 19.8%, down 240 basis points", [("19.8%", 19.8, None), ("240 basis points", -240.0, None)]),
        ("an 8% decline in shipments and 5% lower pricing", [("8%", -8.0, None), ("5%", -5.0, None)]),
        ("dealers took 1,200 fewer units", [("1,200 fewer", -1200.0, None)]),
        ("gross margin fell to 20.6% from 21.8%", [("20.6%", 20.6, None), ("21.8%", 21.8, None)]),
        ("up 12%, and revenue rose 5%", [("12%", 12.0, None), ("5%", 5.0, None)]),
        # Hyphens that are not signs.
        ("costs of $5 million in Q3-5% of sales", [("$5 million", 5e6, None), ("5%", 5.0, None)]),
        ("growth of 5-7% on 5% lower-priced models", [("5-7%", 5.0, 7.0), ("5%", 5.0, None)]),
    ],
)
def test_negative_numbers(text, expected):
    assert [(m.text, m.value, m.high) for m in find_mentions(text)] == expected


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
