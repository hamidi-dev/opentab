from opentab.accounting.averages import SessionPoint, nearest_rank, range_averages, spread


def point(day, cost=1.0, tokens=100, worked=60.0):
    return SessionPoint(day, cost, tokens, worked)


def test_spread_reports_the_median_beside_a_skewed_mean():
    s = spread([1, 1, 2, 2, 100])
    assert (s.median, s.mean, s.p90, s.max, s.count) == (2, 21.2, 100, 100, 5)
    even = spread([4, 1, 3, 2])
    assert even.median == 2.5  # the middle pair, after sorting
    assert spread([]) is None


def test_p90_is_the_nearest_rank_and_never_interpolates():
    ordered = list(range(1, 11))
    assert nearest_rank(ordered, 0.9) == 9
    assert nearest_rank([7.0], 0.9) == 7.0
    assert nearest_rank(list(range(1, 21)), 0.9) == 18
    assert nearest_rank([], 0.9) == 0.0


def test_range_span_divides_by_calendar_days_and_active_days_separately():
    points = [point("2026-01-01", 10), point("2026-01-01", 2), point("2026-01-10", 3)]
    a = range_averages(points, "2026-01-01", "2026-01-10", "2025-06-01", "2026-11-07")
    assert (a.start, a.end, a.calendar_days, a.active_days) == ("2026-01-01", "2026-01-10", 10, 2)
    assert a.sessions == 3 and a.cost == 15
    assert a.per_day(a.cost) == 1.5 and a.per_active_day(a.cost) == 7.5


def test_a_window_older_than_the_history_starts_at_the_first_recorded_session():
    # A one-year window over a month of history must not divide by 365.
    points = [point("2026-10-08"), point("2026-11-07")]
    a = range_averages(points, "2025-11-07", None, "2026-10-08", "2026-11-07")
    assert a.start == "2026-10-08" and a.calendar_days == 31
    # An open range starts at the history too, and runs to today.
    a = range_averages(points, None, None, "2026-10-01", "2026-11-07")
    assert a.start == "2026-10-01" and a.end == "2026-11-07"


def test_the_span_ends_at_the_range_end_capped_at_today_but_covers_every_session():
    a = range_averages([point("2026-03-05")], "2026-03-01", "2026-03-31", None, "2026-11-07")
    assert a.end == "2026-03-31" and a.calendar_days == 31
    a = range_averages([point("2026-11-05")], "2026-11-01", "2026-12-31", None, "2026-11-07")
    assert a.end == "2026-11-07"  # no dividing by days that haven't happened
    a = range_averages([point("2026-11-09")], "2026-11-01", None, None, "2026-11-07")
    assert a.end == "2026-11-09"  # a session dated past today (clock skew) stays inside


def test_unknown_worked_time_is_left_out_of_its_spread_not_counted_as_zero():
    points = [point("2026-01-01", worked=None), point("2026-01-02", worked=120.0)]
    a = range_averages(points, None, None, None, "2026-01-02")
    assert a.worked_spread.count == 1 and a.worked_spread.median == 120.0
    assert (
        range_averages(
            [point("2026-01-01", worked=None)], None, None, None, "2026-01-01"
        ).worked_spread
        is None
    )


def test_undated_rows_are_skipped_and_an_empty_range_has_no_summary():
    assert range_averages([], None, None, None, "2026-01-01") is None
    assert range_averages([point("")], None, None, None, "2026-01-01") is None
    a = range_averages([point(""), point("2026-01-01")], None, None, None, "2026-01-01")
    assert a.sessions == 1
