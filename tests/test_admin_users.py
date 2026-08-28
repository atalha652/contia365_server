"""Admin user directory: country and user-type filters."""

import unittest
from datetime import datetime

from app.services.admin_users_service import (
    AdminUsersService,
    is_admin,
    serialize_user,
)


class FakeUsersCollection:
    def __init__(self, rows):
        self.rows = rows

    def find(self, query=None, projection=None):
        class Cursor:
            def __init__(self, docs):
                self.docs = list(docs)

            def sort(self, *args, **kwargs):
                return self.docs

            def __iter__(self):
                return iter(self.docs)

        return Cursor(self.rows)


class AdminUsersServiceTests(unittest.TestCase):
    def setUp(self):
        self.rows = [
            {
                "_id": "1",
                "name": "Spain Person",
                "email": "es-p@example.com",
                "country": "ES",
                "user_type_selection": "person",
                "role": "user",
                "password_hash": "secret",
                "p12_encrypted": b"blob",
                "created_at": datetime(2026, 1, 1),
            },
            {
                "_id": "2",
                "name": "Italy Business",
                "email": "it-b@example.com",
                "country": "IT",
                "user_type_selection": "company",
                "role": "user",
            },
            {
                "_id": "3",
                "name": "Incomplete",
                "email": "none@example.com",
                "role": "user",
            },
            {
                "_id": "4",
                "name": "Admin",
                "email": "admin@example.com",
                "country": "spain",
                "user_type_selection": "advisor",
                "role": "admin",
            },
        ]
        self.svc = AdminUsersService(FakeUsersCollection(self.rows))

    def test_is_admin(self):
        self.assertTrue(is_admin({"role": "admin"}))
        self.assertFalse(is_admin({"role": "user"}))
        self.assertFalse(is_admin({"user_type_selection": "advisor"}))

    def test_serialize_strips_secrets(self):
        row = serialize_user(self.rows[0])
        self.assertNotIn("password_hash", row)
        self.assertNotIn("p12_encrypted", row)
        self.assertEqual(row["country"], "ES")
        self.assertEqual(row["user_type"], "person")

    def test_list_all(self):
        result = self.svc.list()
        self.assertEqual(result["total"], 4)
        self.assertEqual(len(result["users"]), 4)
        self.assertEqual(result["page"], 1)
        self.assertEqual(result["page_size"], 10)
        self.assertEqual(result["total_pages"], 1)
        self.assertFalse(result["has_next"])

    def test_pagination_max_10(self):
        extra = [
            {
                "_id": str(i),
                "name": f"User {i}",
                "email": f"u{i}@example.com",
                "role": "user",
            }
            for i in range(5, 16)
        ]
        svc = AdminUsersService(FakeUsersCollection(self.rows + extra))
        page1 = svc.list(page=1, page_size=50)
        self.assertEqual(page1["page_size"], 10)
        self.assertEqual(len(page1["users"]), 10)
        self.assertEqual(page1["total"], 15)
        self.assertEqual(page1["total_pages"], 2)
        self.assertTrue(page1["has_next"])

        page2 = svc.list(page=2)
        self.assertEqual(len(page2["users"]), 5)
        self.assertFalse(page2["has_next"])
        self.assertTrue(page2["has_prev"])

        page3 = svc.list(page=3)
        self.assertEqual(page3["users"], [])
        self.assertEqual(page3["total"], 15)

    def test_filter_country_es(self):
        result = self.svc.list(country="ES")
        emails = {u["email"] for u in result["users"]}
        self.assertEqual(emails, {"es-p@example.com", "admin@example.com"})

    def test_filter_country_italy_alias(self):
        result = self.svc.list(country="italy")
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["users"][0]["email"], "it-b@example.com")

    def test_filter_unset_country(self):
        result = self.svc.list(country="unset")
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["users"][0]["email"], "none@example.com")

    def test_filter_user_type_business_alias(self):
        result = self.svc.list(user_type="business")
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["users"][0]["user_type"], "business")

    def test_filter_person_and_spain(self):
        result = self.svc.list(country="ES", user_type="person")
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["users"][0]["email"], "es-p@example.com")

    def test_invalid_country(self):
        with self.assertRaises(ValueError):
            self.svc.list(country="FR")


if __name__ == "__main__":
    unittest.main()
