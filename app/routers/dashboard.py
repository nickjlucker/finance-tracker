from datetime import date

from fastapi import APIRouter, Depends, Request
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import Account, Budget, Transaction

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


@router.get("/api/dashboard/summary")
def dashboard_summary(db: Session = Depends(get_db)):
    accounts = db.query(Account).all()
    total_balance = sum(a.current_balance or 0.0 for a in accounts)

    today = date.today()
    month_start = today.replace(day=1)

    spend_rows = (
        db.query(Transaction.category_primary, func.sum(Transaction.amount))
        .filter(Transaction.date >= month_start, Transaction.date <= today, Transaction.amount > 0)
        .group_by(Transaction.category_primary)
        .all()
    )
    spend_by_category = [{"category": category, "amount": total} for category, total in spend_rows]
    total_spent = sum(row["amount"] for row in spend_by_category)

    income_total = (
        db.query(func.sum(Transaction.amount))
        .filter(Transaction.date >= month_start, Transaction.date <= today, Transaction.amount < 0)
        .scalar()
        or 0.0
    )

    budget_count = db.query(Budget).count()

    return {
        "total_balance": total_balance,
        "account_count": len(accounts),
        "month_spent": total_spent,
        "month_income": -income_total,
        "spend_by_category": sorted(spend_by_category, key=lambda r: -r["amount"]),
        "budget_count": budget_count,
    }
