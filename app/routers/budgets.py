from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Budget, Transaction
from app.schemas import BudgetIn, BudgetOut

router = APIRouter(prefix="/api/budgets", tags=["budgets"])


def _month_spend_by_category(db: Session) -> dict[str, float]:
    today = date.today()
    month_start = today.replace(day=1)
    rows = (
        db.query(Transaction.category_primary, func.sum(Transaction.amount))
        .filter(Transaction.date >= month_start, Transaction.date <= today, Transaction.amount > 0)
        .group_by(Transaction.category_primary)
        .all()
    )
    return {category: total for category, total in rows}


@router.get("", response_model=list[BudgetOut])
def list_budgets(db: Session = Depends(get_db)):
    spend_by_category = _month_spend_by_category(db)
    budgets = db.query(Budget).all()
    out = []
    for budget in budgets:
        data = BudgetOut.model_validate(budget)
        data.spent = spend_by_category.get(budget.category_primary, 0.0)
        out.append(data)
    return out


@router.put("", response_model=BudgetOut)
def upsert_budget(payload: BudgetIn, db: Session = Depends(get_db)):
    budget = db.query(Budget).filter_by(category_primary=payload.category_primary).one_or_none()
    if budget is None:
        budget = Budget(category_primary=payload.category_primary, monthly_limit=payload.monthly_limit)
        db.add(budget)
    else:
        budget.monthly_limit = payload.monthly_limit
    db.commit()

    spend_by_category = _month_spend_by_category(db)
    data = BudgetOut.model_validate(budget)
    data.spent = spend_by_category.get(budget.category_primary, 0.0)
    return data


@router.delete("/{category_primary}")
def delete_budget(category_primary: str, db: Session = Depends(get_db)):
    budget = db.query(Budget).filter_by(category_primary=category_primary).one_or_none()
    if budget is None:
        raise HTTPException(status_code=404, detail="Budget not found")
    db.delete(budget)
    db.commit()
    return {"status": "ok"}
