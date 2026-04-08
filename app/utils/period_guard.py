"""
Monthly Upload Deadline Guard
Users can upload vouchers for a period until the 10th of the following month.
Example: March (2026-03) is open all of March + until April 10th 23:59:59.
"""

from datetime import datetime
from fastapi import Form, HTTPException


def is_period_open(target_period: str) -> bool:
    """
    Returns True if uploads are still allowed for target_period (format: 'YYYY-MM').
    - Current month: always open.
    - Previous month: open until the 10th of the current month (inclusive).
    - Anything older: closed.
    """
    try:
        target = datetime.strptime(target_period, "%Y-%m")
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid period format '{target_period}'. Expected YYYY-MM.")

    now = datetime.utcnow()
    current_year, current_month = now.year, now.month

    # Same month — always open
    if target.year == current_year and target.month == current_month:
        return True

    # Previous month — open until the 10th
    prev_month = current_month - 1 if current_month > 1 else 12
    prev_year  = current_year if current_month > 1 else current_year - 1

    if target.year == prev_year and target.month == prev_month:
        return now.day <= 10

    return False


async def validate_upload_window(period: str = Form(..., description="Tax period in YYYY-MM format, e.g. '2026-03'")):
    """FastAPI dependency — raises 403 if the period is closed for uploads."""
    if not is_period_open(period):
        raise HTTPException(
            status_code=403,
            detail=f"The tax period {period} is closed for uploads. Deadlines are the 10th of the following month."
        )
    return period
