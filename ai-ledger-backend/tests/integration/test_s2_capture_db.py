import base64
import io
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import uuid4
from unittest.mock import patch
from PIL import Image
from app.auth.context import AuthContext
from app.api.routes.expenses import get_capture_model
from app.services.gemini_service import MockGeminiService, ExpenseExtractionResult, ExpenseRevisionResult
from app.services import capture_receipts
from app.repositories import simplified_schema as schema, spending as repo
from tests.support.db_helper import BaseDbTestCase
from tests.integration import test_s2_spending_db as spending_tests


class TestS2CaptureDb(BaseDbTestCase):
    def seed_test_data(self):
        spending_tests.TestS2SpendingDb.seed_test_data(self)
        self.browser = self.actor
        device = schema.create_device(self.conn, self.hh, self.user, "Phone", "ios", b"x" * 32)
        self.device = AuthContext("device", self.user, self.hh, "owner", device_id=device["id"])
        self.actor = self.device
        self.conn.commit()
        self.model = MockGeminiService(self.result())
        self.client.app.dependency_overrides[get_capture_model] = lambda: self.model
        buffer = io.BytesIO()
        Image.new("RGB", (3, 3), (100, 20, 30)).save(buffer, format="PNG")
        self.image = base64.b64encode(buffer.getvalue()).decode()

    def result(self, **kwargs):
        data = dict(occurred_on=date(2026, 1, 2), merchant="Cafe", original_amount=Decimal("28.50"),
                    original_currency="CNY", category="Other", payment_mode="one_off", intent="expense",
                    date_evidence="visible", field_confidence={k: .99 for k in ("amount", "currency", "date", "intent", "category", "account")})
        data.update(kwargs)
        return ExpenseExtractionResult(**data)

    def capture(self, key=None, **overrides):
        body = {"idempotency_key": key or str(uuid4()), "captured_at": "2026-01-02T12:00:00+08:00",
                "image": {"mime_type": "image/png", "base64": self.image}, "note": None}
        body.update(overrides)
        return self.client.post("/api/v1/expenses", json=body)

    def draft(self, **kwargs):
        self.model.set_next_result(self.result(payment_mode=None, **kwargs))
        response = self.capture()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "needs_confirmation")
        return response.json()

    def edit(self, identity, **fields):
        return self.client.patch(f"/api/v1/ingestion-requests/{identity}/draft", json=fields)

    def test_clear_capture_preserves_wire_and_replays_without_ai(self):
        key = str(uuid4())
        response = self.capture(key)
        self.assertEqual(response.status_code, 200, response.text)
        saved = response.json()
        self.assertEqual(saved["status"], "committed")
        self.assertEqual(saved["payment_mode"], "one_off")
        self.assertIn("28.50", saved["display_summary"])
        self.assertEqual(self.capture(key).json(), saved)
        self.assertEqual(self.model.call_count, 1)
        self.assertEqual(self.capture(key, note="changed").status_code, 409)
        self.assertEqual(self.client.get(f"/api/v1/ingestion-requests/by-key/{key}").json(), saved)
        self.assertEqual(self.client.post(f"/api/v1/ingestion-requests/by-key/{key}/cancel").json(), saved)
        self.assertEqual(repo.rows(self.conn, "SELECT count(*) AS n FROM transactions")[0]["n"], 1)
        self.assertEqual(repo.rows(self.conn, "SELECT count(*) AS n FROM account_snapshots")[0]["n"], 0)

    def test_metadata_fallback_commits_with_both_review_reasons(self):
        self.model.set_next_result(self.result(category="Unrecognized", from_account="Unknown"))
        response = self.capture()
        self.assertEqual(response.status_code, 200, response.text)
        row = repo.get(self.conn, self.hh, response.json()["transaction_id"])
        self.assertTrue(row["category_uncertain"])
        self.assertIsNone(row["account_id"])

    def test_revise_and_bodyless_confirm_browser_guard(self):
        draft = self.draft()
        identity = draft["request_id"]
        self.actor = self.browser
        self.assertEqual(self.edit(identity, payment_mode="one_off").status_code, 422)
        edited = self.edit(identity, expected_version=draft["row_version"], payment_mode="one_off")
        self.assertEqual(edited.status_code, 200, edited.text)
        self.actor = self.device
        self.assertEqual(self.client.post(f"/api/v1/ingestion-requests/{identity}/confirm").status_code, 409)
        # A phone revision is an explicit fresh view and restores bodyless confirmation.
        self.assertEqual(self.edit(identity, merchant="Corrected Cafe").status_code, 200)
        saved = self.client.post(f"/api/v1/ingestion-requests/{identity}/confirm", json={})
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(saved.json()["status"], "committed")
        self.assertEqual(self.client.post(f"/api/v1/ingestion-requests/{identity}/confirm", json={"expected_version": 0}).json(), saved.json())

    def test_cancel_before_delayed_post_is_a_tombstone(self):
        key = str(uuid4())
        self.assertEqual(self.client.get(f"/api/v1/ingestion-requests/by-key/{key}").status_code, 404)
        cancelled = self.client.post(f"/api/v1/ingestion-requests/by-key/{key}/cancel")
        self.assertEqual(cancelled.status_code, 200, cancelled.text)
        self.assertEqual(self.capture(key).json(), cancelled.json())
        self.assertEqual(self.model.call_count, 0)

    def test_cancel_during_extraction_wins_and_connections_are_released(self):
        key = str(uuid4())
        entered, release = threading.Event(), threading.Event()
        actual = capture_receipts.get_connection
        opened = []
        def connect(name):
            conn = actual(name)
            opened.append(conn)
            return conn
        def extract(*args, **kwargs):
            self.assertTrue(all(c.closed for c in opened))
            entered.set()
            self.assertTrue(release.wait(5))
            return self.result()
        with patch.object(capture_receipts, "get_connection", side_effect=connect), patch.object(self.model, "extract_expense", side_effect=extract):
            with ThreadPoolExecutor(1) as pool:
                pending = pool.submit(self.capture, key)
                self.assertTrue(entered.wait(5))
                try:
                    response = self.capture(key)
                    self.assertEqual(response.status_code, 202)
                    self.assertIn("Retry-After", response.headers)
                    cancelled = self.client.post(f"/api/v1/ingestion-requests/by-key/{key}/cancel")
                finally:
                    release.set()
                self.assertEqual(pending.result().json(), cancelled.json())
        self.assertEqual(repo.rows(self.conn, "SELECT count(*) AS n FROM transactions")[0]["n"], 0)

    def test_provider_failure_terminal_replay_does_not_leak_payload(self):
        key = str(uuid4())
        with patch.object(self.model, "extract_expense", side_effect=RuntimeError("secret provider body")):
            response = self.capture(key)
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("secret", response.text)
        self.assertEqual(self.capture(key).json(), response.json())
        recovered = self.client.get(f"/api/v1/ingestion-requests/by-key/{key}").json()
        self.assertEqual(recovered["status"], "failed")
        stored = repo.rows(self.conn, "SELECT * FROM ingestion_requests")[0]
        self.assertNotIn(self.image, json.dumps(stored, default=str))

    def test_intent_guards_missing_financial_fields_and_bad_image(self):
        for intent in ("transfer", "repayment", "failed", "refund"):
            self.model.set_next_result(self.result(intent=intent))
            self.assertEqual(self.capture().json()["status"], "rejected")
        for data in ({"intent": "unknown"}, {"original_currency": None}, {"original_amount": None}, {"occurred_on": None}):
            self.model.set_next_result(self.result(**data))
            response = self.capture().json()
            self.assertEqual(response["status"], "needs_confirmation")
            self.assertEqual(self.client.post(f"/api/v1/ingestion-requests/{response['request_id']}/confirm").json()["status"], "needs_confirmation")
        before = self.model.call_count
        response = self.capture(image={"mime_type": "image/png", "base64": "private-broken-image"})
        self.assertEqual(response.status_code, 422)
        self.assertNotIn("private-broken-image", response.text)
        self.assertEqual(self.model.call_count, before)

    def test_explicit_null_and_duplicate_confirmation(self):
        first = self.capture().json()
        duplicate = self.capture().json()
        self.assertEqual(duplicate["status"], "needs_confirmation")
        self.assertIn(first["transaction_id"], duplicate["draft"]["duplicate_ids"])
        identity = duplicate["request_id"]
        self.assertEqual(self.edit(identity, original_amount=None).status_code, 200)
        self.assertEqual(self.client.post(f"/api/v1/ingestion-requests/{identity}/confirm").json()["status"], "needs_confirmation")
        self.edit(identity, original_amount="28.50")
        self.assertEqual(self.client.post(f"/api/v1/ingestion-requests/{identity}/confirm").json()["status"], "committed")

    def test_natural_revision_does_not_commit(self):
        draft = self.draft()
        self.model.set_next_revision_result(ExpenseRevisionResult(payment_mode="one_off", merchant="Revised"))
        result = self.client.post(f"/api/v1/ingestion-requests/{draft['request_id']}/revise", json={"correction_note": "One-off at Revised"})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()["draft"]["merchant"], "Revised")
        self.assertEqual(repo.rows(self.conn, "SELECT count(*) AS n FROM transactions")[0]["n"], 0)

    def test_other_device_cannot_access_household_draft(self):
        draft = self.draft()
        self.actor = AuthContext("device", self.user, self.hh, "owner", device_id=uuid4())
        self.assertEqual(self.client.get(f"/api/v1/ingestion-requests/{draft['request_id']}").status_code, 404)
        self.actor = self.browser
        self.assertEqual(self.client.get(f"/api/v1/ingestion-requests/{draft['request_id']}").status_code, 200)

    def test_concurrent_confirm_returns_one_committed_transaction(self):
        draft = self.draft()
        self.edit(draft["request_id"], payment_mode="one_off")
        with ThreadPoolExecutor(2) as pool:
            responses = list(pool.map(lambda _: self.client.post(
                f"/api/v1/ingestion-requests/{draft['request_id']}/confirm"), range(2)))
        self.assertEqual([r.status_code for r in responses], [200, 200])
        self.assertEqual(responses[0].json(), responses[1].json())
        self.assertEqual(repo.rows(self.conn, "SELECT count(*) AS n FROM transactions")[0]["n"], 1)

    def test_same_text_key_is_independent_across_devices(self):
        key = str(uuid4())
        first = self.capture(key).json()
        second = schema.create_device(self.conn, self.hh, self.user, "Other phone", "ios", b"y" * 32)
        self.conn.commit()
        self.actor = AuthContext("device", self.user, self.hh, "owner", device_id=second["id"])
        other = self.capture(key).json()
        self.assertNotEqual(first["request_id"], other["request_id"])
        self.assertEqual(other["status"], "needs_confirmation")
        self.assertEqual(self.model.call_count, 2)
        self.assertEqual(self.client.get(f"/api/v1/ingestion-requests/by-key/{key}").json()["request_id"], other["request_id"])

    def test_new_duplicate_since_preview_requires_another_review(self):
        draft = self.draft()
        self.edit(draft["request_id"], payment_mode="one_off")
        self.model.set_next_result(self.result())
        other = self.capture().json()
        self.assertEqual(other["status"], "committed")
        response = self.client.post(f"/api/v1/ingestion-requests/{draft['request_id']}/confirm").json()
        self.assertEqual(response["status"], "needs_confirmation")
        self.assertIn(other["transaction_id"], response["draft"]["duplicate_ids"])
        self.assertEqual(repo.rows(self.conn, "SELECT count(*) AS n FROM transactions")[0]["n"], 1)

    def test_daily_job_and_dashboard_catch_up_share_period_identity(self):
        from app.services.spending_schedules import run_due
        from app.db import get_connection
        self.create_schedule(self.schedule_body())
        with patch("app.services.spending_schedules.local_today", return_value=date(2026, 2, 2)), ThreadPoolExecutor(2) as pool:
            job = pool.submit(run_due, lambda: get_connection(self.test_schema))
            browser = pool.submit(self.client.post, "/api/v1/spending-schedules/materialize",
                                  json={}, headers={"Idempotency-Key": str(uuid4())})
            job.result()
            response = browser.result()
            self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(repo.rows(self.conn, "SELECT count(*) AS n FROM transactions")[0]["n"], 2)
        self.assertEqual(repo.rows(self.conn, "SELECT count(*) AS n FROM schedule_occurrences")[0]["n"], 2)

    def test_quote_preparation_does_not_spend_budget_on_recorded_periods(self):
        from app.services.spending_schedules import materialize
        schedule = self.create_schedule({**self.schedule_body(), "currency": "USD", "period_count": 12})
        with patch("app.services.spending_schedules.local_today", return_value=date(2026, 6, 2)):
            materialize(self.conn, self.browser, str(uuid4()))
        class Provider:
            def __init__(self):
                self.days = []
            def fetch_quote(self, source, target, day):
                self.days.append(day)
                return None
        provider = Provider()
        with patch("app.services.spending_schedules.local_today", return_value=date(2026, 7, 2)):
            materialize(self.conn, self.browser, str(uuid4()), provider)
        self.assertEqual(provider.days, [date(2026, 7, 2)])
        self.assertEqual(repo.rows(self.conn, "SELECT count(*) AS n FROM schedule_occurrences WHERE schedule_id=%s", (schedule["id"],))[0]["n"], 7)

    def test_stale_schedule_preview_writes_no_schedule(self):
        self.actor = self.browser
        with patch("app.services.spending_schedules.local_today", return_value=date(2026, 1, 2)):
            response = self.client.post("/api/v1/spending-schedules", json=self.schedule_body(), headers={"Idempotency-Key": str(uuid4())})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "SCHEDULE_PREVIEW_STALE")
        self.assertEqual(repo.rows(self.conn, "SELECT count(*) AS n FROM spending_schedules")[0]["n"], 0)

    def test_future_amount_changes_preserve_posted_periods_and_fixed_calendar(self):
        from app.services.spending_schedules import materialize
        schedule = self.create_schedule({**self.schedule_body(), "period_count": 3, "acknowledged_due_through": "2026-01-02"})
        with patch("app.services.spending_schedules.local_today", return_value=date(2026, 2, 1)):
            rejected = self.client.patch(f"/api/v1/spending-schedules/{schedule['id']}",
                json={"expected_version": schedule["row_version"], "day_of_month": 3}, headers={"Idempotency-Key": str(uuid4())})
            self.assertEqual(rejected.status_code, 422)
            response = self.client.patch(f"/api/v1/spending-schedules/{schedule['id']}",
                json={"expected_version": schedule["row_version"], "amount_per_period": "30.00"}, headers={"Idempotency-Key": str(uuid4())})
            self.assertEqual(response.status_code, 200, response.text)
        with patch("app.services.spending_schedules.local_today", return_value=date(2026, 3, 2)):
            materialize(self.conn, self.browser, str(uuid4()))
        rows = repo.rows(self.conn, "SELECT amount FROM schedule_occurrences WHERE schedule_id=%s ORDER BY period_no", (schedule["id"],))
        self.assertEqual([r["amount"] for r in rows], [Decimal("28.50"), Decimal("30.00"), Decimal("30.00")])

    def test_inactive_schedule_category_falls_back_with_metadata_review(self):
        from app.services.spending_schedules import materialize
        category = schema.create_category(self.conn, self.hh, "Meals", "expense")
        self.conn.commit()
        self.create_schedule({**self.schedule_body(), "category_id": str(category["id"])})
        with self.conn.cursor() as cur:
            cur.execute("UPDATE categories SET status='inactive' WHERE id=%s", (category["id"],))
        self.conn.commit()
        with patch("app.services.spending_schedules.local_today", return_value=date(2026, 1, 2)):
            materialize(self.conn, self.browser, str(uuid4()))
        row = repo.rows(self.conn, "SELECT * FROM transactions")[0]
        self.assertEqual(row["category_id"], self.category)
        self.assertTrue(row["category_uncertain"])

    def test_revision_raw_output_is_never_persisted(self):
        draft = self.draft()
        self.model.set_next_revision_result(ExpenseRevisionResult(merchant="Revised", raw_response={"secret": "DO-NOT-STORE"}))
        response = self.client.post(f"/api/v1/ingestion-requests/{draft['request_id']}/revise", json={"correction_note": "Revised"})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("DO-NOT-STORE", response.text)
        self.assertNotIn("raw_response", json.dumps(schema.get_ingestion_request(self.conn, self.hh, draft["request_id"]), default=str))

    def test_invalid_model_revision_preserves_saved_draft(self):
        draft = self.draft()
        self.model.set_next_revision_result(ExpenseRevisionResult(merchant="x" * 241))
        response = self.client.post(f"/api/v1/ingestion-requests/{draft['request_id']}/revise", json={"correction_note": "Change merchant"})
        self.assertEqual(response.status_code, 503)
        current = self.client.get(f"/api/v1/ingestion-requests/{draft['request_id']}").json()
        self.assertEqual(current["draft"], draft["draft"])
        self.assertEqual(current["row_version"], draft["row_version"])

    def test_overall_deadline_after_extraction_saves_failed_receipt(self):
        key = str(uuid4())
        with patch("app.services.expense_capture.time") as clock:
            clock.monotonic.side_effect = [0, 1, 46]
            response = self.capture(key)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.client.get(f"/api/v1/ingestion-requests/by-key/{key}").json()["status"], "failed")
        self.assertEqual(self.capture(key).status_code, 503)
        self.assertEqual(self.model.call_count, 1)
        self.assertEqual(repo.rows(self.conn, "SELECT count(*) AS n FROM transactions")[0]["n"], 0)

    def test_finalize_failure_rolls_back_money_and_can_be_cancelled(self):
        key = str(uuid4())
        with patch("app.services.expense_capture.receipts.terminal", side_effect=RuntimeError("database failure")):
            with self.assertRaises(RuntimeError):
                self.capture(key)
        self.assertEqual(repo.rows(self.conn, "SELECT count(*) AS n FROM transactions")[0]["n"], 0)
        self.assertEqual(repo.rows(self.conn, "SELECT count(*) AS n FROM audit_events WHERE entity_type='transaction'")[0]["n"], 0)
        self.assertEqual(self.client.get(f"/api/v1/ingestion-requests/by-key/{key}").json()["status"], "processing")
        self.assertEqual(self.client.post(f"/api/v1/ingestion-requests/by-key/{key}/cancel").json()["status"], "rejected")

    def test_source_finalization_failure_rolls_back_schedule_and_preserves_draft(self):
        self.model.set_next_result(self.result(payment_mode="installment"))
        draft = self.capture().json()
        body = {**self.schedule_body(), "source_draft_request_id": draft["request_id"],
                "expected_draft_version": draft["row_version"], "acknowledged_due_through": "2026-01-02"}
        self.actor = self.browser
        with patch("app.services.spending_schedules.local_today", return_value=date(2026, 1, 2)), \
             patch("app.services.spending_schedules.settings.finalize_ingestion_request", side_effect=RuntimeError("database failure")):
            with self.assertRaises(RuntimeError):
                self.client.post("/api/v1/spending-schedules", json=body, headers={"Idempotency-Key": str(uuid4())})
        for table in ("transactions", "spending_schedules", "schedule_occurrences"):
            self.assertEqual(repo.rows(self.conn, f"SELECT count(*) AS n FROM {table}")[0]["n"], 0)
        current = self.client.get(f"/api/v1/ingestion-requests/{draft['request_id']}").json()
        self.assertEqual(current["row_version"], draft["row_version"])
        self.assertEqual(current["status"], "needs_confirmation")

    def test_current_payment_date_and_uncertain_historical_date(self):
        self.model.set_next_result(self.result(occurred_on=None, date_evidence="current_payment"))
        response = self.capture().json()
        row = repo.get(self.conn, self.hh, response["transaction_id"])
        self.assertEqual(row["occurred_on"], date(2026, 1, 2))
        self.assertEqual(row["date_source"], "capture_date")
        self.model.set_next_result(self.result(occurred_on=None, date_evidence="uncertain"))
        self.assertEqual(self.capture().json()["status"], "needs_confirmation")

    def schedule_body(self):
        return {"name": "Laptop", "kind": "installment", "amount_per_period": "28.50", "currency": "CNY",
                "period_count": 2, "start_month": "2026-01-01", "day_of_month": 2,
                "merchant": "Cafe", "category_id": str(self.category), "acknowledged_due_through": "2026-01-01"}

    def create_schedule(self, body):
        self.actor = self.browser
        with patch("app.services.spending_schedules.local_today", return_value=date.fromisoformat(body["acknowledged_due_through"])):
            response = self.client.post("/api/v1/spending-schedules", json=body, headers={"Idempotency-Key": str(uuid4())})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def test_capture_binds_due_period_before_job_and_job_does_not_duplicate(self):
        from app.services.spending_schedules import run_due
        from app.db import get_connection
        schedule = self.create_schedule(self.schedule_body())
        self.actor = self.device
        self.model.set_next_result(self.result(payment_mode="installment"))
        draft = self.capture().json()
        edited = self.edit(draft["request_id"], action="use_schedule_period", schedule_id=schedule["id"],
                           period_no=1, expected_schedule_version=schedule["row_version"])
        self.assertEqual(edited.status_code, 200, edited.text)
        response = self.client.post(f"/api/v1/ingestion-requests/{draft['request_id']}/confirm")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "committed")
        with patch("app.services.spending_schedules.local_today", return_value=date(2026, 1, 2)):
            self.assertEqual(run_due(lambda: get_connection(self.test_schema)), {"processed": 0})
        self.assertEqual(repo.rows(self.conn, "SELECT count(*) AS n FROM transactions")[0]["n"], 1)

    def test_source_draft_is_consumed_atomically_and_replays(self):
        self.model.set_next_result(self.result(payment_mode="installment", total_periods=2))
        draft = self.capture().json()
        self.assertEqual(self.client.post(f"/api/v1/ingestion-requests/{draft['request_id']}/confirm").json()["status"], "needs_confirmation")
        current = self.client.get(f"/api/v1/ingestion-requests/{draft['request_id']}").json()
        body = {**self.schedule_body(), "source_draft_request_id": draft["request_id"],
                "expected_draft_version": current["row_version"], "acknowledged_due_through": "2026-01-02"}
        self.actor = self.browser
        key = str(uuid4())
        with patch("app.services.spending_schedules.local_today", return_value=date(2026, 1, 2)):
            response = self.client.post("/api/v1/spending-schedules", json=body, headers={"Idempotency-Key": key})
            self.assertEqual(response.status_code, 201, response.text)
            self.assertEqual(self.client.post("/api/v1/spending-schedules", json=body, headers={"Idempotency-Key": key}).json(), response.json())
        self.actor = self.device
        old = self.client.post(f"/api/v1/ingestion-requests/{draft['request_id']}/confirm").json()
        self.assertEqual(old["status"], "rejected")
        self.assertEqual(old["schedule_id"], response.json()["id"])
        self.assertEqual(repo.rows(self.conn, "SELECT count(*) AS n FROM transactions")[0]["n"], 1)

    def test_daily_system_receipts_retry_and_disabled_creator(self):
        from app.services.spending_schedules import run_due, freshness
        from app.db import get_connection
        schedule = self.create_schedule(self.schedule_body())
        with self.conn.cursor() as cur:
            cur.execute("UPDATE users SET status='disabled' WHERE id=%s", (self.user,))
        self.conn.commit()
        with patch("app.services.spending_schedules.local_today", return_value=date(2026, 2, 2)):
            self.assertEqual(freshness(self.conn, self.hh), "2026-01-01")
            self.conn.commit()
            self.assertEqual(run_due(lambda: get_connection(self.test_schema)), {"processed": 2})
            self.assertEqual(run_due(lambda: get_connection(self.test_schema)), {"processed": 0})
            self.assertEqual(freshness(self.conn, self.hh), "2026-02-02")
        requests = repo.rows(self.conn, "SELECT * FROM ingestion_requests WHERE actor_scope=%s ORDER BY idempotency_key", (f"system:{self.hh}",))
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0]["idempotency_key"], f"schedule:{schedule['id']}:1")
        self.assertEqual(repo.rows(self.conn, "SELECT count(*) AS n FROM audit_events WHERE actor_type='system' AND entity_type='transaction'")[0]["n"], 2)

    def test_concurrent_schedule_creation_can_consume_draft_only_once(self):
        self.model.set_next_result(self.result(payment_mode="installment"))
        draft = self.capture().json()
        body = {**self.schedule_body(), "source_draft_request_id": draft["request_id"], "expected_draft_version": draft["row_version"]}
        self.actor = self.browser
        with patch("app.services.spending_schedules.local_today", return_value=date(2026, 1, 1)):
            def create(_):
                return self.client.post("/api/v1/spending-schedules", json=body, headers={"Idempotency-Key": str(uuid4())}).status_code
            with ThreadPoolExecutor(2) as pool:
                statuses = list(pool.map(create, range(2)))
        self.assertEqual(sorted(statuses), [201, 409])
        self.assertEqual(repo.rows(self.conn, "SELECT count(*) AS n FROM spending_schedules")[0]["n"], 1)

    def test_concurrent_revision_discards_late_model_result(self):
        draft = self.draft()
        entered, release = threading.Event(), threading.Event()
        def revise(*args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(5))
            return ExpenseRevisionResult(merchant="Stale")
        with patch.object(self.model, "revise_expense_draft", side_effect=revise), ThreadPoolExecutor(1) as pool:
            pending = pool.submit(self.client.post, f"/api/v1/ingestion-requests/{draft['request_id']}/revise", json={"correction_note": "Stale"})
            self.assertTrue(entered.wait(5))
            try:
                self.assertEqual(self.edit(draft["request_id"], merchant="Newer").status_code, 200)
            finally:
                release.set()
            self.assertEqual(pending.result().status_code, 409)
        self.assertEqual(self.client.get(f"/api/v1/ingestion-requests/{draft['request_id']}").json()["draft"]["merchant"], "Newer")
