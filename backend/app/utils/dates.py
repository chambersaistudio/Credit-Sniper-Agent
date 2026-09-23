"""
One parser for the date strings credit reports actually contain
("03/2015", "03/15/2015", "March 2015", "Mar 15, 2015", "2015-03").
Month-only values resolve to the 1st. Returns None instead of guessing
on anything ambiguous or unparseable.
"""
import re
from datetime import date, datetime

_FORMATS = (
    "%m/%d/%Y", "%m-%d-%Y", "%m/%d/%y",
    "%m/%Y", "%m-%Y",
    "%Y-%m-%d", "%Y-%m",
    "%B %Y", "%b %Y",
    "%B %d, %Y", "%b %d, %Y", "%B %d %Y", "%b %d %Y",
)


def parse_report_date(value: str | None) -> date | None:
    if not value:
        return None
    cleaned = re.sub(r"\s+", " ", value.strip().rstrip("."))
    for fmt in _FORMATS:
        try:
            return datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
    return None
