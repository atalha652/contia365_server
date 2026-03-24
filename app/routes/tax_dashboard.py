import os
from dotenv import load_dotenv
from fastapi import APIRouter, HTTPException
from motor.motor_asyncio import AsyncIOMotorClient

from app.models.tax_dashboard import TaxDeadlineResponse


load_dotenv()

MONGO_URI = os.getenv("MONGO_URI")
DB_NAME = os.getenv("DB_NAME")

if not MONGO_URI or not DB_NAME:
    raise RuntimeError("MONGO_URI and DB_NAME must be set in environment variables.")

_motor_client = AsyncIOMotorClient(MONGO_URI)
_db = _motor_client[DB_NAME]

router = APIRouter(tags=["Tax Dashboard"])


@router.get("/tax-dashboard/{user_id}", response_model=TaxDeadlineResponse)
async def get_tax_dashboard_deadline(user_id: str) -> TaxDeadlineResponse:
    """
    Detect the most recent census document for a user, extract the Modelo number,
    join with modelos, and return the deadline details.
    """
    modelo_detection_regex = r"(?i)\bmodelo\W*(\d{2,4})\b"

    pipeline = [
        {"$match": {"user_id": user_id}},
        {
            "$match": {
                "$expr": {
                    "$regexMatch": {
                        "input": {"$ifNull": ["$document_metadata.document_type", ""]},
                        "regex": modelo_detection_regex,
                    }
                }
            }
        },
        {"$sort": {"created_at": -1, "updated_at": -1, "_id": -1}},
        {"$limit": 1},
        {
            "$addFields": {
                "_modelo_match": {
                    "$regexFind": {
                        "input": {"$ifNull": ["$document_metadata.document_type", ""]},
                        "regex": modelo_detection_regex,
                    }
                }
            }
        },
        {"$addFields": {"modelo_no": {"$arrayElemAt": ["$_modelo_match.captures", 0]}}},
        {
            "$lookup": {
                "from": "modelos",
                "localField": "modelo_no",
                "foreignField": "modelo_no",
                "as": "modelo",
            }
        },
        {"$unwind": {"path": "$modelo", "preserveNullAndEmptyArrays": True}},
        {
            "$project": {
                "_id": 0,
                "modelo_no": "$modelo.modelo_no",
                "name": "$modelo.name",
                "deadline": "$modelo.deadline",
                "_extracted_modelo_no": "$modelo_no",
            }
        },
    ]

    results = await _db["census_data"].aggregate(pipeline).to_list(length=1)
    if not results:
        raise HTTPException(
            status_code=404,
            detail="No census data with a Modelo number found for this user.",
        )

    row = results[0]
    extracted = row.get("_extracted_modelo_no")
    if not extracted:
        raise HTTPException(
            status_code=422,
            detail="Could not extract modelo number from document type (expected 'Modelo <digits>').",
        )

    if not row.get("modelo_no") or not row.get("name") or not row.get("deadline"):
        raise HTTPException(
            status_code=404,
            detail=f"Modelo '{extracted}' not found in modelos collection (or missing fields).",
        )

    return TaxDeadlineResponse(
        modelo_no=row["modelo_no"],
        name=row["name"],
        deadline=row["deadline"],
    )

