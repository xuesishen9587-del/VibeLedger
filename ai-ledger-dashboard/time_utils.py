from datetime import date, datetime, time, timezone
from typing import Optional, Union


def format_iso_timestamp(
    target_date: Optional[Union[date, str]] = None,
    target_time: Optional[time] = None,
    tz: Optional[timezone] = None
) -> str:
    """
    Constructs a strict timezone-aware ISO 8601 timestamp string.
    Ensures safe local/browser representation without naive datetimes.
    """
    effective_tz = tz or datetime.now().astimezone().tzinfo or timezone.utc

    if target_date is None:
        return datetime.now(effective_tz).isoformat()

    if isinstance(target_date, str):
        target_date = date.fromisoformat(target_date)

    if target_time is None:
        # Combine with current local time on that date
        now_local = datetime.now(effective_tz)
        target_time = now_local.time()

    dt = datetime.combine(target_date, target_time, tzinfo=effective_tz)
    return dt.isoformat()
