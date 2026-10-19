"""Account evidence and the authenticated uploader's ownership are separate inputs."""
from datetime import datetime, timezone
from unittest import TestCase
from uuid import uuid4

# Initialize auth before services, as in the API entry point (legacy import cycle).
from app.auth.context import AuthContext
from app.services.expense_capture import initial_draft, match, match_account, match_account_evidence
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
        result = ExpenseExtractionResult(from_account=None,
            account_candidates=["First Wallet"], field_confidence={"account": .99})
        draft = initial_draft(result, datetime(2026, 1, 2, tzinfo=timezone.utc),
            {"timezone": "Asia/Singapore"}, self.accounts, [], self.first)
        self.assertIsNone(draft["from_account"])

    def test_invalid_or_conflicting_candidates_cannot_fall_back_to_owner(self):
        for candidates in (["First Wallet", "Outside household"], ["Outside household"], ["Second Wallet"]):
            self.assertIsNone(match_account("First Wallet", self.accounts, self.first, candidates))
        self.assertEqual(match_account("First Wallet", self.accounts, self.first, ["First Wallet", "First Wallet"]), self.accounts[0])

    def test_model_transport_preserves_candidates_and_confidence(self):
        result = ExpenseExtractionResult(from_account="Wallet", account_candidates=["First Wallet", "Second Wallet"],
            field_confidence={"account": .4})
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
        for text in ("Second Person", "Explicit screenshot evidence", "card suffix", "visible_label",
                     "NOT confidence in identifying its owner", "NEVER replace a generic visible label"):
            self.assertIn(text, prompt)
        for secret in ("ownership: uploader", "ownership: other_member", str(self.first), str(self.second),
                       "private@example.test", *(str(a["id"]) for a in accounts)):
            self.assertNotIn(secret, prompt)


class PaymentAccountEvidenceTests(TestCase):
    def setUp(self):
        self.first, self.second = uuid4(), uuid4()
        self.accounts = [
            {"id": uuid4(), "name": "Wallet-A", "aliases": ["零钱"], "owner_user_id": self.first},
            {"id": uuid4(), "name": "Wallet-B", "aliases": ["零钱"], "owner_user_id": self.second},
        ]
        self.label = {"visible_label": "零钱", "label_confidence": .99}

    def draft(self, evidence, **legacy):
        result = ExpenseExtractionResult(account_evidence=evidence, **legacy)
        return initial_draft(result, datetime(2026, 1, 2, tzinfo=timezone.utc),
            {"timezone": "Asia/Singapore"}, self.accounts, [], self.first)

    def test_literal_shared_label_ignores_wrong_model_selection_and_owner_confidence(self):
        draft = self.draft(self.label, from_account="Wallet-B", account_candidates=["Wallet-B"],
                           field_confidence={"account": .4, "amount": .99})
        self.assertEqual(draft["from_account"]["id"], str(self.accounts[0]["id"]))
        self.assertEqual(draft["field_confidence"]["account"], .99)
        self.assertEqual(draft["field_confidence"]["amount"], .99)
        self.assertNotIn("account_evidence", draft)

    def test_every_configured_alias_participates_independent_of_order(self):
        for accounts in (self.accounts, list(reversed(self.accounts))):
            for owner, expected in zip((self.first, self.second), self.accounts):
                account, conf = match_account_evidence(self.label, accounts, owner)
                self.assertEqual(account, expected)
                self.assertEqual(conf, .99)
        self.accounts[1]["owner_user_id"] = self.first
        self.assertIsNone(self.draft(self.label)["from_account"])

    def test_missing_unrecognized_or_uncertain_label_does_not_fall_back_to_guess(self):
        for evidence in ({}, {"visible_label": "Unknown", "label_confidence": .99},
                         {"visible_label": "零钱"}, {**self.label, "label_confidence": .84}):
            with self.subTest(evidence=evidence):
                draft = self.draft(evidence, from_account="Wallet-A", field_confidence={"account": .99})
                self.assertIsNone(draft["from_account"])
        self.assertIsNotNone(self.draft({**self.label, "label_confidence": .85})["from_account"])

    def test_missing_model_evidence_has_no_legacy_fallback(self):
        result = ExpenseExtractionResult.from_transport(ExpenseExtractionTransportSchema())
        self.assertIsNotNone(result.account_evidence)
        result.from_account = "Wallet-A"
        result.field_confidence["account"] = .99
        draft = initial_draft(result, datetime(2026, 1, 2, tzinfo=timezone.utc),
            {"timezone": "Asia/Singapore"}, self.accounts, [], self.first)
        self.assertIsNone(draft["from_account"])

    def test_explicit_other_member_identifier_takes_priority(self):
        for kind, text in (("card_suffix", "尾号4321"), ("holder_name", "Second Person"),
                           ("account_name", "Wallet-B")):
            evidence = {**self.label, "distinctive_kind": kind, "distinctive_text": text,
                        "matched_account_name": "Wallet-B", "distinctive_confidence": .99}
            self.assertEqual(self.draft(evidence)["from_account"]["id"], str(self.accounts[1]["id"]))

    def test_generic_alias_cannot_masquerade_as_distinctive_evidence(self):
        for kind in ("account_name", "card_suffix", "holder_name"):
            evidence = {**self.label, "distinctive_kind": kind, "distinctive_text": "零钱",
                        "matched_account_name": "Wallet-B", "distinctive_confidence": .99}
            self.assertIsNone(self.draft(evidence)["from_account"])

    def test_incomplete_uncertain_or_conflicting_identifier_keeps_account_unknown(self):
        valid = {**self.label, "distinctive_kind": "card_suffix", "distinctive_text": "尾号4321",
                 "matched_account_name": "Wallet-B", "distinctive_confidence": .99}
        for changes in ({"distinctive_kind": None}, {"distinctive_text": " "},
                        {"matched_account_name": None}, {"matched_account_name": "Outside household"},
                        {"distinctive_confidence": .84}, {"distinctive_confidence": None},
                        {"visible_label": "Wallet-A"}, {"distinctive_text": "Wallet-A"},
                        {"distinctive_kind": "account_name"}):
            with self.subTest(changes=changes):
                self.assertIsNone(self.draft({**valid, **changes})["from_account"])

    def test_unassigned_and_missing_uploader_remain_conservative(self):
        for uploader in (None, uuid4()):
            self.assertIsNone(match_account_evidence(self.label, self.accounts, uploader)[0])
        self.accounts[1]["owner_user_id"] = None
        self.assertIsNone(match_account_evidence(self.label, self.accounts, self.second)[0])
        self.accounts[1]["aliases"] = ["Shared cash"]
        evidence = {"visible_label": "Shared cash", "label_confidence": .99}
        self.assertEqual(match_account_evidence(evidence, self.accounts, self.first)[0], self.accounts[1])

    def test_holder_name_cannot_hide_multiple_matching_accounts(self):
        for account in self.accounts:
            account["owner_name"] = "Same Holder"
        evidence = {**self.label, "distinctive_kind": "holder_name", "distinctive_text": "Same Holder",
                    "matched_account_name": "Wallet-B", "distinctive_confidence": .99}
        self.assertIsNone(self.draft(evidence)["from_account"])
        self.accounts[0]["aliases"] = ["Different wallet"]
        self.assertEqual(self.draft(evidence)["from_account"]["id"], str(self.accounts[1]["id"]))

    def test_local_validation_rejects_bad_evidence_without_relaxing_financial_confidence(self):
        from pydantic import ValidationError
        for evidence in ({**self.label, "label_confidence": 1.1},
                         {**self.label, "distinctive_confidence": -.1},
                         {**self.label, "distinctive_kind": "guessed_owner"},
                         {**self.label, "unexpected": "value"}):
            with self.subTest(evidence=evidence), self.assertRaises(ValidationError):
                ExpenseExtractionTransportSchema.model_validate({"account_evidence": evidence})
        transport = ExpenseExtractionTransportSchema.model_validate({"account_evidence": self.label,
            "field_confidence": {"amount": .4}})
        result = ExpenseExtractionResult.from_transport(transport)
        draft = initial_draft(result, datetime(2026, 1, 2, tzinfo=timezone.utc),
            {"timezone": "Asia/Singapore"}, self.accounts, [], self.first)
        self.assertEqual(draft["field_confidence"], {"account": .99, "amount": .4})
