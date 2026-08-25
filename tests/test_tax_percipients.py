"""T14: percipient CRUD used by 111 / 190."""

import unittest

from app.models.tax_percipient import PercipientCreate
from app.services.tax_percipient_service import TaxPercipientService


class InMemoryPercipients:
    def __init__(self):
        self.rows = []

    def create(self, document):
        document = dict(document)
        document["_id"] = f"id-{len(self.rows)+1}"
        self.rows.append(document)
        return dict(document)

    def get(self, row_id, user_id):
        for row in self.rows:
            if str(row["_id"]) == str(row_id) and row["user_id"] == str(user_id):
                return dict(row)
        return None

    def list(self, user_id, year=None, quarter=None):
        out = []
        for row in self.rows:
            if row["user_id"] != str(user_id):
                continue
            if year is not None and row.get("year") != year:
                continue
            if quarter and row.get("quarter") != quarter:
                continue
            out.append(dict(row))
        return out

    def update(self, row_id, user_id, values):
        for i, row in enumerate(self.rows):
            if str(row["_id"]) == str(row_id) and row["user_id"] == str(user_id):
                row = {**row, **values}
                self.rows[i] = row
                return dict(row)
        return None

    def delete(self, row_id, user_id):
        before = len(self.rows)
        self.rows = [
            row for row in self.rows
            if not (str(row["_id"]) == str(row_id) and row["user_id"] == str(user_id))
        ]
        return len(self.rows) < before


class PercipientServiceTests(unittest.TestCase):
    def test_create_and_list_by_quarter(self):
        svc = TaxPercipientService(repo=InMemoryPercipients())
        user = {"_id": "user-1"}
        created = svc.create(user, PercipientCreate(
            nif="12345678z",
            full_name="Worker",
            perception_key="a",
            year=2026,
            quarter="q1",
            base_amount=1000,
            withheld_amount=150,
            kind="employee",
        ))
        self.assertEqual(created["nif"], "12345678Z")
        self.assertEqual(created["perception_key"], "A")
        self.assertEqual(created["quarter"], "Q1")
        listed = svc.list(user, year=2026, quarter="Q1")
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["withheld_amount"], 150)


if __name__ == "__main__":
    unittest.main()
