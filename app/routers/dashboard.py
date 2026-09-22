from datetime import date

from fastapi import APIRouter, Depends, Request
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app import analytics
from app.config import settings
from app.database import get_db
from app.models import Account, Budget
from app.schemas import DashboardSummary

router = APIRouter(tags=["dashboard"])
templates = Jinja2Templates(directory="app/templates")
templates.env.globals["plaid_env"] = settings.plaid_env


@router.get("/")
def dashboard_page(request: Request):
    return templates.TemplateResponse("index.html", {"request": request, "active": "dashboard"})


@router.get("/transactions")
def transactions_page(request: Request):
    return templates.TemplateResponse("transactions.html", {"request": request, "active": "transactions"})


@router.get("/budgets")
def budgets_page(request: Request):
    return templates.TemplateResponse("budgets.html", {"request": request, "active": "budgets"})


@router.get("/api/dashboard/summary", response_model=DashboardSummary)
def dashboard_summary(db: Session = Depends(get_db)):
    accounts = db.query(Account).all()
    today = date.today()
    month_start = today.replace(day=1)

    spend_rows = analytics.spend_by_category(db, month_start, today)
    spend_by_category = [{"category": category, "amount": total} for category, total in spend_rows]
    total_spent = sum(row["amount"] for row in spend_by_category)

    budget_count = db.query(Budget).count()

    return {
        **analytics.cash_debt_net(db),
        "account_count": len(accounts),
        "month_spent": total_spent,
        "month_income": analytics.income_total(db, month_start, today),
        "month_invested": analytics.invested_amount(db, month_start, today),
        "interest_paid_ttm": analytics.interest_paid_ttm(db),
        "spend_by_category": sorted(spend_by_category, key=lambda r: -r["amount"]),
        "budget_count": budget_count,
    }
