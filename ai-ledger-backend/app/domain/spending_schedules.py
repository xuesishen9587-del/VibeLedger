from calendar import monthrange
from datetime import date
from app.domain.spending import fail


def due_date(start_month, day_of_month, period_no):
    if isinstance(start_month, str):
        start_month = date.fromisoformat(start_month)
    offset = start_month.year * 12 + start_month.month - 1 + period_no - 1
    year, month = divmod(offset, 12)
    month += 1
    if not 1 <= year <= 9999:
        fail("INVALID_SCHEDULE", "The schedule dates exceed the supported calendar.")
    return date(year, month, min(day_of_month, monthrange(year, month)[1]))


def due_periods(schedule, through):
    start = schedule["start_month"]
    if isinstance(start, str):
        start = date.fromisoformat(start)
    elapsed = max(0, (through.year - start.year) * 12 + through.month - start.month + 1)
    count = min(elapsed, schedule["period_count"]) if schedule["period_count"] else elapsed
    return [(period, day) for period in range(1, count + 1)
            if (day := due_date(start, schedule["day_of_month"], period)) <= through]
