from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app import analytics
from app.database import get_db
from app.schemas import (
    AlertOut,
    FlaggedTransactionOut,
    NetWorthOut,
    RecurringBillOut,
)

router = APIRouter(prefix="/api/dashboard", tags=["insights"])


@router.get("/alerts", response_model=list[AlertOut])
def alerts(db: Session = Depends(get_db)):
    return analytics.compute_critical_alerts(db)


@router.get("/recurring", response_model=list[RecurringBillOut])
def recurring(db: Session = Depends(get_db)):
    return analytics.detect_recurring_bills(db)


@router.get("/flags", response_model=list[FlaggedTransactionOut])
def flags(db: Session = Depends(get_db)):
    return analytics.detect_flagged_transactions(db)


@router.get("/networth", response_model=NetWorthOut)
def networth(annual_return_pct: float = Query(5.0), db: Session = Depends(get_db)):
    history = analytics.net_worth_history(db)
    current_net_worth = history[-1]["net_worth"] if history else analytics.cash_debt_net(db)["net_position"]
    monthly_net_savings = analytics.trailing_monthly_net_savings(db)
    projection = analytics.compute_net_worth_projection(current_net_worth, monthly_net_savings, annual_return_pct)

    return {
        "history": history,
        "tracking_since": history[0]["date"] if history else None,
        "monthly_net_savings": monthly_net_savings,
        "annual_return_pct": annual_return_pct,
        "projection": projection,
    }
