"""Federal calendar regression tests with hand-worked expected dates."""

from datetime import date

import pytest

from src.foia.calendar import (
    add_business_days,
    business_days_between,
    federal_holidays,
    is_business_day,
    next_business_day,
)


def test_federal_holidays_returns_date_to_string_dictionary():
    holidays = federal_holidays(2026)

    assert isinstance(holidays, dict)
    assert holidays
    assert all(isinstance(day, date) for day in holidays)
    assert all(isinstance(name, str) and name.strip() for name in holidays.values())


def test_plain_weekend_span():
    # 2026-10-02 is Friday (day 0).
    received = date(2026, 10, 2)
    # Skip October 3 (Saturday) and 4 (Sunday).
    # Count: October 5 (Monday) = business day 1.
    expected = date(2026, 10, 5)

    assert add_business_days(received, 1) == expected
    assert business_days_between(received, expected) == 1


def test_columbus_day_span():
    # 2026-10-09 is Friday (day 0).
    received = date(2026, 10, 9)
    # 2026-10-12 is Monday, Columbus Day.
    holiday = date(2026, 10, 12)
    # Skip October 10 (Saturday), 11 (Sunday), and 12 (holiday).
    # Count: October 13 (Tuesday) = business day 1.
    expected = date(2026, 10, 13)

    assert holiday in federal_holidays(2026)
    assert is_business_day(holiday) is False
    assert add_business_days(received, 1) == expected
    assert business_days_between(received, expected) == 1


def test_full_twenty_business_day_clock():
    # 2026-10-01 is Thursday (day 0).
    received = date(2026, 10, 1)
    # Hand count, excluding weekends and October 12 (Monday, Columbus Day):
    #  1: Oct  2 Fri;  2: Oct  5 Mon;  3: Oct  6 Tue;  4: Oct  7 Wed;
    #  5: Oct  8 Thu;  6: Oct  9 Fri;  7: Oct 13 Tue;  8: Oct 14 Wed;
    #  9: Oct 15 Thu; 10: Oct 16 Fri; 11: Oct 19 Mon; 12: Oct 20 Tue;
    # 13: Oct 21 Wed; 14: Oct 22 Thu; 15: Oct 23 Fri; 16: Oct 26 Mon;
    # 17: Oct 27 Tue; 18: Oct 28 Wed; 19: Oct 29 Thu; 20: Oct 30 Fri.
    # 2026-10-30 is Friday, business day 20.
    expected = date(2026, 10, 30)

    assert add_business_days(received, 20) == expected
    assert business_days_between(received, expected) == 20


def test_saturday_independence_day_is_observed_on_friday():
    # 2026-07-04 is Saturday; observation moves to 2026-07-03, Friday.
    observed = date(2026, 7, 3)

    assert observed in federal_holidays(2026)
    assert is_business_day(observed) is False
    assert is_business_day(date(2026, 7, 4)) is False  # Saturday.


def test_sunday_veterans_day_is_observed_on_monday():
    # 2029-11-11 is Sunday; observation moves to 2029-11-12, Monday.
    observed = date(2029, 11, 12)

    assert observed in federal_holidays(2029)
    assert is_business_day(observed) is False
    assert is_business_day(date(2029, 11, 11)) is False  # Sunday.


def test_next_year_new_year_observation_appears_in_current_year():
    # 2028-01-01 is Saturday; observation is 2027-12-31, Friday.
    observed = date(2027, 12, 31)

    assert observed in federal_holidays(2027)
    assert is_business_day(observed) is False


def test_saturday_inauguration_is_not_shifted():
    # 2029-01-20 is Saturday; Inauguration Day stays on that date.
    inauguration = date(2029, 1, 20)
    # 2029-01-22 is Monday; Saturday inauguration adds no Monday holiday.
    following_monday = date(2029, 1, 22)
    with_inauguration = federal_holidays(2029, include_inauguration=True)
    without_inauguration = federal_holidays(2029, include_inauguration=False)

    assert inauguration in with_inauguration
    assert inauguration not in without_inauguration
    assert following_monday not in with_inauguration
    assert following_monday not in without_inauguration
    assert is_business_day(inauguration) is False
    assert is_business_day(following_monday) is True


def test_2025_inauguration_and_mlk_share_one_holiday_date():
    # 2025-01-20 is Monday, both Inauguration Day and the third Monday (MLK).
    shared_holiday = date(2025, 1, 20)
    with_inauguration = federal_holidays(2025, include_inauguration=True)
    without_inauguration = federal_holidays(2025, include_inauguration=False)

    assert shared_holiday in with_inauguration
    assert shared_holiday in without_inauguration
    # Adding inauguration creates zero additional dates because MLK coincides.
    assert set(with_inauguration) == set(without_inauguration)
    assert list(with_inauguration).count(shared_holiday) == 1
    assert is_business_day(shared_holiday, include_inauguration=True) is False
    assert is_business_day(shared_holiday, include_inauguration=False) is False


def test_2033_inauguration_can_be_included_or_excluded():
    # 2033-01-19 is Wednesday (day 0).
    received = date(2033, 1, 19)
    # 2033-01-20 is Thursday, Inauguration Day.
    inauguration = date(2033, 1, 20)
    # 2033-01-21 is Friday.
    following_day = date(2033, 1, 21)

    assert inauguration in federal_holidays(2033, include_inauguration=True)
    assert inauguration not in federal_holidays(2033, include_inauguration=False)
    assert is_business_day(inauguration, include_inauguration=True) is False
    assert is_business_day(inauguration, include_inauguration=False) is True

    # With inauguration: skip Thursday Jan 20; Friday Jan 21 = day 1.
    assert add_business_days(received, 1) == following_day
    assert (
        add_business_days(received, 1, include_inauguration=True)
        == following_day
    )
    # Without inauguration: Thursday Jan 20 = day 1.
    assert (
        add_business_days(received, 1, include_inauguration=False)
        == inauguration
    )


def test_sunday_inauguration_2041_coincides_with_mlk_observation():
    # 2041-01-01 is Tuesday, so Mondays are Jan 7, 14, and 21.
    # MLK is therefore Jan 21, the third Monday.
    # 2041-01-20 is Sunday; inauguration is observed Monday Jan 21.
    observed = date(2041, 1, 21)
    # 2041-01-22 is Tuesday.
    following_day = date(2041, 1, 22)
    with_inauguration = federal_holidays(2041, include_inauguration=True)
    without_inauguration = federal_holidays(2041, include_inauguration=False)

    assert observed in with_inauguration
    assert observed in without_inauguration
    # The Sunday observation coincides with MLK, adding zero holiday dates.
    assert set(with_inauguration) == set(without_inauguration)
    assert is_business_day(observed, include_inauguration=True) is False
    assert is_business_day(observed, include_inauguration=False) is False
    assert is_business_day(following_day, include_inauguration=True) is True
    assert is_business_day(following_day, include_inauguration=False) is True


def test_juneteenth_starts_in_2021():
    # 2020-06-19 is Friday; Juneteenth was not yet a federal holiday.
    before_enactment = date(2020, 6, 19)
    # 2021-06-19 is Saturday; observation is 2021-06-18, Friday.
    first_observed = date(2021, 6, 18)

    assert before_enactment not in federal_holidays(2020)
    assert is_business_day(before_enactment) is True
    assert first_observed in federal_holidays(2021)
    assert is_business_day(first_observed) is False


def test_thanksgiving_and_christmas_2026():
    holidays = federal_holidays(2026)
    # 2026-11-26 is Thursday, the fourth Thursday in November.
    thanksgiving = date(2026, 11, 26)
    # 2026-12-25 is Friday; Christmas needs no observation shift.
    christmas = date(2026, 12, 25)

    assert thanksgiving in holidays
    assert christmas in holidays
    assert is_business_day(thanksgiving) is False
    assert is_business_day(christmas) is False


def test_business_days_between_thanksgiving_week_endpoints():
    # 2026-11-25 is Wednesday (excluded start).
    start = date(2026, 11, 25)
    # 2026-11-30 is Monday (included end).
    end = date(2026, 11, 30)
    # Skip Nov 26 Thu (Thanksgiving), Nov 28 Sat, and Nov 29 Sun.
    # Count: Nov 27 Fri = 1; Nov 30 Mon = 2.
    assert business_days_between(start, end) == 2
    # Reversing the same span negates its manually counted two days.
    assert business_days_between(end, start) == -2


@pytest.mark.parametrize(
    "day",
    [
        date(2026, 10, 2),   # 2026-10-02 is Friday, a business day.
        date(2026, 10, 3),   # 2026-10-03 is Saturday.
        date(2026, 10, 4),   # 2026-10-04 is Sunday.
        date(2026, 10, 12),  # 2026-10-12 is Monday, Columbus Day.
    ],
)
def test_zero_day_operations_preserve_start(day):
    # Count: zero advances leave day 0 unchanged, even on a nonbusiness day.
    assert add_business_days(day, 0) == day
    # Count: there are zero dates satisfying day < d <= day.
    assert business_days_between(day, day) == 0


@pytest.mark.parametrize(
    ("start", "expected"),
    [
        (
            date(2026, 10, 3),  # 2026-10-03 is Saturday.
            date(2026, 10, 5),  # 2026-10-05 is Monday.
        ),
        (
            date(2026, 10, 12),  # 2026-10-12 is Monday, Columbus Day.
            date(2026, 10, 13),  # 2026-10-13 is Tuesday.
        ),
        (
            date(2026, 7, 3),  # 2026-07-03 is Friday, observed Independence Day.
            date(2026, 7, 6),  # 2026-07-06 is Monday.
        ),
    ],
)
def test_next_business_day_skips_weekends_and_holidays(start, expected):
    # Counts: Oct 3 skips Sat/Sun to Mon Oct 5; Oct 12 skips the holiday
    # to Tue Oct 13; Jul 3 skips the holiday and Sat/Sun to Mon Jul 6.
    # In each case the expected date is the first available business day.
    assert next_business_day(start) == expected
