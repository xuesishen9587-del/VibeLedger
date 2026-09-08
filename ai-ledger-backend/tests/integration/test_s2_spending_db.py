"""S2 persistence proofs against the disposable PostgreSQL baseline."""
from datetime import date
from uuid import uuid4
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from fastapi.testclient import TestClient
from app.auth.context import AuthContext
from app.api.deps import get_db_connection, get_auth_context
from app.api.routes.transactions import get_fx_provider
from app.main import create_app
from app.db import get_connection
from app.repositories import simplified_schema as schema, spending as repo
from app.services import spending_service as service
from tests.support.db_helper import BaseDbTestCase


class TestS2SpendingDb(BaseDbTestCase):
    def seed_test_data(self):
        self.hh = uuid4()
        self.user = uuid4()
        schema.create_household(self.conn, "Spending", household_id=self.hh)
        schema.create_user(self.conn, auth_subject=str(self.user), email=f"{self.user}@example.test",
                           display_name="Test", user_id=self.user)
        schema.add_household_member(self.conn, self.hh, self.user, "owner")
        self.category = schema.create_category(self.conn, self.hh, "Other", "expense", is_fallback=True)["id"]
        self.actor = AuthContext("browser", self.user, self.hh, "owner")
        self.conn.commit()
        app = create_app()
        app.dependency_overrides[get_auth_context] = lambda: self.actor
        app.dependency_overrides[get_fx_provider] = lambda: None
        def db():
            conn = get_connection(self.test_schema)
            try:
                yield conn
            finally:
                conn.close()
        app.dependency_overrides[get_db_connection] = db
        self.client = TestClient(app)

    def post(self, data=None, key=None):
        payload = {"transaction_type": "expense", "occurred_on": "2026-01-01",
                   "original_amount": "100.00", "original_currency": "CNY",
                   "category_id": str(self.category), "merchant": "Lunch"}
        payload.update(data or {})
        return self.client.post("/api/v1/transactions", json=payload,
                               headers={"Idempotency-Key": key or str(uuid4())})

    def change(self, identity, data, key=None):
        return self.client.patch(f"/api/v1/transactions/{identity}", json=data,
                                headers={"Idempotency-Key": key or str(uuid4())})

    def test_create_replay_and_independence(self):
        key = str(uuid4())
        response = self.post(key=key)
        self.assertEqual(response.status_code, 201, response.text)
        row = response.json()
        self.assertEqual(row["review_reasons"], ["MISSING_ACCOUNT"])
        self.assertEqual(row["original_amount"], "100.00")
        self.assertEqual(self.post(key=key).json(), row)
        self.assertEqual(self.post({"original_amount": "101.00"}, key).status_code, 409)
        for table, count in (("transactions", 1), ("audit_events", 1), ("account_snapshots", 0)):
            self.assertEqual(repo.rows(self.conn, f"SELECT count(*) AS n FROM {table}")[0]["n"], count)

    def test_flags_correct_independently_and_preserve_id(self):
        row = self.post().json()
        with self.conn.cursor() as cur:
            cur.execute("UPDATE transactions SET category_uncertain=true WHERE id=%s", (row["id"],))
        self.conn.commit()
        fixed = self.change(row["id"], {"expected_version": 0, "category_id": str(self.category)}).json()
        self.assertEqual(fixed["review_reasons"], ["MISSING_ACCOUNT"])
        key = str(uuid4())
        payload = {"expected_version": 1, "account_review_acknowledged": True}
        fixed = self.change(row["id"], payload, key).json()
        self.assertEqual(fixed["review_reasons"], [])
        self.assertEqual(self.change(row["id"], payload, key).json(), fixed)
        self.assertEqual(self.change(row["id"], payload).status_code, 409)
        self.assertEqual(len(self.client.get("/api/v1/transactions?missing_account=true").json()["items"]), 1)
        self.assertEqual(self.client.get("/api/v1/transactions?needs_metadata_review=true").json()["items"], [])

    def test_refund_limits_and_void(self):
        purchase = self.post().json()
        refund = self.post({"transaction_type": "refund", "original_amount": "30.00",
                            "refund_of_transaction_id": purchase["id"]})
        self.assertEqual(refund.status_code, 201, refund.text)
        self.assertEqual(self.post({"transaction_type": "refund", "original_amount": "71.00",
                                  "refund_of_transaction_id": purchase["id"]}).status_code, 409)
        self.assertEqual(self.change(purchase["id"], {"expected_version": 0, "original_amount": "29.00"}).status_code, 409)
        path = f"/api/v1/transactions/{purchase['id']}/void"
        self.assertEqual(self.client.post(path, json={"expected_version": 0, "delete_reason": "Mistake"},
                                         headers={"Idempotency-Key": str(uuid4())}).status_code, 409)
        self.assertEqual(self.post({"transaction_type": "refund", "remarks": "Old purchase", "original_amount": "5.00"}).status_code, 201)

    def test_money_references_and_presence(self):
        for amount, curr in (("1.01", "JPY"), ("NaN", "CNY"), ("Infinity", "CNY"),
                             ("0", "CNY"), ("100000000000000", "CNY"), (1.1, "CNY")):
            self.assertEqual(self.post({"original_amount": amount, "original_currency": curr}).status_code, 422)
        self.assertEqual(self.post({"category_id": str(uuid4())}).status_code, 404)
        self.assertEqual(self.post({"transaction_type": "refund"}).status_code, 422)
        row = self.post().json()
        self.assertEqual(self.change(row["id"], {"expected_version": 0, "category_id": None}).status_code, 422)
        self.assertEqual(self.change(row["id"], {"expected_version": 0, "category_uncertain": True}).status_code, 422)
        self.assertEqual(self.change(row["id"], {"expected_version": 0, "merchant": None}).json()["merchant"], None)

    def test_fx_missing_then_frozen_and_edit_recompute(self):
        missing = self.post({"original_currency": "USD"}).json()
        self.assertIsNone(missing["reporting_amount"])
        with self.conn.cursor() as cur:
            cur.execute("INSERT INTO fx_quotes(from_currency,to_currency,rate_as_of,rate,source) "
                        "VALUES('USD','CNY','2026-01-01',7.2,'fixture'),('USD','CNY','2026-01-02',7.3,'fixture')")
        self.conn.commit()
        row = self.post({"original_currency": "USD", "original_amount": "10.00"}).json()
        self.assertEqual(row["reporting_amount"], "72.00")
        changed = self.change(row["id"], {"expected_version": 0, "merchant": "Edited"}).json()
        self.assertEqual(changed["reporting_amount"], "72.00")
        changed = self.change(row["id"], {"expected_version": 1, "occurred_on": "2026-01-02"}).json()
        self.assertEqual(changed["reporting_amount"], "73.00")

    def test_audit_failure_rolls_back_everything(self):
        with patch.object(service.audit, "insert_audit_event", side_effect=RuntimeError("injected")):
            with self.assertRaises(RuntimeError):
                self.post()
        self.assertEqual(repo.rows(self.conn, "SELECT count(*) AS n FROM transactions")[0]["n"], 0)
        self.assertEqual(repo.rows(self.conn, "SELECT count(*) AS n FROM ingestion_requests")[0]["n"], 0)

    def test_pagination_void_and_cross_household(self):
        identities = [self.post().json()["id"] for _ in range(3)]
        page = self.client.get("/api/v1/transactions?limit=2").json()
        tail = self.client.get("/api/v1/transactions", params={"limit": 2, "cursor": page["next_cursor"]}).json()
        self.assertEqual(len({r["id"] for r in page["items"] + tail["items"]}), 3)
        self.assertEqual(self.client.get("/api/v1/transactions?cursor=bad").status_code, 422)
        self.assertEqual(self.client.get("/api/v1/transactions?from=2026-02-01&to=2026-01-01").status_code, 422)
        self.actor = AuthContext("browser", self.user, uuid4(), "owner")
        self.assertEqual(self.client.get(f"/api/v1/transactions/{identities[0]}").status_code, 404)
        self.assertEqual(self.client.get("/api/v1/transactions").json()["items"], [])

    def test_concurrent_refunds_cannot_exceed_purchase(self):
        purchase = self.post().json()
        def refund(_):
            return self.post({"transaction_type": "refund", "original_amount": "60.00",
                              "refund_of_transaction_id": purchase["id"]}).status_code
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(refund, range(2)))
        self.assertEqual(sorted(results), [201, 409])
        self.assertEqual(repo.refund_total(self.conn, self.hh, purchase["id"]), Decimal("60"))

    def test_review_and_reports_expose_partial_totals(self):
        self.post()
        self.post({"transaction_type": "refund", "original_amount": "30.00", "remarks": "Pre-start purchase"})
        self.post({"original_amount": "10.00", "original_currency": "USD"})
        response = self.client.get("/api/v1/reports/spending?from=2026-01-01&to=2026-01-31")
        self.assertEqual(response.status_code, 200, response.text)
        report = response.json()
        self.assertIsNone(report["net_spending"])
        self.assertEqual(Decimal(report["known_net_spending"]), Decimal("70"))
        self.assertEqual(report["missing_conversion_count"], 1)
        review = self.client.get("/api/v1/review").json()
        self.assertEqual(review["counts"]["transactions"], 3)
        self.assertEqual(len(review["items"]), 3)

    def test_refresh_fills_once_with_audit_and_replay(self):
        row = self.post({"original_currency": "USD", "original_amount": "10.00"}).json()
        with self.conn.cursor() as cur:
            cur.execute("INSERT INTO fx_quotes(from_currency,to_currency,rate_as_of,rate,source) "
                        "VALUES('USD','CNY','2026-01-01',7.2,'fixture')")
        self.conn.commit()
        key = str(uuid4())
        result = self.client.post("/api/v1/reports/refresh-fx", json={}, headers={"Idempotency-Key": key})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json(), {"filled": 1, "pending": 0})
        self.assertEqual(self.client.post("/api/v1/reports/refresh-fx", json={}, headers={"Idempotency-Key": key}).json(), result.json())
        saved = self.client.get(f"/api/v1/transactions/{row['id']}").json()
        self.assertEqual(saved["reporting_amount"], "72.00")
        self.assertEqual(saved["row_version"], 1)
        self.assertEqual(repo.rows(self.conn, "SELECT count(*) AS n FROM audit_events WHERE action='fill_reporting_fx'")[0]["n"], 1)

    def schedule(self, today=date(2026, 1, 31), **overrides):
        body = {"name": "Laptop", "kind": "installment", "amount_per_period": "50.00",
                "currency": "CNY", "period_count": 3, "start_month": "2026-01-01",
                "day_of_month": 31, "merchant": "Laptop shop", "category_id": str(self.category)}
        body.update(overrides)
        with patch("app.services.spending_schedules.local_today", return_value=today):
            preview = self.client.post("/api/v1/spending-schedules/preview", json=body)
            self.assertEqual(preview.status_code, 200, preview.text)
            body["acknowledged_due_through"] = preview.json()["acknowledged_due_through"]
            response = self.client.post("/api/v1/spending-schedules", json=body, headers={"Idempotency-Key": str(uuid4())})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def catch_up(self, today):
        with patch("app.services.spending_schedules.local_today", return_value=today):
            result = self.client.post("/api/v1/spending-schedules/materialize", headers={"Idempotency-Key": str(uuid4())})
        self.assertEqual(result.status_code, 200, result.text)
        return result.json()

    def test_schedule_clamp_due_only_catch_up_and_complete(self):
        schedule = self.schedule()
        self.assertEqual(repo.rows(self.conn, "SELECT count(*) AS n FROM transactions")[0]["n"], 1)
        for _ in range(2):
            self.catch_up(date(2026, 3, 31))
        detail = self.client.get(f"/api/v1/spending-schedules/{schedule['id']}").json()
        self.assertEqual([o["due_on"] for o in detail["occurrences"]], ["2026-01-31", "2026-02-28", "2026-03-31"])
        self.assertEqual(detail["status"], "completed")
        self.assertEqual(repo.rows(self.conn, "SELECT sum(original_amount) AS n FROM transactions")[0]["n"], Decimal("150"))
        self.assertEqual(repo.rows(self.conn, "SELECT count(*) AS n FROM account_snapshots")[0]["n"], 0)

    def test_schedule_pause_skips_missed_dates_and_resume_does_not_backcharge(self):
        schedule = self.schedule(period_count=4)
        identity = schedule["id"]
        def action(name, version, today):
            with patch("app.services.spending_schedules.local_today", return_value=today):
                result = self.client.post(f"/api/v1/spending-schedules/{identity}/{name}",
                    json={"expected_version": version}, headers={"Idempotency-Key": str(uuid4())})
            self.assertEqual(result.status_code, 200, result.text)
            return result.json()
        paused = action("pause", 0, date(2026, 1, 31))
        action("resume", paused["row_version"], date(2026, 3, 1))
        self.catch_up(date(2026, 3, 31))
        detail = self.client.get(f"/api/v1/spending-schedules/{identity}").json()
        self.assertEqual([o["status"] for o in detail["occurrences"]], ["recorded", "skipped", "recorded"])
        cancelled = action("cancel", detail["row_version"], date(2026, 4, 1))
        self.assertEqual(cancelled["status"], "cancelled")
        self.catch_up(date(2026, 5, 1))
        self.assertEqual(repo.rows(self.conn, "SELECT count(*) AS n FROM transactions")[0]["n"], 2)

    def test_schedule_duplicate_link_and_void_never_regenerate(self):
        purchase = self.post({"occurred_on": "2026-01-31", "original_amount": "50.00", "merchant": "Laptop shop"}).json()
        schedule = self.schedule(period_count=1)
        occurrence = self.client.get(f"/api/v1/spending-schedules/{schedule['id']}").json()["occurrences"][0]
        self.assertEqual(occurrence["status"], "needs_confirmation")
        result = self.client.post(f"/api/v1/schedule-occurrences/{occurrence['id']}/resolve",
            json={"expected_version": occurrence["row_version"], "action": "link_existing",
                  "transaction_id": purchase["id"], "expected_transaction_version": 0}, headers={"Idempotency-Key": str(uuid4())})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(repo.rows(self.conn, "SELECT count(*) AS n FROM transactions")[0]["n"], 1)
        self.client.post(f"/api/v1/transactions/{purchase['id']}/void",
            json={"expected_version": 1, "delete_reason": "Skipped"}, headers={"Idempotency-Key": str(uuid4())})
        self.catch_up(date(2026, 2, 28))
        self.assertEqual(repo.rows(self.conn, "SELECT count(*) AS n FROM transactions WHERE status='committed'")[0]["n"], 0)

    def test_schedule_concurrent_materialize_once(self):
        self.schedule(today=date(2026, 1, 1))
        with patch("app.services.spending_schedules.local_today", return_value=date(2026, 3, 31)):
            def run(_):
                return self.client.post("/api/v1/spending-schedules/materialize", headers={"Idempotency-Key": str(uuid4())}).status_code
            with ThreadPoolExecutor(2) as pool:
                self.assertEqual(list(pool.map(run, range(2))), [200, 200])
        self.assertEqual(repo.rows(self.conn, "SELECT count(*) AS n FROM transactions")[0]["n"], 3)

    def test_schedule_upfront_conversion_is_atomic(self):
        purchase = self.post().json()
        body = {"name": "Laptop", "kind": "installment", "amount_per_period": "50.00", "currency": "CNY",
                "period_count": 2, "start_month": "2026-01-01", "day_of_month": 31, "merchant": "Laptop shop",
                "category_id": str(self.category), "acknowledged_due_through": "2026-01-31",
                "replaces_transaction_id": purchase["id"], "expected_transaction_version": 0}
        with patch("app.services.spending_schedules.local_today", return_value=date(2026, 1, 31)):
            response = self.client.post("/api/v1/spending-schedules", json=body, headers={"Idempotency-Key": str(uuid4())})
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(self.client.get(f"/api/v1/transactions/{purchase['id']}").json()["status"], "voided")
        self.assertEqual(repo.rows(self.conn, "SELECT sum(original_amount) AS n FROM transactions WHERE status='committed'")[0]["n"], Decimal("50"))
