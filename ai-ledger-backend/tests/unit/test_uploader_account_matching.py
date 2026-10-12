"""Account evidence and the authenticated uploader's ownership are separate inputs."""
from datetime import datetime, timezone
from unittest import TestCase
from uuid import uuid4

# Initialize auth before services, as in the API entry point (legacy import cycle).
from app.auth.context import AuthContext
from app.services.expense_capture import initial_draft, match, match_account
from app.services.gemini_service import ExpenseExtractionResult, ExpenseExtractionTransportSchema, GeminiService


class UploaderAccountMatchingTests(TestCase):
    def setUp(self):
        self.first, self.second = uuid4(), uuid4()
        self.accounts = [
            {"id": uuid4(), "name": "First Wallet", "aliases": ["Wallet"], "owner_user_id": self.first},
            {"id": uuid4(), "name": "Second Wallet", "aliases": ["Wallet"], "owner_user_id": self.second},
            {"id": uuid4(), "name": "Shared Savings", "aliases": ["Savings"], "owner_user_id": None},
        ]

    def test_shared_alias_prefers_each_uploaders_unique_account(self):
        for owner, expected in ((self.first, self.accounts[0]), (self.second, self.accounts[1])):
            actor = AuthContext("device", owner, uuid4(), "member", device_id=uuid4())
            self.assertEqual(match_account(" wallet ", self.accounts, actor.user_id), expected)

    def test_similar_names_without_a_supported_label_cannot_use_owner_as_evidence(self):
        names = [a["name"] for a in self.accounts[:2]]
        for owner in (self.first, self.second):
            self.assertIsNone(match_account("Mobile payment", self.accounts, owner, names))

    def test_multiple_own_accounts_are_ambiguous_regardless_of_order(self):
        self.accounts[1]["owner_user_id"] = self.first
        for accounts in (self.accounts, list(reversed(self.accounts))):
            self.assertIsNone(match_account("Wallet", accounts, self.first))
            self.assertIsNone(match_account(None, accounts, self.first, [a["name"] for a in self.accounts[:2]]))

    def test_distinct_other_account_evidence_wins_over_uploader(self):
        self.accounts[1]["aliases"].append("Card ending 4321")
        for label in (self.accounts[1]["name"], str(self.accounts[1]["id"]), "Card ending 4321"):
            self.assertEqual(match_account(label, self.accounts, self.first), self.accounts[1])
            self.assertEqual(match_account(label, self.accounts, self.first, [a["name"] for a in self.accounts[:2]]), self.accounts[1])

    def test_shared_name_and_alias_remain_ambiguous_with_candidates(self):
        self.accounts[0]["name"] = "Wallet"
        self.assertEqual(match_account("Wallet", self.accounts, self.second, ["Wallet", "Second Wallet"]), self.accounts[1])

    def test_model_cannot_hide_known_shared_alias_ambiguity_by_dropping_candidates(self):
        self.assertIsNone(match_account("Wallet", self.accounts, self.first, ["First Wallet"]))
        self.accounts[1]["owner_user_id"] = self.first
        self.assertIsNone(match_account("Wallet", self.accounts, self.first, ["First Wallet"]))

    def test_unassigned_accounts_remain_eligible(self):
        self.assertEqual(match_account("Savings", self.accounts, self.first), self.accounts[2])
        self.accounts[0]["owner_user_id"] = None
        self.assertIsNone(match_account("Wallet", self.accounts, self.first))
        self.assertEqual(match_account("Wallet", self.accounts, self.second), self.accounts[1])

    def test_missing_identity_or_evidence_does_not_create_a_default(self):
        for owner in (None, uuid4()):
            self.assertIsNone(match_account("Wallet", self.accounts, owner))
        for value in (None, "Unknown bank"):
            self.assertIsNone(match_account(value, self.accounts, self.first))
        self.assertIsNone(match_account("Wallet", self.accounts, self.first, []))

    def test_candidates_alone_are_not_account_evidence(self):
        for label in (None, "", "  ", "Unrecognized payment label"):
            for candidates in (["First Wallet"], ["First Wallet", "Second Wallet"]):
                with self.subTest(label=label, candidates=candidates):
                    for owner in (None, self.first, self.second):
                        self.assertIsNone(match_account(label, self.accounts, owner, candidates))

    def test_high_model_confidence_cannot_replace_missing_account_evidence(self):
        transport = ExpenseExtractionTransportSchema.model_validate({"from_account": None,
            "account_candidates": ["First Wallet"], "field_confidence": {"account": .99}})
        result = ExpenseExtractionResult.from_transport(transport)
        draft = initial_draft(result, datetime(2026, 1, 2, tzinfo=timezone.utc),
            {"timezone": "Asia/Singapore"}, self.accounts, [], self.first)
        self.assertIsNone(draft["from_account"])

    def test_invalid_or_conflicting_candidates_cannot_fall_back_to_owner(self):
        for candidates in (["First Wallet", "Outside household"], ["Outside household"], ["Second Wallet"]):
            self.assertIsNone(match_account("First Wallet", self.accounts, self.first, candidates))
        self.assertEqual(match_account("First Wallet", self.accounts, self.first, ["First Wallet", "First Wallet"]), self.accounts[0])

    def test_model_transport_preserves_candidates_and_confidence(self):
        transport = ExpenseExtractionTransportSchema.model_validate({
            "from_account": "Wallet", "account_candidates": ["First Wallet", "Second Wallet"],
            "field_confidence": {"account": .4},
        })
        result = ExpenseExtractionResult.from_transport(transport)
        draft = initial_draft(result, datetime(2026, 1, 2, tzinfo=timezone.utc),
            {"timezone": "Asia/Singapore"}, self.accounts, [], self.first)
        self.assertEqual(draft["from_account"]["id"], str(self.accounts[0]["id"]))
        self.assertEqual(draft["field_confidence"]["account"], .4)
        self.assertNotIn("account_candidates", draft)

    def test_shared_matcher_used_by_balances_and_revisions_is_unchanged(self):
        self.assertIsNone(match("Wallet", self.accounts))
        self.assertEqual(match("Savings", self.accounts), self.accounts[2])

    def test_prompt_uses_ownership_context_without_identity_secrets(self):
        accounts = [{**a, "account_type": "cash", "currency": "CNY", "ownership": ownership,
                     "owner_name": name, "email": "private@example.test"}
                    for a, ownership, name in zip(self.accounts,
                        ("uploader", "other_member", "unassigned"), ("First Person", "Second Person", None))]
        prompt = GeminiService(api_key="mock").build_system_prompt(accounts, [])
        for text in ("ownership: uploader", "ownership: other_member", "ownership: unassigned", "Second Person",
                     "List ALL compatible", "Explicit screenshot evidence", "ownership must not inflate",
                     "card suffix", "candidate names alone"):
            self.assertIn(text, prompt)
        for secret in (str(self.first), str(self.second), "private@example.test", *(str(a["id"]) for a in accounts)):
            self.assertNotIn(secret, prompt)
