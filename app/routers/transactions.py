from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app import analytics, plaid_client
from app.database import get_db
from app.merchant import normalize_merchant
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
    institution_name = account.item.institution_name if account.item else ""
    existing.canonical_merchant = normalize_merchant(txn.name, txn.merchant_name, institution_name)


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
    include_internal: bool = True,
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
        query = query.filter(
            or_(Transaction.name.ilike(like), Transaction.merchant_name.ilike(like), Transaction.canonical_merchant.ilike(like))
        )

    if not include_internal:
        query = query.filter(Transaction.is_internal_transfer.is_(False))

    rows = query.order_by(Transaction.date.desc()).offset(offset).limit(limit).all()

    matched_ids = [t.matched_transaction_id for t in rows if t.matched_transaction_id]
    matched = {t.id: t for t in db.query(Transaction).filter(Transaction.id.in_(matched_ids)).all()} if matched_ids else {}

    out = []
    for txn in rows:
        data = TransactionOut.model_validate(txn)
        data.account_name = txn.account.name
        partner = matched.get(txn.matched_transaction_id)
        if partner is not None and partner.account is not None:
            partner_account = partner.account
            data.counterparty_account = (
                analytics.card_display_name(partner_account) if partner_account.type == "credit" else partner_account.name
            )
        out.append(data)
    return out


@router.get("/summary")
def transactions_summary(
    db: Session = Depends(get_db),
    account_id: str | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
):
    account = None
    if account_id:
        account = db.query(Account).filter_by(account_id=account_id).one_or_none()
        if account is None:
            raise HTTPException(status_code=404, detail="Account not found")
    return analytics.account_summary(db, account, start_date, end_date)


@router.get("/categories")
def list_categories(db: Session = Depends(get_db)):
    rows = db.query(Transaction.category_primary).distinct().all()
    return sorted({r[0] for r in rows})
