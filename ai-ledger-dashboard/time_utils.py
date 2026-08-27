import os
from zoneinfo import ZoneInfo
from datetime import date, datetime, time, timezone
from typing import Optional, Union

DEFAULT_DASHBOARD_TIMEZONE = "Asia/Singapore"


def get_dashboard_timezone() -> ZoneInfo:
    """
    Returns the configured IANA timezone for the dashboard.
    Controlled by DASHBOARD_TIMEZONE environment variable (default: 'Asia/Singapore').
    """
    tz_name = os.environ.get("DASHBOARD_TIMEZONE", DEFAULT_DASHBOARD_TIMEZONE)
    try:
        return ZoneInfo(tz_name)
    except Exception:
        return ZoneInfo(DEFAULT_DASHBOARD_TIMEZONE)


def get_dashboard_now() -> datetime:
    """
    Returns the current datetime in the configured dashboard timezone.
    """
    return datetime.now(get_dashboard_timezone())


def get_dashboard_today() -> date:
    """
    Returns the current business date in the configured dashboard timezone.
    """
    return get_dashboard_now().date()


def format_iso_timestamp(
    target_date: Optional[Union[date, str]] = None,
    target_time: Optional[time] = None,
    tz: Optional[Union[timezone, ZoneInfo]] = None
) -> str:
    """
    Constructs a strict timezone-aware ISO 8601 timestamp string.
    Defaults to the configured DASHBOARD_TIMEZONE (e.g. Asia/Singapore).
    """
    effective_tz = tz or get_dashboard_timezone()

    if target_date is None:
        return datetime.now(effective_tz).isoformat()

    if isinstance(target_date, str):
        target_date = date.fromisoformat(target_date)

    if target_time is None:
        now_local = datetime.now(effective_tz)
        target_time = now_local.time()

    dt = datetime.combine(target_date, target_time, tzinfo=effective_tz)
    return dt.isoformat()
