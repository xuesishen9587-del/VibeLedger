import os
import unittest
from unittest.mock import MagicMock, patch
from datetime import date, datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from time_utils import (
    format_iso_timestamp,
    get_dashboard_timezone,
    get_dashboard_today,
    get_dashboard_now
)
from api_client import ApiClient, AuthError, ConflictError, ValidationError


class TestDashboardFlows(unittest.TestCase):
    """
    Tests covering user-visible workflows and presentation logic for VibeLedger Dashboard.
    """

    def setUp(self):
        self.client = ApiClient(base_url="http://mock-backend:8000", auth_token="mock.token")

    def test_timezone_singapore_boundary(self):
        """
        Proves that when DASHBOARD_TIMEZONE is Asia/Singapore and UTC is 2026-08-27 23:30,
        the local business date in Singapore is 2026-08-28 (boundary check).
        """
        with patch.dict(os.environ, {"DASHBOARD_TIMEZONE": "Asia/Singapore"}):
            self.assertEqual(get_dashboard_timezone().key, "Asia/Singapore")

            # Mock datetime.now() with UTC 2026-08-27 23:30:00
            utc_dt = datetime(2026, 8, 27, 23, 30, 0, tzinfo=timezone.utc)
            with patch("time_utils.datetime") as mock_datetime:
                mock_datetime.now.side_effect = lambda tz=None: utc_dt.astimezone(tz or ZoneInfo("Asia/Singapore"))
                mock_datetime.combine = datetime.combine

                now_local = get_dashboard_now()
                self.assertEqual(now_local.year, 2026)
                self.assertEqual(now_local.month, 8)
                self.assertEqual(now_local.day, 28)
                self.assertEqual(now_local.hour, 7)
                self.assertEqual(now_local.minute, 30)

                today_local = get_dashboard_today()
                self.assertEqual(today_local, date(2026, 8, 28))

                iso_ts = format_iso_timestamp()
                self.assertTrue(iso_ts.startswith("2026-08-28T07:30:00+08:00"))





    def test_snapshot_timestamp_helper_timezone_aware(self):
        """Proves format_iso_timestamp constructs timezone-aware ISO 8601 strings."""
        target_d = date(2026, 8, 27)
        iso_str = format_iso_timestamp(target_d)
        dt = datetime.fromisoformat(iso_str)
        self.assertIsNotNone(dt.tzinfo, "Constructed timestamp must be timezone-aware.")
        self.assertEqual(dt.date(), target_d)










if __name__ == "__main__":
    unittest.main()
