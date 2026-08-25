"""T12: waitlist interest is persisted for sales."""

import unittest


class FakeWaitlistCollection:
    def __init__(self):
        self.rows = []

    def create_index(self, *args, **kwargs):
        return None

    def find_one(self, query):
        for row in self.rows:
            if all(row.get(key) == value for key, value in query.items()):
                return dict(row)
        return None

    def insert_one(self, document):
        stored = dict(document)
        stored["_id"] = f"wl-{len(self.rows) + 1}"
        self.rows.append(stored)

        class Result:
            inserted_id = stored["_id"]

        return Result()

    def find(self, query=None):
        query = query or {}
        matched = [
            dict(row)
            for row in self.rows
            if all(row.get(key) == value for key, value in query.items())
        ]

        class Cursor:
            def sort(self, *args, **kwargs):
                return matched

        return Cursor()


from app.services.waitlist_service import (
    WaitlistError,
    WaitlistService,
    can_view_sales_waitlist,
)


class WaitlistServiceTests(unittest.TestCase):
    def setUp(self):
        self.svc = WaitlistService(FakeWaitlistCollection())
        self.user = {
            "_id": "user-1",
            "email": "a@example.com",
            "name": "Ana",
            "country": "ES",
        }

    def test_join_white_label(self):
        row = self.svc.join(self.user, "white_label")
        self.assertEqual(row["interest"], "white_label")
        self.assertEqual(row["email"], "a@example.com")
        self.assertEqual(row["user_id"], "user-1")
        self.assertTrue(row["created_at"])

    def test_join_is_idempotent(self):
        first = self.svc.join(self.user, "italy")
        second = self.svc.join(self.user, "italy")
        self.assertEqual(first["_id"], second["_id"])
        self.assertEqual(len(self.svc.collection.rows), 1)

    def test_invalid_interest(self):
        with self.assertRaises(WaitlistError):
            self.svc.join(self.user, "payroll")

    def test_sales_can_list_all(self):
        self.svc.join(self.user, "white_label")
        self.svc.join({**self.user, "_id": "user-2", "email": "b@x.com"}, "italy")
        rows = self.svc.list_all()
        self.assertEqual(len(rows), 2)
        self.assertTrue(can_view_sales_waitlist({"role": "admin"}))
        self.assertTrue(can_view_sales_waitlist({"user_type_selection": "advisor"}))
        self.assertFalse(can_view_sales_waitlist({"role": "user"}))


if __name__ == "__main__":
    unittest.main()
