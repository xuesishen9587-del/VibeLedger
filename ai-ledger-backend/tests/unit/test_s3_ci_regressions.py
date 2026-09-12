"""Regressions found by the Python 3.10/PostgreSQL CI run on 9f9a4b4."""
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4
import unittest
from fastapi import HTTPException
from app.domain.balances import SnapshotCorrection, instant
from app.services.statement_import import prepare


class ObservationTimestampTest(unittest.TestCase):
    def test_pydantic_correction_timestamp_round_trips_on_python310(self):
        payload=SnapshotCorrection(expected_version=0,expected_latest_snapshot_id=uuid4(),
            expected_account_version=0,reason="Correct amount",balance="0.00",currency="CNY",
            as_of=datetime(2026,2,2,4,tzinfo=timezone.utc),time_basis="explicit")
        wire=payload.model_dump(mode="json")["as_of"]
        self.assertEqual(instant(wire),payload.as_of)

    def test_utc_suffix_and_explicit_offsets_are_equivalent(self):
        expected=datetime(2026,2,2,4,0,0,123456,tzinfo=timezone.utc)
        for value in ("2026-02-02T04:00:00.123456Z","2026-02-02T04:00:00.123456+00:00",
                      "2026-02-02T12:00:00.123456+08:00",expected):
            with self.subTest(value=value):
                self.assertEqual(instant(value),expected)

    def test_missing_timezone_and_malformed_timestamp_still_fail(self):
        for value in ("2026-02-02T04:00:00","not-a-dateZ",None,datetime(2026,2,2)):
            with self.subTest(value=value),self.assertRaises(HTTPException) as error:
                instant(value)
            self.assertEqual(error.exception.detail["error"]["code"],"INVALID_DATE")


class StatementExtractionOwnershipTest(unittest.TestCase):
    def test_preparing_the_same_parser_result_twice_preserves_page_evidence(self):
        data={"account_hint":"Wallet","account_currency":"CNY","account_confidence":.99,
            "period_start":"2026-02-01","period_end":"2026-02-28","lines":[],
            "actual_page_count":2,"processed_pages":[1],"expected_line_count":0,"complete":True}
        original=deepcopy(data)
        account={"id":uuid4(),"name":"Wallet","currency":"CNY","aliases":[]}
        categories=[{"id":uuid4(),"name":"Other","is_fallback":True}]
        for _ in range(2):
            draft=prepare(None,SimpleNamespace(household_id=uuid4()),{"id":uuid4()},data,account,categories,{})
            self.assertEqual(draft["actual_page_count"],2)
            self.assertTrue(draft["partial"])
            self.assertEqual(data,original)
