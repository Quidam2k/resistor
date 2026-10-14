"""Federal business-day arithmetic for FOIA deadlines."""

from datetime import date, timedelta
from functools import lru_cache

_DAY = timedelta(days=1)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))


def _observed_date(d: date) -> date:
    if d.weekday() == 5:
        return d - _DAY
    if d.weekday() == 6:
        return d + _DAY
    return d


@lru_cache(maxsize=None)
def _holiday_map(year: int, include_inauguration: bool) -> dict[date, str]:
    holidays: dict[date, str] = {}

    def add_fixed(month: int, day: int, name: str) -> None:
        actual = date(year, month, day)
        observed = _observed_date(actual)
        if observed.year == year:
            holidays[observed] = name + (" (observed)" if observed != actual else "")

    add_fixed(1, 1, "New Year's Day")
    holidays[_nth_weekday(year, 1, 0, 3)] = "Birthday of Martin Luther King, Jr."
    holidays[_nth_weekday(year, 2, 0, 3)] = "Washington's Birthday"
    last_may = date(year, 5, 31)
    holidays[last_may - timedelta(days=last_may.weekday())] = "Memorial Day"
    if year >= 2021:
        add_fixed(6, 19, "Juneteenth National Independence Day")
    add_fixed(7, 4, "Independence Day")
    holidays[_nth_weekday(year, 9, 0, 1)] = "Labor Day"
    holidays[_nth_weekday(year, 10, 0, 2)] = "Columbus Day"
    add_fixed(11, 11, "Veterans Day")
    holidays[_nth_weekday(year, 11, 3, 4)] = "Thanksgiving Day"
    add_fixed(12, 25, "Christmas Day")

    # Next year's New Year's Day can be observed in this year.
    if year < date.max.year and date(year + 1, 1, 1).weekday() == 5:
        holidays[date(year, 12, 31)] = "New Year's Day (observed)"

    if include_inauguration and (year - 1) % 4 == 0:
        inauguration = date(year, 1, 20)
        name = "Inauguration Day"
        if inauguration.weekday() == 6:
            inauguration += _DAY
            name += " (observed)"
        if inauguration in holidays:
            holidays[inauguration] += " / " + name
        else:
            holidays[inauguration] = name

    return holidays


def federal_holidays(
    year: int, include_inauguration: bool = True
) -> dict[date, str]:
    """Return observed holidays within the given calendar year."""
    return _holiday_map(year, include_inauguration).copy()


def is_business_day(d: date, include_inauguration: bool = True) -> bool:
    """Return whether the date is a federal business day."""
    return d.weekday() < 5 and d not in _holiday_map(d.year, include_inauguration)


def add_business_days(
    start: date, n: int, include_inauguration: bool = True
) -> date:
    """Add business days, excluding the start date."""
    if n < 0:
        raise ValueError("n must be nonnegative")
    current = start
    while n:
        current += _DAY
        if is_business_day(current, include_inauguration):
            n -= 1
    return current


def business_days_between(
    start: date, end: date, include_inauguration: bool = True
) -> int:
    """Count business days after start through end, signed for reverse ranges."""
    if end < start:
        return -business_days_between(end, start, include_inauguration)
    current = start
    count = 0
    while current < end:
        current += _DAY
        if is_business_day(current, include_inauguration):
            count += 1
    return count


def next_business_day(d: date, include_inauguration: bool = True) -> date:
    """Return the date itself or the following business day."""
    while not is_business_day(d, include_inauguration):
        d += _DAY
    return d
