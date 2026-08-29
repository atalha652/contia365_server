"""Admin dashboard snapshot from users, filings, and waitlist."""

import unittest
from datetime import datetime, timedelta

from app.services.admin_dashboard_service import AdminDashboardService


class FakeCollection:
    def __init__(self, rows):
        self.rows = rows

    def find(self, query=None, projection=None):
        class Cursor:
            def __init__(self, docs):
                self.docs = list(docs)

            def sort(self, *args, **kwargs):
                return self

            def __iter__(self):
                return iter(self.docs)

        return Cursor(self.rows)


class AdminDashboardServiceTests(unittest.TestCase):
    def test_snapshot_spain_funnel_and_excludes_admins(self):
        users = FakeCollection([
            {
                "_id": "a",
                "email": "admin@example.com",
                "role": "admin",
                "country": "ES",
                "onboarding_completed": True,
            },
            {
                "_id": "1",
                "email": "es@example.com",
                "role": "user",
                "country": "ES",
                "user_type_selection": "person",
                "onboarding_completed": True,
                "fiscal_profile_completed": True,
                "p12_encrypted": b"blob",
                "created_at": datetime.utcnow(),
            },
            {
                "_id": "2",
                "email": "it@example.com",
                "role": "user",
                "country": "IT",
                "user_type_selection": "business",
                "created_at": datetime.utcnow() - timedelta(days=20),
            },
            {
                "_id": "3",
                "email": "stuck@example.com",
                "role": "user",
                "country": "ES",
                "onboarding_completed": False,
            },
        ])
        filings = FakeCollection([
            {
                "modelo": "303",
                "status": "ACCEPTED",
                "redeme": False,
                "quarter": "Q1",
                "year": 2026,
                "submitted_at": datetime.utcnow(),
                "submission": {"reference": "AEAT-1", "test_mode": False},
            },
            {
                "modelo": "130",
                "status": "REJECTED",
                "period_key": "2026-Q1",
                "year": 2026,
                "submitted_at": datetime.utcnow(),
                "submission": {"reference": "TEST-ABC", "test_mode": True},
            },
            {
                "modelo": "303",
                "status": "DRAFT",
                "redeme": True,
                "month": 3,
                "period_key": "2026-03",
            },
        ])
        waitlist = FakeCollection([
            {"interest": "italy"},
            {"interest": "white_label"},
        ])
        snap = AdminDashboardService(users, filings, waitlist).snapshot()
        self.assertEqual(snap["users"]["total"], 3)
        self.assertEqual(snap["users"]["admins"], 1)
        self.assertEqual(snap["users"]["by_country"]["ES"], 2)
        self.assertEqual(snap["users"]["by_country"]["IT"], 1)
        self.assertEqual(snap["users"]["recent_signups_7d"], 1)
        self.assertEqual(snap["spain"]["onboarding"]["incomplete"], 1)
        self.assertEqual(snap["spain"]["certificate"]["present"], 1)
        self.assertEqual(snap["filings"]["by_status"]["ACCEPTED"], 1)
        self.assertEqual(snap["filings"]["by_modelo"]["303"], 2)
        self.assertEqual(snap["filings"]["modelo_303"]["redeme_monthly"], 1)
        self.assertEqual(snap["filings"]["submit_mode"]["test"], 1)
        self.assertEqual(snap["filings"]["submit_mode"]["live"], 1)
        self.assertEqual(snap["waitlist"]["italy"], 1)
        self.assertTrue(any(item["kind"] == "onboarding" for item in snap["queue"]))
        self.assertIn("303", snap["live_modelos"])


if __name__ == "__main__":
    unittest.main()
