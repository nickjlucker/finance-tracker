from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app import plaid_client
from app.database import get_db
from app.models import Account, PlaidItem, Transaction
from app.schemas import SyncResult, TransactionOut

router = APIRouter(prefix="/api/transactions", tags=["transactions"])


def _upsert_transaction(db: Session, account_by_plaid_id: dict[str, Account], txn) -> None:
    account = account_by_plaid_id.get(txn.account_id)
    if account is None:
        return

    category = txn.personal_finance_category
    category_primary = category.primary if category else "OTHER"
    category_detailed = category.detailed if category else ""

    existing = db.query(Transaction).filter_by(transaction_id=txn.transaction_id).one_or_none()
    if existing is None:
        existing = Transaction(transaction_id=txn.transaction_id, account_id=account.id)
        db.add(existing)

    existing.account_id = account.id
    existing.name = txn.name
    existing.merchant_name = txn.merchant_name
    existing.amount = txn.amount
    existing.iso_currency_code = txn.iso_currency_code
    existing.date = txn.date
    existing.pending = txn.pending
    existing.category_primary = category_primary
    existing.category_detailed = category_detailed


@router.post("/sync", response_model=SyncResult)
def sync_transactions(db: Session = Depends(get_db)):
    items = db.query(PlaidItem).all()
    if not items:
        raise HTTPException(status_code=404, detail="No linked accounts yet")

    total_added = total_modified = total_removed = 0
    for item in items:
        try:
            result = plaid_client.sync_transactions(item.access_token, item.transactions_cursor)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Plaid error: {exc}") from exc

        account_by_plaid_id = {a.account_id: a for a in item.accounts}

        for txn in result["added"]:
            _upsert_transaction(db, account_by_plaid_id, txn)
        for txn in result["modified"]:
            _upsert_transaction(db, account_by_plaid_id, txn)
        for removed in result["removed"]:
            db.query(Transaction).filter_by(transaction_id=removed.transaction_id).delete()

        item.transactions_cursor = result["cursor"]
        total_added += len(result["added"])
        total_modified += len(result["modified"])
        total_removed += len(result["removed"])

    db.commit()
    return SyncResult(added=total_added, modified=total_modified, removed=total_removed)


@router.get("", response_model=list[TransactionOut])
def list_transactions(
    db: Session = Depends(get_db),
    category: str | None = None,
    account_id: str | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    q: str | None = None,
    limit: int = Query(100, le=500),
    offset: int = 0,
):
    query = db.query(Transaction).join(Account)
    if category:
        query = query.filter(Transaction.category_primary == category)
    if account_id:
        query = query.filter(Account.account_id == account_id)
    if start_date:
        query = query.filter(Transaction.date >= start_date)
    if end_date:
        query = query.filter(Transaction.date <= end_date)
    if q:
        like = f"%{q}%"
        query = query.filter(Transaction.name.ilike(like))

    rows = query.order_by(Transaction.date.desc()).offset(offset).limit(limit).all()

    out = []
    for txn in rows:
        data = TransactionOut.model_validate(txn)
        data.account_name = txn.account.name
        out.append(data)
    return out


@router.get("/categories")
def list_categories(db: Session = Depends(get_db)):
    rows = db.query(Transaction.category_primary).distinct().all()
    return sorted({r[0] for r in rows})
