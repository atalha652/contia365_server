from typing import Optional
from fastapi import APIRouter, File, Depends, HTTPException
from pydantic import BaseModel, EmailStr
from pymongo import MongoClient
from bson import ObjectId
from dotenv import load_dotenv
import os
import certifi
from fastapi import APIRouter, HTTPException
from app.routes.auth import get_current_user
from pymongo import MongoClient
from datetime import datetime
import certifi
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from datetime import datetime
from fastapi import APIRouter, File, UploadFile, Depends, Form, HTTPException, UploadFile
from datetime import datetime
from bson import ObjectId
import boto3
import pytesseract
import os
from datetime import datetime
from pydantic import BaseModel, Field
from typing import Optional
from typing import List, Optional
from fastapi import APIRouter, File, UploadFile, Form, Depends, HTTPException
from datetime import datetime
from fastapi import Query
from fastapi.responses import FileResponse
from app.utils.period_guard import validate_upload_window
# Load env variables
load_dotenv()

pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"

# Font config for WeasyPrint (if needed)
os.environ["FONTCONFIG_FILE"] = r"C:\OCR Project\fonts\fonts.conf"


router = APIRouter(prefix="/accounting/voucher", tags=["vouchers"])

MONGO_URI = os.getenv("MONGO_URI")
DB_NAME = os.getenv("DB_NAME")
client = MongoClient(MONGO_URI, tlsCAFile=certifi.where())
db = client[DB_NAME]
voucher_collection = db["voucher"]
ocr_collection = db["ocr"]  # Replace 'db' with your actual DB object
AWS_ACCESS_KEY = os.getenv("AWS_ACCESS_KEY_ID")
AWS_SECRET_KEY = os.getenv("AWS_SECRET_ACCESS_KEY")
OPENAI_KEY = os.getenv("OPENAI_API_KEY")


s3 = boto3.client(
    "s3",
    aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
    aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
   region_name="eu-north-1"
)

bucket_name = "ai-auto-invoice"



def upload_to_s3(user_id, project_id, file: UploadFile, folder_type="Package"):
    """Uploads file to S3 in Images/Package or Images/Result folder."""
    if folder_type not in ["Package", "Result"]:
        raise ValueError("folder_type must be 'Package' or 'Result'")

    s3_folder = f"{user_id}/{project_id}/Images/{folder_type}/"

    # Save temporarily
    temp_path = f"{file.filename}"
    with open(temp_path, "wb") as buffer:
        buffer.write(file.file.read())

    # Upload to S3
    content_type = file.content_type
    s3.upload_file(
        Filename=temp_path,
        Bucket=bucket_name,
        Key=f"{s3_folder}{file.filename}",
        ExtraArgs={
            "ContentType": content_type,
            "ContentDisposition": "inline"
        }
    )

    # Remove temp file
    os.remove(temp_path)

    return f"{s3_folder}{file.filename}"

@router.post("/upload")
async def upload_voucher(
    user_id: str = Form(..., description="User ID of the person uploading the voucher"),
    files: List[UploadFile] = File(...),   # Accept multiple files
    title: Optional[str] = Form(None, description="Optional title for the voucher"),
    description: Optional[str] = Form(None, description="Optional description for the voucher"),
    category: Optional[str] = Form(None, description="Optional category name for the voucher"),
    transaction_type: Optional[str] = Form(None, description="Transaction type: 'credit' or 'debit'"),
    period: str = Depends(validate_upload_window),
):
    # Step 1: Validate all files
    for file in files:
        if file.content_type not in ["image/png", "image/jpeg", "application/pdf"]:
            raise HTTPException(status_code=400, detail="Only image or PDF allowed")
    
    # Step 1.5: Validate transaction_type if provided
    if transaction_type and transaction_type not in ["credit", "debit"]:
        raise HTTPException(status_code=400, detail="transaction_type must be either 'credit' or 'debit'")

    # Step 2: Create new voucher record with status "pending"
    new_voucher = {
        "user_id": user_id,
        "status": "pending",
        "OCR": "pending",
        "period": period,
        "created_at": datetime.utcnow(),
        "files": []
    }
    
    # Add optional fields if provided
    if title:
        new_voucher["title"] = title
    if description:
        new_voucher["description"] = description
    if category:
        new_voucher["category"] = category
    if transaction_type:
        new_voucher["transaction_type"] = transaction_type
    
    result = voucher_collection.insert_one(new_voucher)
    voucher_id = str(result.inserted_id)

    # Step 3: Upload each file to S3
    file_records = []
    for file in files:
        s3_key = upload_to_s3(
            user_id=user_id,
            project_id=voucher_id,
            file=file,
            folder_type="Package"
        )
        file_url = s3.generate_presigned_url(
            'get_object',
            Params={
                'Bucket': bucket_name,
                'Key': s3_key,
                'ResponseContentType': file.content_type
            },
            ExpiresIn=86400  # 24 hours
        )
        file_records.append({
            "name": file.filename,
            "file_url": file_url,
            "s3_key": s3_key
        })

    # Step 4: Update voucher with uploaded file info
    voucher_collection.update_one(
        {"_id": result.inserted_id},
        {"$set": {"files": file_records}}
    )

    # Step 5: Return response
    response = {
        "message": "Voucher uploaded successfully",
        "voucher_id": voucher_id,
        "user_id": user_id,
        "period": period,
        "files": file_records,
        "status": "pending",
        "OCR": "pending"
    }
    
    # Include optional fields in response if provided
    if title:
        response["title"] = title
    if description:
        response["description"] = description
    if category:
        response["category"] = category
    if transaction_type:
        response["transaction_type"] = transaction_type
    
    return response


@router.get("/awaiting-approval")
async def get_awaiting_approval_vouchers(
    user_id: str = Query(..., description="User ID to fetch vouchers for")
):
    """
    Get all vouchers for a specific user with status 'awaiting_approval'.
    Example: GET /accounting/voucher/awaiting-approval?user_id=123
    """
    query = {
        "user_id": user_id,
        "status": "awaiting_approval"
    }

    vouchers = list(voucher_collection.find(query))

    if not vouchers:
        raise HTTPException(status_code=404, detail="No vouchers found with status 'awaiting_approval'")

    # Convert ObjectId and datetime for readability
    for voucher in vouchers:
        voucher["_id"] = str(voucher["_id"])
        if "created_at" in voucher:
            voucher["created_at"] = voucher["created_at"].strftime("%Y-%m-%d %H:%M:%S")
        if "approval_requested_at" in voucher:
            voucher["approval_requested_at"] = voucher["approval_requested_at"].strftime("%Y-%m-%d %H:%M:%S")
        if "updated_at" in voucher:
            voucher["updated_at"] = voucher["updated_at"].strftime("%Y-%m-%d %H:%M:%S")
        # Ensure rejection_count is included (default to 0 if not present)
        if "rejection_count" not in voucher:
            voucher["rejection_count"] = 0

    return {
        "count": len(vouchers),
        "vouchers": vouchers
    }


@router.get("/approved")
async def get_approved_vouchers(
    user_id: str = Query(..., description="User ID to fetch vouchers for")
):
    """
    Get all vouchers for a specific user with status 'approved'.
    Example: GET /accounting/voucher/approved?user_id=123
    """
    query = {
        "user_id": user_id,
        "status": "approved"
    }

    vouchers = list(voucher_collection.find(query).sort("approved_at", -1))

    if not vouchers:
        raise HTTPException(status_code=404, detail="No vouchers found with status 'approved'")

    # Convert ObjectId and datetime for readability
    for voucher in vouchers:
        voucher["_id"] = str(voucher["_id"])
        if "created_at" in voucher:
            voucher["created_at"] = voucher["created_at"].strftime("%Y-%m-%d %H:%M:%S")
        if "approval_requested_at" in voucher:
            voucher["approval_requested_at"] = voucher["approval_requested_at"].strftime("%Y-%m-%d %H:%M:%S")
        if "approved_at" in voucher:
            voucher["approved_at"] = voucher["approved_at"].strftime("%Y-%m-%d %H:%M:%S")
        if "updated_at" in voucher:
            voucher["updated_at"] = voucher["updated_at"].strftime("%Y-%m-%d %H:%M:%S")
        if "ocr_completed_at" in voucher:
            voucher["ocr_completed_at"] = voucher["ocr_completed_at"].strftime("%Y-%m-%d %H:%M:%S")

    return {
        "count": len(vouchers),
        "vouchers": vouchers
    }


@router.get("/awaiting-approval")
async def get_awaiting_approval_vouchers(
    user_id: str = Query(..., description="User ID to fetch vouchers for")
):
    """
    Get all vouchers for a specific user with status 'awaiting_approval'.
    Example: GET /accounting/voucher/awaiting-approval?user_id=123
    """
    query = {
        "user_id": user_id,
        "status": "awaiting_approval"
    }

    vouchers = list(voucher_collection.find(query))

    if not vouchers:
        raise HTTPException(status_code=404, detail="No vouchers found with status 'awaiting_approval'")

    # Convert ObjectId and datetime for readability
    for voucher in vouchers:
        voucher["_id"] = str(voucher["_id"])
        if "created_at" in voucher:
            voucher["created_at"] = voucher["created_at"].strftime("%Y-%m-%d %H:%M:%S")
        if "approval_requested_at" in voucher:
            voucher["approval_requested_at"] = voucher["approval_requested_at"].strftime("%Y-%m-%d %H:%M:%S")
        if "updated_at" in voucher:
            voucher["updated_at"] = voucher["updated_at"].strftime("%Y-%m-%d %H:%M:%S")

    return {
        "count": len(vouchers),
        "vouchers": vouchers
    }


@router.get("/{voucher_id}")
async def get_voucher_by_id(
    voucher_id: str,
    user_id: Optional[str] = Query(None, description="Optional user ID to verify ownership")
):
    """
    Get a specific voucher by its ID.
    Example: GET /accounting/vouchers/68f880bcadf2e0b66e482d11?user_id=68a46f1d960572d49facd776
    """
    try:
        # Convert string to ObjectId
        obj_id = ObjectId(voucher_id)
        query = {"_id": obj_id}
        
        # Debug: Check if voucher exists without user_id filter first
        voucher_exists = voucher_collection.find_one({"_id": obj_id})
        
        # Optionally filter by user_id if provided
        if user_id:
            query["user_id"] = user_id
        
        voucher = voucher_collection.find_one(query)
        
        if not voucher:
            if voucher_exists:
                raise HTTPException(
                    status_code=403, 
                    detail=f"Voucher exists but user_id mismatch. Voucher user_id: {voucher_exists.get('user_id')}"
                )
            else:
                raise HTTPException(status_code=404, detail=f"Voucher not found with ID: {voucher_id}")
        
        # Convert ObjectId and datetime for readability
        voucher["_id"] = str(voucher["_id"])
        if "created_at" in voucher:
            voucher["created_at"] = voucher["created_at"].strftime("%Y-%m-%d %H:%M:%S")
        
        return voucher
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid voucher ID format: {str(e)}")


@router.get("/")
async def get_vouchers(
    user_id: str = Query(..., description="User ID to fetch vouchers for")
):
    """
    Get all vouchers for a specific user with status 'pending' or 'rejected'.
    Example: GET /accounting/voucher?user_id=123
    """
    query = {
        "user_id": user_id,
        "status": {"$in": ["pending", "rejected"]}
    }

    vouchers = list(voucher_collection.find(query))

    if not vouchers:
        raise HTTPException(status_code=404, detail="No vouchers found with status 'pending' or 'rejected'")

    # Convert ObjectId and datetime for readability
    for voucher in vouchers:
        voucher["_id"] = str(voucher["_id"])
        if "created_at" in voucher:
            voucher["created_at"] = voucher["created_at"].strftime("%Y-%m-%d %H:%M:%S")

    return {
        "count": len(vouchers),
        "vouchers": vouchers
    }


# Pydantic models for request bodies
class ApprovalRequest(BaseModel):
    approver_id: str = Field(..., description="ID of the user who will approve")
    voucher_ids: List[str] = Field(..., description="List of voucher IDs to approve")
    notes: Optional[str] = Field(None, description="Approval notes")


class BulkApprovalRequest(BaseModel):
    voucher_ids: List[str] = Field(..., description="List of voucher IDs to send for approval")
    approver_id: str = Field(..., description="ID of the user who will approve")

class RejectionRequest(BaseModel):
    rejected_by: str = Field(..., description="ID of the user rejecting the voucher")
    rejection_reason: str = Field(..., description="Reason for rejection")
    voucher_ids: List[str] = Field(..., description="List of voucher IDs to reject")


class ClassificationRequest(BaseModel):
    document_type: Optional[str] = Field(None, description="Manual document type (supplier_invoice, expense, receipt, purchase_order, credit_note, etc.)")
    use_ai: bool = Field(False, description="Use AI to auto-classify the document")


class ForwardRequest(BaseModel):
    current_approver_id: str = Field(..., description="ID of the current approver forwarding the voucher")
    new_approver_id: str = Field(..., description="ID of the new approver to forward to")
    reason: Optional[str] = Field(None, description="Reason for forwarding")


@router.post("/send-for-request")
async def send_multiple_for_approval(
    approval_data: BulkApprovalRequest
):
    """
    Send multiple vouchers for approval in bulk.
    Changes status to 'awaiting_approval' and assigns an approver for all specified vouchers.
    
    Example: POST /accounting/voucher/bulk/approve-request
    Body: {
        "voucher_ids": ["68f880bcadf2e0b66e482d11", "68f880bcadf2e0b66e482d12"],
        "approver_id": "123"
    }
    """
    try:
        results = {
            "successful": [],
            "failed": []
        }
        
        for voucher_id in approval_data.voucher_ids:
            try:
                obj_id = ObjectId(voucher_id)
                
                # Check if voucher exists
                voucher = voucher_collection.find_one({"_id": obj_id})
                if not voucher:
                    results["failed"].append({
                        "voucher_id": voucher_id,
                        "reason": "Voucher not found"
                    })
                    continue
                
                # Check if voucher is in a valid state for approval request
                current_status = voucher.get("status")
                if current_status == "approved":
                    results["failed"].append({
                        "voucher_id": voucher_id,
                        "reason": f"Voucher is already approved"
                    })
                    continue
                
                # Update voucher with approval request details
                update_data = {
                    "status": "awaiting_approval",
                    "approver_id": approval_data.approver_id,
                    "approval_requested_at": datetime.utcnow(),
                    "updated_at": datetime.utcnow()
                }
                
                result = voucher_collection.update_one(
                    {"_id": obj_id},
                    {"$set": update_data}
                )
                
                if result.modified_count > 0:
                    results["successful"].append({
                        "voucher_id": voucher_id,
                        "status": "awaiting_approval"
                    })
                else:
                    results["failed"].append({
                        "voucher_id": voucher_id,
                        "reason": "Failed to update voucher"
                    })
                    
            except Exception as e:
                results["failed"].append({
                    "voucher_id": voucher_id,
                    "reason": str(e)
                })
        
        return {
            "message": f"Processed {len(approval_data.voucher_ids)} vouchers",
            "total_requested": len(approval_data.voucher_ids),
            "successful_count": len(results["successful"]),
            "failed_count": len(results["failed"]),
            "results": results
        }
    
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Error: {str(e)}")


@router.post("/{voucher_id}/approve-request")
async def send_for_approval(
    voucher_id: str,
    approval_data: ApprovalRequest
):
    """
    Send a voucher for approval.
    Changes status to 'awaiting_approval' and assigns an approver.
    Example: POST /accounting/voucher/68f880bcadf2e0b66e482d11/approve-request
    """
    try:
        obj_id = ObjectId(voucher_id)
        
        # Check if voucher exists
        voucher = voucher_collection.find_one({"_id": obj_id})
        if not voucher:
            raise HTTPException(status_code=404, detail="Voucher not found")
        
        # Check if voucher is in a valid state for approval request
        current_status = voucher.get("status")
        if current_status == "approved":
            raise HTTPException(
                status_code=400, 
                detail=f"Cannot send for approval. Voucher is already approved"
            )
        
        # Update voucher with approval request details
        update_data = {
            "status": "awaiting_approval",
            "approver_id": approval_data.approver_id,
            "approval_requested_at": datetime.utcnow(),
            "updated_at": datetime.utcnow()
        }
        
        result = voucher_collection.update_one(
            {"_id": obj_id},
            {"$set": update_data}
        )
        
        if result.modified_count == 0:
            raise HTTPException(status_code=500, detail="Failed to update voucher")
        
        # Get updated voucher
        updated_voucher = voucher_collection.find_one({"_id": obj_id})
        updated_voucher["_id"] = str(updated_voucher["_id"])
        if "created_at" in updated_voucher:
            updated_voucher["created_at"] = updated_voucher["created_at"].strftime("%Y-%m-%d %H:%M:%S")
        if "approval_requested_at" in updated_voucher:
            updated_voucher["approval_requested_at"] = updated_voucher["approval_requested_at"].strftime("%Y-%m-%d %H:%M:%S")
        if "updated_at" in updated_voucher:
            updated_voucher["updated_at"] = updated_voucher["updated_at"].strftime("%Y-%m-%d %H:%M:%S")
        
        return {
            "message": "Voucher sent for approval successfully",
            "voucher": updated_voucher
        }
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Error: {str(e)}")


@router.post("/approve")
async def approve_vouchers(
    approval_data: ApprovalRequest
):
    """
    Approve multiple vouchers.
    Changes status to 'approved' for all specified vouchers.
    
    Example: POST /accounting/voucher/approve
    Body: {
        "voucher_ids": ["68f880bcadf2e0b66e482d11", "68f880bcadf2e0b66e482d12"],
        "approver_id": "123",
        "notes": "All documents verified"
    }
    """
    try:
        results = {
            "successful": [],
            "failed": []
        }
        
        for voucher_id in approval_data.voucher_ids:
            try:
                obj_id = ObjectId(voucher_id)
                
                # Check if voucher exists
                voucher = voucher_collection.find_one({"_id": obj_id})
                if not voucher:
                    results["failed"].append({
                        "voucher_id": voucher_id,
                        "reason": "Voucher not found"
                    })
                    continue
                
                # Check if voucher is awaiting approval
                current_status = voucher.get("status")
                if current_status != "awaiting_approval":
                    results["failed"].append({
                        "voucher_id": voucher_id,
                        "reason": f"Cannot approve. Voucher status is '{current_status}', expected 'awaiting_approval'"
                    })
                    continue
                
                # Verify approver
                assigned_approver = voucher.get("approver_id")
                if assigned_approver and assigned_approver != approval_data.approver_id:
                    results["failed"].append({
                        "voucher_id": voucher_id,
                        "reason": f"Unauthorized. This voucher is assigned to approver: {assigned_approver}"
                    })
                    continue
                
                # Update voucher to approved
                update_data = {
                    "status": "approved",
                    "approved_by": approval_data.approver_id,
                    "approved_at": datetime.utcnow(),
                    "updated_at": datetime.utcnow()
                }
                
                if approval_data.notes:
                    update_data["approval_notes"] = approval_data.notes
                
                result = voucher_collection.update_one(
                    {"_id": obj_id},
                    {"$set": update_data}
                )
                if result.modified_count > 0:
                    results["successful"].append({
                        "voucher_id": voucher_id,
                        "status": "approved"
                    })
                else:
                    results["failed"].append({
                        "voucher_id": voucher_id,
                        "reason": "Failed to approve voucher"
                    })
                    
            except Exception as e:
                results["failed"].append({
                    "voucher_id": voucher_id,
                    "reason": str(e)
                })
        
        return {
            "message": f"Processed {len(approval_data.voucher_ids)} vouchers",
            "total_requested": len(approval_data.voucher_ids),
            "successful_count": len(results["successful"]),
            "failed_count": len(results["failed"]),
            "results": results
        }
    
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Error: {str(e)}")


@router.post("/reject")
async def reject_vouchers(
    rejection_data: RejectionRequest
):
    """
    Reject multiple vouchers.
    Changes status to 'rejected' with reason for all specified vouchers.
    
    Example: POST /accounting/voucher/reject
    Body: {
        "voucher_ids": ["68f880bcadf2e0b66e482d11", "68f880bcadf2e0b66e482d12"],
        "rejected_by": "123",
        "rejection_reason": "Missing documentation"
    }
    """
    try:
        results = {
            "successful": [],
            "failed": []
        }
        
        for voucher_id in rejection_data.voucher_ids:
            try:
                obj_id = ObjectId(voucher_id)
                
                # Check if voucher exists
                voucher = voucher_collection.find_one({"_id": obj_id})
                if not voucher:
                    results["failed"].append({
                        "voucher_id": voucher_id,
                        "reason": "Voucher not found"
                    })
                    continue
                
                # Check if voucher can be rejected
                current_status = voucher.get("status")
                if current_status in ["approved", "rejected"]:
                    results["failed"].append({
                        "voucher_id": voucher_id,
                        "reason": f"Cannot reject. Voucher is already {current_status}"
                    })
                    continue
                
                # Get current rejection count and increment it
                current_rejection_count = voucher.get("rejection_count", 0)
                new_rejection_count = current_rejection_count + 1
                
                # Update voucher to rejected
                update_data = {
                    "status": "rejected",
                    "rejected_by": rejection_data.rejected_by,
                    "rejection_reason": rejection_data.rejection_reason,
                    "rejected_at": datetime.utcnow(),
                    "updated_at": datetime.utcnow(),
                    "rejection_count": new_rejection_count
                }
                
                result = voucher_collection.update_one(
                    {"_id": obj_id},
                    {"$set": update_data}
                )
                
                if result.modified_count > 0:
                    results["successful"].append({
                        "voucher_id": voucher_id,
                        "status": "rejected"
                    })
                else:
                    results["failed"].append({
                        "voucher_id": voucher_id,
                        "reason": "Failed to reject voucher"
                    })
                    
            except Exception as e:
                results["failed"].append({
                    "voucher_id": voucher_id,
                    "reason": str(e)
                })
        
        return {
            "message": f"Processed {len(rejection_data.voucher_ids)} vouchers",
            "total_requested": len(rejection_data.voucher_ids),
            "successful_count": len(results["successful"]),
            "failed_count": len(results["failed"]),
            "results": results
        }
    
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Error: {str(e)}")




@router.patch("/{voucher_id}/classify")
async def classify_voucher(
    voucher_id: str,
    classification_data: ClassificationRequest
):
    """
    Classify a voucher by document type.
    Can be done manually or using AI auto-classification.
    
    Document types: supplier_invoice, expense, receipt, purchase_order, credit_note, debit_note, payment_voucher
    
    Example 1 (Manual): PATCH /accounting/voucher/68f880bcadf2e0b66e482d11/classify
    Body: {"document_type": "supplier_invoice"}
    
    Example 2 (AI): PATCH /accounting/voucher/68f880bcadf2e0b66e482d11/classify
    Body: {"use_ai": true}
    """
    try:
        obj_id = ObjectId(voucher_id)
        
        # Check if voucher exists
        voucher = voucher_collection.find_one({"_id": obj_id})
        if not voucher:
            raise HTTPException(status_code=404, detail="Voucher not found")
        
        document_type = None
        classification_method = "manual"
        ai_confidence = None
        
        # AI Classification
        if classification_data.use_ai:
            classification_method = "ai"
            
            # Check if voucher has files
            files = voucher.get("files", [])
            if not files:
                raise HTTPException(status_code=400, detail="No files found in voucher for AI classification")
            
            # Use OpenAI to classify the document
            try:
                import openai
                openai.api_key = OPENAI_KEY
                
                # Get the first file URL or OCR data
                file_info = files[0]
                
                # Check if OCR data exists for this voucher
                ocr_data = ocr_collection.find_one({"voucher_id": voucher_id})
                
                if ocr_data and ocr_data.get("extracted_text"):
                    text_content = ocr_data.get("extracted_text", "")
                else:
                    text_content = f"Document filename: {file_info.get('name', 'unknown')}"
                
                # Create prompt for classification
                prompt = f"""Analyze this document and classify it into one of these categories:
- supplier_invoice: Invoice from a supplier/vendor
- expense: Employee expense report or reimbursement
- receipt: Purchase receipt
- purchase_order: Purchase order document
- credit_note: Credit note from supplier
- debit_note: Debit note
- payment_voucher: Payment voucher or proof of payment

Document content:
{text_content[:1000]}

Respond with ONLY the category name and confidence (0-100). Format: category|confidence
Example: supplier_invoice|95"""

                response = openai.chat.completions.create(
                    model="gpt-3.5-turbo",
                    messages=[
                        {"role": "system", "content": "You are a document classification expert."},
                        {"role": "user", "content": prompt}
                    ],
                    temperature=0.3,
                    max_tokens=50
                )
                
                result = response.choices[0].message.content.strip()
                
                # Parse result
                if "|" in result:
                    document_type, confidence = result.split("|")
                    document_type = document_type.strip()
                    ai_confidence = float(confidence.strip())
                else:
                    document_type = result.strip()
                    ai_confidence = 85.0
                
            except Exception as ai_error:
                raise HTTPException(
                    status_code=500, 
                    detail=f"AI classification failed: {str(ai_error)}"
                )
        
        # Manual Classification
        elif classification_data.document_type:
            document_type = classification_data.document_type
            classification_method = "manual"
        else:
            raise HTTPException(
                status_code=400, 
                detail="Either provide 'document_type' for manual classification or set 'use_ai' to true"
            )
        
        # Validate document type
        valid_types = [
            "supplier_invoice", "expense", "receipt", "purchase_order", 
            "credit_note", "debit_note", "payment_voucher"
        ]
        if document_type not in valid_types:
            raise HTTPException(
                status_code=400, 
                detail=f"Invalid document type. Must be one of: {', '.join(valid_types)}"
            )
        
        # Update voucher with classification
        update_data = {
            "document_type": document_type,
            "classification_method": classification_method,
            "classified_at": datetime.utcnow(),
            "updated_at": datetime.utcnow()
        }
        
        if ai_confidence:
            update_data["ai_confidence"] = ai_confidence
        
        result = voucher_collection.update_one(
            {"_id": obj_id},
            {"$set": update_data}
        )
        
        if result.modified_count == 0:
            raise HTTPException(status_code=500, detail="Failed to classify voucher")
        
        # Get updated voucher
        updated_voucher = voucher_collection.find_one({"_id": obj_id})
        updated_voucher["_id"] = str(updated_voucher["_id"])
        if "created_at" in updated_voucher:
            updated_voucher["created_at"] = updated_voucher["created_at"].strftime("%Y-%m-%d %H:%M:%S")
        if "classified_at" in updated_voucher:
            updated_voucher["classified_at"] = updated_voucher["classified_at"].strftime("%Y-%m-%d %H:%M:%S")
        if "updated_at" in updated_voucher:
            updated_voucher["updated_at"] = updated_voucher["updated_at"].strftime("%Y-%m-%d %H:%M:%S")
        
        return {
            "message": f"Voucher classified successfully as '{document_type}' using {classification_method}",
            "document_type": document_type,
            "classification_method": classification_method,
            "ai_confidence": ai_confidence,
            "voucher": updated_voucher
        }
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Error: {str(e)}")


@router.get("/approvals/pending")
async def get_pending_vouchers(
    user_id: Optional[str] = Query(None, description="Filter by user ID"),
    limit: int = Query(50, description="Number of results to return", ge=1, le=100),
    offset: int = Query(0, description="Number of results to skip", ge=0)
):
    """
    Get all vouchers with status 'pending'.
    
    Examples:
    - GET /accounting/voucher/pending
    - GET /accounting/voucher/pending?user_id=123
    - GET /accounting/voucher/pending?limit=20&offset=0
    """
    try:
        # Build query for pending status
        query = {"status": "pending"}
        
        # Add user filter if provided
        if user_id:
            query["user_id"] = user_id
        
        # Get total count
        total_count = voucher_collection.count_documents(query)
        
        # Get vouchers with pagination
        vouchers = list(
            voucher_collection.find(query)
            .sort("created_at", -1)  # Most recent first
            .skip(offset)
            .limit(limit)
        )
        
        # Format vouchers
        formatted_vouchers = []
        for voucher in vouchers:
            formatted_voucher = {
                "_id": str(voucher["_id"]),
                "user_id": voucher.get("user_id"),
                "status": voucher.get("status"),
                "document_type": voucher.get("document_type"),
                "created_at": voucher.get("created_at").strftime("%Y-%m-%d %H:%M:%S") if voucher.get("created_at") else None,
                "updated_at": voucher.get("updated_at").strftime("%Y-%m-%d %H:%M:%S") if voucher.get("updated_at") else None,
                "files_count": len(voucher.get("files", [])),
                "files": voucher.get("files", [])
            }
            formatted_vouchers.append(formatted_voucher)
        
        # Calculate pagination info
        total_pages = (total_count + limit - 1) // limit if total_count > 0 else 0
        current_page = (offset // limit) + 1
        has_next = offset + limit < total_count
        has_previous = offset > 0
        
        return {
            "status": "pending",
            "total_count": total_count,
            "count": len(formatted_vouchers),
            "pagination": {
                "current_page": current_page,
                "total_pages": total_pages,
                "limit": limit,
                "offset": offset,
                "has_next": has_next,
                "has_previous": has_previous
            },
            "vouchers": formatted_vouchers
        }
    
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Error: {str(e)}")


@router.patch("/{voucher_id}/forward")
async def forward_voucher(
    voucher_id: str,
    forward_data: ForwardRequest
):
    """
    Forward/Reassign a voucher to another approver.
    Only the current assigned approver can forward the voucher.
    
    Example: PATCH /accounting/voucher/68f880bcadf2e0b66e482d11/forward
    Body: {
        "current_approver_id": "123",
        "new_approver_id": "456",
        "reason": "This requires finance team approval"
    }
    """
    try:
        obj_id = ObjectId(voucher_id)
        
        # Check if voucher exists
        voucher = voucher_collection.find_one({"_id": obj_id})
        if not voucher:
            raise HTTPException(status_code=404, detail="Voucher not found")
        
        # Check if voucher is in awaiting_approval status
        current_status = voucher.get("status")
        if current_status != "awaiting_approval":
            raise HTTPException(
                status_code=400, 
                detail=f"Cannot forward voucher. Status is '{current_status}', expected 'awaiting_approval'"
            )
        
        # Verify current user is the assigned approver
        current_approver = voucher.get("approver_id")
        if current_approver != forward_data.current_approver_id:
            raise HTTPException(
                status_code=403, 
                detail=f"Unauthorized. Only the assigned approver can forward this voucher. Current approver: {current_approver}"
            )
        
        # Check if forwarding to the same approver
        if forward_data.new_approver_id == forward_data.current_approver_id:
            raise HTTPException(
                status_code=400, 
                detail="Cannot forward to the same approver"
            )
        
        # Create forwarding history entry
        forwarding_history = voucher.get("forwarding_history", [])
        forwarding_entry = {
            "from_approver_id": forward_data.current_approver_id,
            "to_approver_id": forward_data.new_approver_id,
            "forwarded_at": datetime.utcnow(),
            "reason": forward_data.reason
        }
        forwarding_history.append(forwarding_entry)
        
        # Update voucher with new approver
        update_data = {
            "approver_id": forward_data.new_approver_id,
            "previous_approver_id": forward_data.current_approver_id,
            "forwarding_history": forwarding_history,
            "forwarded_at": datetime.utcnow(),
            "updated_at": datetime.utcnow()
        }
        
        if forward_data.reason:
            update_data["forward_reason"] = forward_data.reason
        
        result = voucher_collection.update_one(
            {"_id": obj_id},
            {"$set": update_data}
        )
        
        if result.modified_count == 0:
            raise HTTPException(status_code=500, detail="Failed to forward voucher")
        
        # Get updated voucher
        updated_voucher = voucher_collection.find_one({"_id": obj_id})
        updated_voucher["_id"] = str(updated_voucher["_id"])
        if "created_at" in updated_voucher:
            updated_voucher["created_at"] = updated_voucher["created_at"].strftime("%Y-%m-%d %H:%M:%S")
        if "forwarded_at" in updated_voucher:
            updated_voucher["forwarded_at"] = updated_voucher["forwarded_at"].strftime("%Y-%m-%d %H:%M:%S")
        if "updated_at" in updated_voucher:
            updated_voucher["updated_at"] = updated_voucher["updated_at"].strftime("%Y-%m-%d %H:%M:%S")
        
        # Format forwarding history
        if "forwarding_history" in updated_voucher:
            for entry in updated_voucher["forwarding_history"]:
                if "forwarded_at" in entry:
                    entry["forwarded_at"] = entry["forwarded_at"].strftime("%Y-%m-%d %H:%M:%S")
        
        return {
            "message": f"Voucher forwarded successfully from approver {forward_data.current_approver_id} to {forward_data.new_approver_id}",
            "previous_approver_id": forward_data.current_approver_id,
            "new_approver_id": forward_data.new_approver_id,
            "forward_reason": forward_data.reason,
            "voucher": updated_voucher
        }
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Error: {str(e)}")


@router.get("/{voucher_id}/forwarding-history")
async def get_forwarding_history(voucher_id: str):
    """
    Get the forwarding history of a voucher.
    Shows all approvers the voucher has been forwarded to.
    
    Example: GET /accounting/voucher/68f880bcadf2e0b66e482d11/forwarding-history
    """
    try:
        obj_id = ObjectId(voucher_id)
        
        # Check if voucher exists
        voucher = voucher_collection.find_one({"_id": obj_id})
        if not voucher:
            raise HTTPException(status_code=404, detail="Voucher not found")
        
        # Get forwarding history
        forwarding_history = voucher.get("forwarding_history", [])
        
        # Format dates
        formatted_history = []
        for entry in forwarding_history:
            formatted_entry = {
                "from_approver_id": entry.get("from_approver_id"),
                "to_approver_id": entry.get("to_approver_id"),
                "forwarded_at": entry.get("forwarded_at").strftime("%Y-%m-%d %H:%M:%S") if entry.get("forwarded_at") else None,
                "reason": entry.get("reason")
            }
            formatted_history.append(formatted_entry)
        
        return {
            "voucher_id": voucher_id,
            "current_approver_id": voucher.get("approver_id"),
            "total_forwards": len(formatted_history),
            "forwarding_history": formatted_history
        }
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Error: {str(e)}")



from typing import Optional, List, Dict, Any
class EmailsInput(BaseModel):
    user_id: str = Field(..., description="User ID of the person uploading the voucher")
    emails: List[Dict[str, Any]]

def convert_to_toon(obj: Dict[str, Any]) -> str:
    """
    Convert a dictionary object to Token-Oriented Object Notation (TOON)
    TOON format: key1:value1|key2:value2|key3:value3
    """
    tokens = []
    for key, value in obj.items():
        if value is None:
            tokens.append(f"{key}:null")
        elif isinstance(value, str):
            # Escape pipe characters in strings
            escaped_value = value.replace("|", "\\|")
            tokens.append(f"{key}:{escaped_value}")
        else:
            tokens.append(f"{key}:{value}")
    return "|".join(tokens)

@router.post("/gmail-data")
async def convert_emails_to_toon(emails_input: EmailsInput):
    """
    Convert email JSON objects to Token-Oriented Object Notation (TOON) and store in vouchers collection.
    
    Field Mapping:
    - sender_name -> title
    - subject -> description
    - purchase_type -> category
    - merchant -> additional metadata
    
    - **user_id**: User ID of the person uploading the voucher
    - **emails**: List of email objects to convert and store
    
    Returns voucher IDs and TOON-formatted data
    """
    try:
        stored_vouchers = []
        
        for email in emails_input.emails:
            # Convert email to TOON format
            toon_string = convert_to_toon(email)
            
            # Create voucher record with field mapping
            new_voucher = {
                "user_id": emails_input.user_id,
                "status": "pending",
                "OCR": "not_applicable",  # TOON data doesn't need OCR
                "data_format": "toon",  # Mark this as TOON format voucher
                "created_at": datetime.utcnow(),
                "files": [{
                    "name": f"email_{email.get('id', 'unknown')}.toon",
                    "toon_data": toon_string,
                    "original_email": email
                }]
            }
            
            # Map email fields to voucher fields
            if email.get("sender_name"):
                new_voucher["title"] = email.get("sender_name")
            
            if email.get("subject"):
                new_voucher["description"] = email.get("subject")
            
            if email.get("purchase_type"):
                new_voucher["category"] = email.get("purchase_type")
            
            # Add additional email metadata
            if email.get("merchant"):
                new_voucher["merchant"] = email.get("merchant")
            
            if email.get("amount"):
                new_voucher["amount"] = email.get("amount")
            
            if email.get("currency"):
                new_voucher["currency"] = email.get("currency")
            
            if email.get("order_number"):
                new_voucher["order_number"] = email.get("order_number")
            
            if email.get("sender_email"):
                new_voucher["sender_email"] = email.get("sender_email")
            
            if email.get("date"):
                new_voucher["email_date"] = email.get("date")
            
            # Insert into vouchers collection
            result = voucher_collection.insert_one(new_voucher)
            voucher_id = str(result.inserted_id)
            
            stored_vouchers.append({
                "voucher_id": voucher_id,
                "email_id": email.get("id"),
                "title": new_voucher.get("title"),
                "description": new_voucher.get("description"),
                "category": new_voucher.get("category"),
                "merchant": new_voucher.get("merchant"),
                "toon_data": toon_string
            })
        
        return {
            "success": True,
            "count": len(stored_vouchers),
            "message": f"Successfully stored {len(stored_vouchers)} vouchers",
            "vouchers": stored_vouchers
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error converting to TOON and storing: {str(e)}")
