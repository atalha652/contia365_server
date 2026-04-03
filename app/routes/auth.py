from fastapi import APIRouter, File, Depends, HTTPException
from pydantic import BaseModel, EmailStr
from pymongo import MongoClient
from bson import ObjectId
import os
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional, List
import bcrypt
import certifi
from jose import JWTError, jwt
from dotenv import load_dotenv
from fastapi.security import OAuth2PasswordBearer
from typing import List, Optional
from pydantic import BaseModel, EmailStr
from enum import Enum
from fastapi import Form, File, UploadFile
import os, json, uuid
from fastapi.responses import RedirectResponse, JSONResponse
from google_auth_oauthlib.flow import Flow
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests

# -------------------- Load Environment Variables --------------------
load_dotenv()
MONGO_URI = os.getenv("MONGO_URI")
DB_NAME = os.getenv("DB_NAME")
SECRET_KEY = os.getenv("SECRET_KEY", "ikingkhs23a")
ALGORITHM = os.getenv("ALGORITHM", "HS256")
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "600"))

# -------------------- Database Connection --------------------
client = MongoClient(MONGO_URI, tlsCAFile=certifi.where())
db = client[DB_NAME]
users_collection = db["users"]
oauth_states_collection = db["oauth_states"]
org_types_collection = db["org_types"]

# -------------------- Router --------------------
router = APIRouter()
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="login")

# -------------------- Enums --------------------
class UserType(str, Enum):
    individual = "individual"
    organization = "organization"
    freelancer = "freelancer"
    company = "company"
    advisor = "advisor"

# -------------------- Pydantic Models --------------------
class OrgTypeCreate(BaseModel):
    name: str

class OrgTypeResponse(BaseModel):
    id: str
    name: str

class OrganizationInfo(BaseModel):
    type_id: Optional[str] = None   # dropdown selection
    type_name: Optional[str] = None # custom user entry
    company_name: Optional[str] = None
    address: Optional[str] = None
    phone: Optional[str] = None

class BankDetails(BaseModel):
    iban: str
    account_holder: str


class GmailCredentials(BaseModel):
    token: Optional[str] = None
    refresh_token: Optional[str] = None
    token_uri: Optional[str] = None
    client_id: Optional[str] = None
    client_secret: Optional[str] = None
    scopes: Optional[List[str]] = None


class PaymentMethod(str, Enum):
    stripe = "Stripe"
    redsys = "Redsys"
    bizum = "Bizum"
class Role(str, Enum):
    user = "user"
    admin = "admin"
class OtherCertificate(BaseModel):
    name: str
    url_: str
class UserCreate(BaseModel):
    name: str
    email: EmailStr
    phone: Optional[str] = None
    password: str
    type: UserType
    tax_id: Optional[str] = None
    organization_info: Optional[OrganizationInfo] = None
    registration_flow: Optional[str] = None
    has_digital_certificate: Optional[str] = None
    auto_fill: Optional[bool] = False
    dni_nie: Optional[str] = None
    bank_details: Optional[BankDetails] = None
    # Change this line to properly handle None:
    payment_method: Optional[PaymentMethod] = None  # This should work now
    role: Optional[Role] = None
    connect_to_fnmt: Optional[bool] = False
    connect_to_aeat: Optional[bool] = False
    administrator_check: Optional[bool] = False
    type_of_administration: Optional[str] = None
    other_certificate: Optional[List[OtherCertificate]] = []
    status: Optional[bool] = False
    gmail_credentials: Optional[GmailCredentials] = None


class UserLogin(BaseModel):
    email: EmailStr
    password: str

# -------------------- Token Helper --------------------
def create_access_token(data: dict, expires_delta: timedelta = None):
    to_encode = data.copy()
    expire = datetime.utcnow() + (expires_delta or timedelta(minutes=15))
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

# -------------------- Add Organization Type --------------------
@router.post("/org-types", response_model=dict)
def add_org_type(type_data: OrgTypeCreate):
    # Check if type already exists
    if org_types_collection.find_one({"name": type_data.name.lower()}):
        raise HTTPException(status_code=400, detail="Type already exists")

    result = org_types_collection.insert_one({
        "name": type_data.name.lower(),
        "created_at": datetime.utcnow()
    })
    return {"message": "Organization type added", "type_id": str(result.inserted_id)}

# -------------------- Get All Organization Types --------------------
@router.get("/org-types", response_model=List[OrgTypeResponse])
def get_org_types():
    types = org_types_collection.find({}, {"name": 1})
    return [{"id": str(t["_id"]), "name": t["name"]} for t in types]

# -------------------- Signup --------------------
@router.post(
    "/signup",
    summary="Register a new user",
    description="""
This endpoint registers a new user in the system.  
It supports both **individual** and **organization** flows.  

### Features:
- Registration flow (personal/company)
- Optional Digital Certificate upload
- FNMT & AEAT integration flags
- Administration checks
- Additional certificates (JSON list)
- Payment method selection

### Notes:
- `certificate` must be uploaded as `multipart/form-data` file.
- `other_certificate` must be sent as a **JSON string** inside form-data,  
  e.g. `[{"name":"Cert A","url_":"https://example.com/a"}]`.

"""
)
async def signup(
    # Basic info
    name: str = Form(..., description="Full name of the user"),
    email: EmailStr = Form(..., description="Email address (must be unique)"),
    password: str = Form(..., description="Password (will be hashed)"),
    type: UserType = Form(..., description="User type: 'individual' or 'organization'"),
    phone: Optional[str] = Form(None, description="Phone number"),
    tax_id: Optional[str] = Form(None, description="Tax identification number (NIF/CIF)"),

    # Registration flow
    registration_flow: Optional[str] = Form(None, description="Registration flow: 'personal_flow' or 'company_flow'"),
    role: Optional[Role] = Form(None, description="User role: 'user' or 'admin'"),
    # Digital certificate
    has_digital_certificate: Optional[str] = Form(None, description="'yes_flow' or 'no_flow'"),
    auto_fill: Optional[bool] = Form(False, description="Auto-fill data if certificate available"),
    dni_nie: Optional[str] = Form(None, description="National ID (DNI/NIE)"),
    iban: Optional[str] = Form(None, description="IBAN (bank account)"),
    account_holder: Optional[str] = Form(None, description="Bank account holder name"),
    # certificate: UploadFile = File(None, description="Digital certificate file (.p12/.pfx/.pdf)"),

    # FNMT & AEAT
    connect_to_fnmt: Optional[bool] = Form(False, description="Generate FNMT request code"),
    connect_to_aeat: Optional[bool] = Form(False, description="Request AEAT appointment (online/in-person)"),
    status: Optional[bool] = Form(False, description="Status of the organization (default: False)"),
    # Administration
    administrator_check: Optional[bool] = Form(False, description="Admin validation required?"),
    type_of_administration: Optional[str] = Form(None, description="Type of administration (e.g. central, regional)"),

    # Other certificates
    other_certificate: Optional[str] = Form(
        None,
        description="JSON list of certificates. Example: "
                    "[{\"name\":\"Cert A\",\"url_\":\"https://example.com/a\"}]"
    ),

    # Payment
    payment_method: Optional[str] = Form(None, description="Payment method: Stripe / Redsys / Bizum")
):
    # Convert empty string to None
    if payment_method == "":
        payment_method = None
    
    # Convert string to enum if provided
    payment_method_enum = None
    if payment_method:
        try:
            payment_method_enum = PaymentMethod(payment_method)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid payment method")

    # Hash password
    hashed_pw = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

    # Parse other_certificate JSON
    other_certs = []
    if other_certificate:
        try:
            other_certs = json.loads(other_certificate)
        except Exception:
            other_certs = []

# Build user object
    user = UserCreate(
        name=name,
        email=email,
        phone=phone,
        password=password,
        type=type,
        tax_id=tax_id,
        organization_info=None,
        registration_flow=registration_flow,
        has_digital_certificate=has_digital_certificate,
        auto_fill=auto_fill,
        dni_nie=dni_nie,
        bank_details=BankDetails(iban=iban, account_holder=account_holder) if iban and account_holder else None,
        payment_method=payment_method,  # Use the converted enum
        connect_to_fnmt=connect_to_fnmt,
        connect_to_aeat=connect_to_aeat,
        administrator_check=administrator_check,
        status=status,
        role=role,
        type_of_administration=type_of_administration,
        other_certificate=[OtherCertificate(**oc) for oc in other_certs] if other_certs else []
    )

    # Prepare DB document
    new_user = user.dict()
    new_user.update({
        "password_hash": hashed_pw,
        "certificate_path": cert_path if has_digital_certificate == "yes_flow" else None,
        "created_at": datetime.utcnow(),
        "gmail_credentials": user.gmail_credentials.dict() if user.gmail_credentials else None,
    })

    # Organization handling
    if user.type == UserType.organization and user.organization_info:
        org_type = None

        # Case 1: Dropdown selection (type_id provided)
        if getattr(user.organization_info, "type_id", None):
            try:
                org_type = org_types_collection.find_one(
                    {"_id": ObjectId(user.organization_info.type_id)}
                )
            except:
                raise HTTPException(status_code=400, detail="Invalid organization type ID")

            if not org_type:
                raise HTTPException(status_code=400, detail="Invalid organization type")

        # Case 2: User entered their own type (type_name provided)
        elif getattr(user.organization_info, "type_name", None):
            type_name = user.organization_info.type_name.strip().lower()
            org_type = org_types_collection.find_one({"name": type_name})

            # If type doesn't exist, insert it
            if not org_type:
                inserted_type = org_types_collection.insert_one({
                    "name": type_name,
                    "created_at": datetime.utcnow()
                })
                org_type = {"_id": inserted_type.inserted_id, "name": type_name}

        else:
            raise HTTPException(status_code=400, detail="Organization type is required")

        # Save organization info in user document
        new_user["organization_info"] = {
            "type": org_type["name"],
            "company_name": user.organization_info.company_name,
            "address": user.organization_info.address,
            "phone": user.organization_info.phone,
        }

    # Insert into database
    result = users_collection.insert_one(new_user)
    return {"message": "User created successfully", "user_id": str(result.inserted_id)}

# -------------------- Login --------------------
@router.post("/login")
def login(user: UserLogin):
    db_user = users_collection.find_one({"email": user.email.lower()})
    if not db_user or not bcrypt.checkpw(user.password.encode("utf-8"), db_user["password_hash"].encode("utf-8")):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    # Remove password from response
    db_user["_id"] = str(db_user["_id"])
    db_user.pop("password_hash", None)

    access_token = create_access_token(
        {"sub": str(db_user["_id"])},
        timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    )

    return {
        "message": "User logged in successfully",
        "access_token": access_token,
        "token_type": "bearer",
        "name": db_user["name"],
        "email": db_user["email"],
        "user_id": db_user["_id"],
        "tax_id": db_user["tax_id"],
        "organization_info": db_user.get("organization_info", {})
    }

# Get current logged-in user
def get_current_user(token: str = Depends(oauth2_scheme)):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id = payload.get("sub")
        if not user_id:
            raise HTTPException(status_code=401, detail="Invalid token")
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid token")

    user = users_collection.find_one({"_id": ObjectId(user_id)})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user

# -------------------- Onboarding: Save User Type --------------------
class OnboardingUpdate(BaseModel):
    user_type: UserType

@router.patch("/users/onboarding")
def update_user_type(
    data: OnboardingUpdate,
    current_user: dict = Depends(get_current_user)
):
    allowed = {UserType.freelancer, UserType.company, UserType.advisor}
    if data.user_type not in allowed:
        raise HTTPException(status_code=400, detail="Invalid onboarding type. Choose freelancer, company, or advisor.")

    users_collection.update_one(
        {"_id": current_user["_id"]},
        {"$set": {"type": data.user_type, "onboarding_completed": True}}
    )
    return {"message": "User type updated successfully", "user_type": data.user_type}

# Example protected route
@router.get("/dashboard")
def dashboard(current_user: dict = Depends(get_current_user)):
    return {
        "message": f"Welcome {current_user['name']}!",
        "email": current_user["email"],
        "id": str(current_user["_id"])  # Convert ObjectId to string
    }



# -------------------- Google OAuth (Login/Signup) --------------------
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", os.getenv("GMAIL_CLIENT_ID"))
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", os.getenv("GMAIL_CLIENT_SECRET"))
GOOGLE_AUTH_URI = os.getenv("GOOGLE_AUTH_URI", "https://accounts.google.com/o/oauth2/v2/auth")
GOOGLE_TOKEN_URI = os.getenv("GOOGLE_TOKEN_URI", "https://oauth2.googleapis.com/token")
GOOGLE_CERT_URL = os.getenv("GOOGLE_CERT_URL", "https://www.googleapis.com/oauth2/v1/certs")
GOOGLE_REDIRECT_URI = os.getenv(
    "GOOGLE_REDIRECT_URI",
    "https://ai-invoice-automate-backend-njgp.onrender.com/api/auth/google/callback"
)
GOOGLE_SCOPES = ["openid", "email", "profile"]

def _build_google_login_client_config():
    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
        raise HTTPException(status_code=500, detail="Missing GOOGLE_CLIENT_ID or GOOGLE_CLIENT_SECRET in environment")
    return {
        "web": {
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "auth_uri": GOOGLE_AUTH_URI,
            "token_uri": GOOGLE_TOKEN_URI,
            "auth_provider_x509_cert_url": GOOGLE_CERT_URL,
            "redirect_uris": [GOOGLE_REDIRECT_URI],
        }
    }

@router.get("/google/authorize")
def google_authorize():
    """
    Start Google OAuth (OpenID Connect) for login/signup.
    Redirects to Google's consent screen.
    """
    try:
        flow = Flow.from_client_config(
            _build_google_login_client_config(),
            scopes=GOOGLE_SCOPES,
            redirect_uri=GOOGLE_REDIRECT_URI
        )
        authorization_url, state = flow.authorization_url(
            access_type='offline',
            include_granted_scopes='true',
            prompt='consent'
        )
        # Track state to prevent CSRF
        oauth_states_collection.insert_one({
            "state": state,
            "created_at": datetime.utcnow()
        })
        return RedirectResponse(authorization_url)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Google OAuth authorize error: {str(e)}")

@router.get("/google/callback")
def google_callback(code: str, state: str):
    """
    Handle Google OAuth callback, create or login user, and return app JWT.
    """
    try:
        # Validate state
        state_doc = oauth_states_collection.find_one({"state": state})
        if not state_doc:
            raise HTTPException(status_code=400, detail="Invalid OAuth state")
        # Clean up state
        oauth_states_collection.delete_one({"_id": state_doc["_id"]})

        flow = Flow.from_client_config(
            _build_google_login_client_config(),
            scopes=GOOGLE_SCOPES,
            redirect_uri=GOOGLE_REDIRECT_URI
        )
        # Exchange code
        flow.fetch_token(code=code)
        credentials = flow.credentials
        id_token_jwt = credentials.id_token
        if not id_token_jwt:
            raise HTTPException(status_code=400, detail="Missing id_token in OAuth response")

        # Verify id_token and extract user info
        claims = id_token.verify_oauth2_token(
            id_token_jwt,
            google_requests.Request(),
            audience=GOOGLE_CLIENT_ID
        )
        email = claims.get("email")
        if not email:
            raise HTTPException(status_code=400, detail="No email in Google ID token")
        email_lower = email.lower()
        name = claims.get("name") or email_lower.split("@")[0]
        sub = claims.get("sub")
        picture = claims.get("picture")
        email_verified = claims.get("email_verified", False)

        # Upsert user
        user = users_collection.find_one({"email": email_lower})
        if not user:
            new_user = {
                "name": name,
                "email": email_lower,
                "password_hash": None,
                "created_at": datetime.utcnow(),
                "registration_flow": "google_oauth",
                "status": True,
                "role": "user",
                "google": {
                    "sub": sub,
                    "email_verified": email_verified,
                    "picture": picture
                },
                "google_credentials": {
                    "token": credentials.token,
                    "refresh_token": credentials.refresh_token,
                    "token_uri": credentials.token_uri,
                    "client_id": credentials.client_id,
                    "client_secret": credentials.client_secret,
                    "scopes": credentials.scopes
                }
            }
            result = users_collection.insert_one(new_user)
            user_id_str = str(result.inserted_id)
        else:
            # Update Google details/tokens
            users_collection.update_one(
                {"_id": user["_id"]},
                {"$set": {
                    "name": name,
                    "google": {
                        "sub": sub,
                        "email_verified": email_verified,
                        "picture": picture
                    },
                    "google_credentials": {
                        "token": credentials.token,
                        "refresh_token": credentials.refresh_token,
                        "token_uri": credentials.token_uri,
                        "client_id": credentials.client_id,
                        "client_secret": credentials.client_secret,
                        "scopes": credentials.scopes
                    }
                }}
            )
            user_id_str = str(user["_id"]) if isinstance(user["_id"], ObjectId) else user["_id"]

        # Issue app JWT
        access_token = create_access_token(
            {"sub": user_id_str},
            timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
        )
        return {
            "message": "Google login successful",
            "access_token": access_token,
            "token_type": "bearer",
            "name": name,
            "email": email_lower,
            "user_id": user_id_str,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Google OAuth callback error: {str(e)}")


