"""T13: one onboarding vocabulary — person / business (+ legacy advisor)."""

import unittest

from app.models.onboarding import OnboardingRequest, UserTypeSelection, USER_TYPE_CATALOG
from app.services.fiscal_profile_service import derive_periodic_tax_obligations
from app.services.user_type_vocab import (
    BUSINESS,
    PERSON,
    canonicalize_user_type,
    is_person_user_type,
    migrate_census_document,
    migrate_user_config,
    migrate_user_document,
    stored_account_kind,
)


class UserTypeVocabTests(unittest.TestCase):
    def test_aliases_map_to_person_and_business(self):
        self.assertEqual(canonicalize_user_type("freelancer"), PERSON)
        self.assertEqual(canonicalize_user_type("autonomo"), PERSON)
        self.assertEqual(canonicalize_user_type("person"), PERSON)
        self.assertEqual(canonicalize_user_type("company"), BUSINESS)
        self.assertEqual(canonicalize_user_type("empresa"), BUSINESS)
        self.assertEqual(canonicalize_user_type("business"), BUSINESS)
        self.assertEqual(canonicalize_user_type("advisor"), "advisor")
        self.assertIsNone(canonicalize_user_type("white_label"))

    def test_enum_accepts_legacy_ids(self):
        self.assertEqual(UserTypeSelection("freelancer"), UserTypeSelection.PERSON)
        self.assertEqual(UserTypeSelection("company"), UserTypeSelection.BUSINESS)
        req = OnboardingRequest(user_type="freelancer")
        self.assertEqual(req.user_type, UserTypeSelection.PERSON)
        req = OnboardingRequest(user_type="company")
        self.assertEqual(req.user_type, UserTypeSelection.BUSINESS)

    def test_catalog_ids_are_person_and_business(self):
        self.assertEqual(USER_TYPE_CATALOG[UserTypeSelection.PERSON].id, "person")
        self.assertEqual(USER_TYPE_CATALOG[UserTypeSelection.BUSINESS].id, "business")
        self.assertEqual(USER_TYPE_CATALOG[UserTypeSelection.PERSON].name, "Person")
        self.assertEqual(USER_TYPE_CATALOG[UserTypeSelection.BUSINESS].name, "Business")

    def test_fiscal_130_uses_person_alias(self):
        profile = {
            "user_type": "freelancer",
            "professional_registration": {
                "vat_regime": "exempt",
                "economic_activities": [{"code": "799"}],
            },
        }
        modelos = {item["modelo"] for item in derive_periodic_tax_obligations(profile)}
        self.assertIn("130", modelos)
        self.assertTrue(is_person_user_type("freelancer"))
        self.assertTrue(is_person_user_type("person"))
        self.assertFalse(is_person_user_type("company"))

    def test_stored_account_kind_and_config_migration(self):
        self.assertEqual(stored_account_kind("freelancer"), "individual")
        self.assertEqual(stored_account_kind("company"), "organization")
        self.assertEqual(stored_account_kind("advisor"), "organization")
        config = migrate_user_config({
            "dashboard_layout": "freelancer",
            "chart_of_accounts": "company_coa",
            "tax_regime": "company",
        })
        self.assertEqual(config["dashboard_layout"], "person")
        self.assertEqual(config["chart_of_accounts"], "business_coa")
        self.assertEqual(config["tax_regime"], "business")
        update = migrate_user_document({
            "user_type_selection": "freelancer",
            "type": "freelancer",
            "organization_info": {"type": "company"},
            "user_config": {"dashboard_layout": "company"},
        })
        self.assertEqual(update["user_type_selection"], "person")
        self.assertEqual(update["type"], "individual")
        self.assertEqual(update["organization_info.type"], "business")
        self.assertEqual(update["user_config"]["dashboard_layout"], "business")
        self.assertEqual(migrate_census_document({"user_type": "company"}), {"user_type": "business"})
        self.assertEqual(migrate_census_document({"user_type": "person"}), {})
        self.assertEqual(
            migrate_user_document({"user_type_selection": "person", "type": "individual"}),
            {},
        )

    def test_status_rewrites_freelancer_to_person(self):
        from app.services.onboarding_status import persist_computed_onboarding

        class Users:
            def __init__(self):
                self.sets = []

            def update_one(self, _query, update):
                self.sets.append(update["$set"])

        class Census:
            def find_one(self, *args, **kwargs):
                return None

        users = Users()
        user = {"_id": "u1", "country": "IT", "user_type_selection": "freelancer"}
        status = persist_computed_onboarding(users, Census(), user)
        self.assertEqual(status["user_type_selected"], "person")
        self.assertEqual(users.sets[0]["user_type_selection"], "person")


if __name__ == "__main__":
    unittest.main()
