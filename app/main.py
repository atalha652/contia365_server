from fastapi import FastAPI
from contextlib import asynccontextmanager
from app.routes import (
    api, auth, project, report, accounting, voucher, ledger, ocr,
    gmail_api, ledgers, outlook_api, dashboard, bank_transactions, billing, modelo, onboarding,
    census_data, tax_dashboard, tax_engine
)
from fastapi.middleware.cors import CORSMiddleware
from pymongo import MongoClient
import os
import certifi
from dotenv import load_dotenv

# Load environment variables
load_dotenv()
MONGO_URI = os.getenv("MONGO_URI")
DB_NAME = os.getenv("DB_NAME")

# Database connection
client = MongoClient(MONGO_URI, tlsCAFile=certifi.where())
db = client[DB_NAME]


# Lifespan context manager for startup/shutdown events
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    from app.tasks.scheduled_billing import init_scheduled_tasks
    init_scheduled_tasks(db)
    print("✅ Scheduled billing tasks initialized")
    yield
    # Shutdown
    from app.tasks.scheduled_billing import shutdown_scheduler
    shutdown_scheduler()
    print("✅ Scheduler shutdown complete")


app = FastAPI(
    title="Contia365 AI Invoice Automation API",
    description="Complete invoice automation with bank import and payment processing",
    version="2.0.0",
    lifespan=lifespan
)

# CORS — configured here only (not via .env). Use a literal list instead of "*"
# if you want to restrict to known frontends, e.g. ["http://localhost:5174", "https://app.example.com"].
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routes
app.include_router(api.router, prefix="/api/api")
app.include_router(auth.router, prefix="/api/auth")
app.include_router(onboarding.router, prefix="/api/onboarding")  # New onboarding routes
app.include_router(project.router, prefix="/api/project")
app.include_router(report.router, prefix="/api/report")
app.include_router(accounting.router, prefix="/api")
app.include_router(voucher.router, prefix="/api")
app.include_router(ledger.router, prefix="/api")
app.include_router(ocr.router, prefix="/api")
app.include_router(gmail_api.router, prefix="/api")
app.include_router(outlook_api.router, prefix="/api")
app.include_router(ledgers.router, prefix="/api")
app.include_router(dashboard.router, prefix="/api")

# New routes - Bank & Billing
app.include_router(bank_transactions.router, prefix="/api")
app.include_router(billing.router, prefix="/api")

# Modelo routes
app.include_router(modelo.router, prefix="/api")

# Census Data routes
app.include_router(census_data.router, prefix="/api")

# Tax dashboard routes
app.include_router(tax_dashboard.router, prefix="/api")

# Tax Calculation Engine routes
app.include_router(tax_engine.router, prefix="/api")


@app.get("/")
def root():
    return {"message": "Welcome to Contia365 AI Invoice Automation API"}
