from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app import analytics
from app.database import get_db
from app.models import Account, Holding, Security
from app.schemas import (
    AlertsOut,
    CashFlowForecastOut,
    FlaggedTransactionOut,
    HoldingOut,
    MonthlySummaryOut,
    NetWorthOut,
    RecurringItemOut,
    RecurringOverrideIn,
)

router = APIRouter(prefix="/api/dashboard", tags=["insights"])

VALID_CLASSIFICATIONS = {
    "fixed_obligation",
    "subscription",
    "recurring_discretionary",
    "probable_recurrence",
    "not_recurring",
    "reliable_income",
    "probable_income",
}


@router.get("/alerts", response_model=AlertsOut)
def alerts(db: Session = Depends(get_db)):
    return {
        "alerts": analytics.compute_critical_alerts(db),
        "liquidity": analytics.compute_liquidity_breakdown(db),
    }


@router.get("/recurring", response_model=list[RecurringItemOut])
def recurring(db: Session = Depends(get_db)):
    return analytics.analyze_recurring(db, "spend")


@router.get("/recurring/income", response_model=list[RecurringItemOut])
def recurring_income(db: Session = Depends(get_db)):
    return analytics.analyze_recurring(db, "income")


@router.put("/recurring/{canonical_merchant}/override")
def set_recurring_override(canonical_merchant: str, payload: RecurringOverrideIn, db: Session = Depends(get_db)):
    if payload.classification not in VALID_CLASSIFICATIONS:
        raise HTTPException(status_code=400, detail=f"Invalid classification: {payload.classification}")
    analytics.set_recurring_override(db, canonical_merchant, payload.classification)
    db.commit()
    return {"status": "ok"}


@router.delete("/recurring/{canonical_merchant}/override")
def clear_recurring_override(canonical_merchant: str, db: Session = Depends(get_db)):
    found = analytics.clear_recurring_override(db, canonical_merchant)
    db.commit()
    if not found:
        raise HTTPException(status_code=404, detail="No override set for this merchant")
    return {"status": "ok"}


@router.get("/flags", response_model=list[FlaggedTransactionOut])
def flags(db: Session = Depends(get_db)):
    return analytics.detect_flagged_transactions(db)


@router.get("/networth", response_model=NetWorthOut)
def networth(annual_return_pct: float = Query(5.0), db: Session = Depends(get_db)):
    history = analytics.net_worth_history(db)
    current_net_worth = history[-1]["net_worth"] if history else analytics.asset_breakdown(db)["net_worth"]
    monthly_net_savings = analytics.trailing_monthly_net_savings(db)
    projection = analytics.compute_net_worth_projection(current_net_worth, monthly_net_savings, annual_return_pct)
    projection_series = analytics.net_worth_projection_series(current_net_worth, monthly_net_savings, annual_return_pct)
    estimated = [p["date"] for p in history if p["estimated"]]

    return {
        "history": history,
        "tracking_since": history[0]["date"] if history else None,
        # Confidence reflects real synced snapshots only, not reconstructed days.
        "data_confidence": analytics.history_data_confidence([p for p in history if not p["estimated"]]),
        "monthly_net_savings": monthly_net_savings,
        "annual_return_pct": annual_return_pct,
        "projection": projection,
        "projection_series": projection_series,
        "reconstructed_until": estimated[-1] if estimated else None,
    }


@router.get("/forecast", response_model=CashFlowForecastOut)
def forecast(days: int = Query(30), safety_floor: float = Query(500.0), db: Session = Depends(get_db)):
    return analytics.forecast_cash_flow(db, days=days, safety_floor=safety_floor)


@router.get("/monthly-summary", response_model=list[MonthlySummaryOut])
def monthly_summary(months: int = Query(6), db: Session = Depends(get_db)):
    return analytics.monthly_income_spend_savings(db, months=months)


@router.get("/holdings", response_model=list[HoldingOut])
def holdings(db: Session = Depends(get_db)):
    rows = (
        db.query(Holding, Security, Account)
        .join(Security, Holding.security_id == Security.id)
        .join(Account, Holding.account_id == Account.id)
        .all()
    )
    return [
        {
            "account_name": account.name,
            "ticker_symbol": security.ticker_symbol,
            "security_name": security.name,
            "quantity": holding.quantity,
            "institution_value": holding.institution_value,
            "cost_basis": holding.cost_basis,
        }
        for holding, security, account in rows
    ]
