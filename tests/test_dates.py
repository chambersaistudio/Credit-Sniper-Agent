from datetime import date

import pytest

from app.utils.dates import parse_report_date


@pytest.mark.parametrize("raw, expected", [
    ("03/2015", date(2015, 3, 1)),
    ("03/15/2015", date(2015, 3, 15)),
    ("March 2015", date(2015, 3, 1)),
    ("Mar 15, 2015", date(2015, 3, 15)),
    ("2015-03", date(2015, 3, 1)),
    ("  03/2015 ", date(2015, 3, 1)),
])
def test_parses_report_formats(raw, expected):
    assert parse_report_date(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "N/A", "13/2015", "sometime"])
def test_unparseable_is_none(raw):
    assert parse_report_date(raw) is None

