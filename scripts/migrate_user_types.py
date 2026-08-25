"""One-off T13 migration: freelancer/company → person/business.

Dry-run by default. Apply with:

    py -3 scripts/migrate_user_types.py --apply
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import certifi
from dotenv import load_dotenv
from pymongo import MongoClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from app.services.user_type_vocab import (  # noqa: E402
    migrate_census_document,
    migrate_user_document,
)

USERS = "users"
CENSUS = "census_data"


def run(apply: bool) -> dict:
    mongo_uri = os.getenv("MONGO_URI")
    db_name = os.getenv("DB_NAME")
    if not mongo_uri or not db_name:
        raise SystemExit("MONGO_URI and DB_NAME must be set")

    client = MongoClient(mongo_uri, tlsCAFile=certifi.where())
    db = client[db_name]
    summary = {"users": 0, "census_data": 0, "apply": apply}

    for doc in db[USERS].find({}):
        update = migrate_user_document(doc)
        if not update:
            continue
        summary["users"] += 1
        if apply:
            db[USERS].update_one({"_id": doc["_id"]}, {"$set": update})

    for doc in db[CENSUS].find({"user_type": {"$exists": True}}):
        update = migrate_census_document(doc)
        if not update:
            continue
        summary["census_data"] += 1
        if apply:
            db[CENSUS].update_one({"_id": doc["_id"]}, {"$set": update})

    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate freelancer/company to person/business.")
    parser.add_argument("--apply", action="store_true", help="Write changes. Default is dry-run.")
    args = parser.parse_args()
    summary = run(apply=args.apply)
    mode = "applied" if summary["apply"] else "dry-run"
    print(
        f"{mode}: {summary['users']} users, {summary['census_data']} census_data rows"
    )


if __name__ == "__main__":
    main()
