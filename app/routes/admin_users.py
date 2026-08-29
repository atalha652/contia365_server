"""Admin-only Contia pages: dashboard, users directory, and sales waitlist."""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pymongo import MongoClient
import os
import certifi
from dotenv import load_dotenv

from app.routes.auth import get_current_user
from app.services.admin_dashboard_service import AdminDashboardService
from app.services.admin_users_service import (
    ADMIN_PAGE_SIZE,
    ADMIN_PAGES,
    AdminUsersService,
    is_admin,
    pagination_meta,
    parse_page,
    parse_page_size,
)
from app.services.waitlist_service import WaitlistService

load_dotenv()
client = MongoClient(os.getenv("MONGO_URI"), tlsCAFile=certifi.where())
db = client[os.getenv("DB_NAME")]

router = APIRouter(prefix="/admin", tags=["Admin"])
users_service = AdminUsersService(db["users"])
waitlist_service = WaitlistService(db["waitlist"])
dashboard_service = AdminDashboardService(
    db["users"],
    db["tax_filings"],
    db["waitlist"],
)


def _require_admin(current_user: dict):
    if not is_admin(current_user):
        raise HTTPException(status_code=403, detail="Only admin accounts can access this.")


@router.get("/pages")
def admin_pages(current_user: dict = Depends(get_current_user)):
    """Admin UI: Dashboard, Users, and Sales."""
    _require_admin(current_user)
    return {"pages": ADMIN_PAGES}


@router.get("/dashboard")
def admin_dashboard(current_user: dict = Depends(get_current_user)):
    """Platform snapshot for admin role only — not the taxpayer dashboard."""
    _require_admin(current_user)
    return dashboard_service.snapshot()


@router.get("/users")
def list_users(
    country: Optional[str] = Query(
        None,
        description="ES, IT, or unset (no country selected)",
    ),
    user_type: Optional[str] = Query(
        None,
        description="person, business, advisor, or unset",
    ),
    page: int = Query(1, ge=1, description="1-based page"),
    page_size: int = Query(
        ADMIN_PAGE_SIZE,
        ge=1,
        le=ADMIN_PAGE_SIZE,
        description="Max 10 users per page",
    ),
    current_user: dict = Depends(get_current_user),
):
    _require_admin(current_user)
    try:
        return users_service.list(
            country=country,
            user_type=user_type,
            page=page,
            page_size=page_size,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/sales")
def list_sales(
    page: int = Query(1, ge=1, description="1-based page"),
    page_size: int = Query(
        ADMIN_PAGE_SIZE,
        ge=1,
        le=ADMIN_PAGE_SIZE,
        description="Max 10 rows per page",
    ),
    current_user: dict = Depends(get_current_user),
):
    """Sales waitlist (White Label / Italy). Admin only."""
    _require_admin(current_user)
    page_n = parse_page(page)
    size = parse_page_size(page_size)
    rows = waitlist_service.list_all()
    start = (page_n - 1) * size
    return {
        "items": rows[start:start + size],
        **pagination_meta(len(rows), page_n, size),
    }
