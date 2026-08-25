"""FastAPI dependency: block Spanish tax APIs for country=IT."""

from fastapi import Depends, HTTPException

from app.routes.auth import get_current_user
from app.services.spain_tax_access import (
    ItalyTaxUnavailableError,
    assert_spanish_tax_allowed,
)


def require_spanish_tax(current_user: dict = Depends(get_current_user)) -> dict:
    try:
        assert_spanish_tax_allowed(current_user)
    except ItalyTaxUnavailableError as exc:
        raise HTTPException(status_code=403, detail=exc.detail) from exc
    return current_user
