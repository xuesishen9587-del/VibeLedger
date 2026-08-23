import os
os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("DATABASE_URL", "postgresql://test_user:test_pass@127.0.0.1:5432/vibeledger_test")
os.environ.setdefault("DB_SCHEMA", "vibeledger_test_runner")

import unittest
from unittest.mock import MagicMock, patch
from uuid import uuid4
from decimal import Decimal
from datetime import datetime, date, timezone

from app.services.snapshot_service import (
    ledger_balance_as_of,
    is_first_account_observation,
    sum_committed_transaction_deltas
)
from app.services.reference_fx_service import ReferenceFxService

class TestSnapshotServiceUnit(unittest.TestCase):
    def setUp(self):
        self.account_id = uuid4()
        self.mock_fx = ReferenceFxService(fixed_rates={
            ("USD", "CNY"): Decimal("7.20"),
            ("EUR", "CNY"): Decimal("7.80")
        })

    def test_historical_balance_as_of_with_snapshot_anchor(self):
        """
        Tests ledger_balance_as_of when an authoritative snapshot anchor exists.
        Snapshot at Jan 31 = 1000 CNY.
        Feb 1 = -50 CNY (expense).
        Balance at Feb 2 should be 950 CNY.
        """
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur

        with patch("app.repositories.accounts.get_account") as mock_get_account, \
             patch("app.repositories.snapshots.get_latest_authoritative_snapshot") as mock_get_snap:
            
            mock_get_account.return_value = {"id": self.account_id, "currency": "CNY"}
            mock_get_snap.return_value = {
                "id": uuid4(),
                "as_of": datetime(2026, 1, 31, 23, 59, 59, tzinfo=timezone.utc),
                "balance": Decimal("1000.00"),
                "currency": "CNY"
            }

            # Transaction on Feb 1: from_account_id=account_id, from_amount=50.00
            mock_cur.fetchall.return_value = [
                (self.account_id, None, Decimal("50.00"), None)
            ]

            bal = ledger_balance_as_of(
                conn=mock_conn,
                account_id=self.account_id,
                as_of_dt=datetime(2026, 2, 2, 12, 0, 0, tzinfo=timezone.utc)
            )

            self.assertEqual(bal, Decimal("950.00"))

    def test_historical_balance_as_of_without_snapshot_anchor(self):
        """
        Tests ledger_balance_as_of without any prior snapshot.
        Opening at Jan 1 = +1000
        Jan 5 = -100
        Jan 10 = +200
        Feb 1 = -50
        """
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur

        with patch("app.repositories.accounts.get_account") as mock_get_account, \
             patch("app.repositories.snapshots.get_latest_authoritative_snapshot") as mock_get_snap:
            
            mock_get_account.return_value = {"id": self.account_id, "currency": "CNY"}
            mock_get_snap.return_value = None

            # Transactions up to Feb 2
            mock_cur.fetchall.return_value = [
                (None, self.account_id, None, Decimal("1000.00")), # Opening +1000
                (self.account_id, None, Decimal("100.00"), None),   # Jan 5 -100
                (None, self.account_id, None, Decimal("200.00")),   # Jan 10 +200
                (self.account_id, None, Decimal("50.00"), None)     # Feb 1 -50
            ]

            bal = ledger_balance_as_of(
                conn=mock_conn,
                account_id=self.account_id,
                as_of_dt=datetime(2026, 2, 2, 12, 0, 0, tzinfo=timezone.utc)
            )

            # 1000 - 100 + 200 - 50 = 1050
            self.assertEqual(bal, Decimal("1050.00"))

    def test_is_first_account_observation_check(self):
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur

        # Case 1: neither snapshot nor opening balance -> True
        mock_cur.fetchone.side_effect = [None, None]
        self.assertTrue(is_first_account_observation(mock_conn, self.account_id))

        # Case 2: snapshot exists -> False
        mock_cur.fetchone.side_effect = [(1,)]
        self.assertFalse(is_first_account_observation(mock_conn, self.account_id))

        # Case 3: opening balance exists -> False
        mock_cur.fetchone.side_effect = [None, (1,)]
        self.assertFalse(is_first_account_observation(mock_conn, self.account_id))

    def test_sum_committed_transaction_deltas(self):
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur

        other_acc = uuid4()
        mock_cur.fetchall.return_value = [
            (self.account_id, other_acc, Decimal("200.00"), Decimal("200.00")), # Transfer out: -200
            (other_acc, self.account_id, Decimal("500.00"), Decimal("500.00")), # Transfer in: +500
            (self.account_id, None, Decimal("50.00"), None)                     # Expense: -50
        ]

        delta = sum_committed_transaction_deltas(
            mock_conn, self.account_id, date(2026, 8, 1), date(2026, 8, 31)
        )
        # -200 + 500 - 50 = +250
        self.assertEqual(delta, Decimal("250.00"))

if __name__ == "__main__":
    unittest.main()
