import logging

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app import analytics, plaid_client
from app.database import get_db
from app.models import Account, CardLiability, PlaidItem, Transaction
from app.routers.transactions import _upsert_transaction

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/sync", tags=["sync"])


def _refresh_balances(db: Session, item: PlaidItem) -> None:
    plaid_accounts = plaid_client.get_accounts(item.access_token)
    by_account_id = {a.account_id: a for a in plaid_accounts}
    for account in item.accounts:
        plaid_acct = by_account_id.get(account.account_id)
        if not plaid_acct:
            continue
        account.current_balance = plaid_acct.balances.current
        account.available_balance = plaid_acct.balances.available


def _sync_item_transactions(db: Session, item: PlaidItem) -> dict:
    result = plaid_client.sync_transactions(item.access_token, item.transactions_cursor)
    account_by_plaid_id = {a.account_id: a for a in item.accounts}

    for txn in result["added"]:
        _upsert_transaction(db, account_by_plaid_id, txn)
    for txn in result["modified"]:
        _upsert_transaction(db, account_by_plaid_id, txn)
    for removed in result["removed"]:
        db.query(Transaction).filter_by(transaction_id=removed.transaction_id).delete()

    item.transactions_cursor = result["cursor"]
    return result


def _refresh_liabilities(db: Session, item: PlaidItem) -> int:
    try:
        credit_liabilities = plaid_client.get_liabilities(item.access_token)
    except Exception:
        logger.info("Liabilities not available for item %s (product not granted yet)", item.item_id)
        return 0

    account_by_plaid_id = {a.account_id: a for a in item.accounts}
    updated = 0
    for liability in credit_liabilities:
        account = account_by_plaid_id.get(liability.account_id)
        if account is None:
            continue
        apr_purchase = None
        for apr in liability.aprs or []:
            if apr.apr_type == "purchase_apr":
                apr_purchase = apr.apr_percentage
                break

        record = db.query(CardLiability).filter_by(account_id=account.id).one_or_none()
        if record is None:
            record = CardLiability(account_id=account.id)
            db.add(record)
        record.apr_purchase = apr_purchase
        record.last_statement_balance = liability.last_statement_balance
        record.minimum_payment_amount = liability.minimum_payment_amount
        record.next_payment_due_date = liability.next_payment_due_date
        record.is_overdue = liability.is_overdue
        record.last_payment_amount = liability.last_payment_amount
        record.last_payment_date = liability.last_payment_date
        updated += 1
    return updated


@router.post("/full")
def sync_full(db: Session = Depends(get_db)):
    items = db.query(PlaidItem).all()
    totals = {"added": 0, "modified": 0, "removed": 0, "liabilities_updated": 0}

    for item in items:
        try:
            _refresh_balances(db, item)
        except Exception as exc:
            logger.warning("Balance refresh failed for %s: %s", item.institution_name, exc)

        result = _sync_item_transactions(db, item)
        totals["added"] += len(result["added"])
        totals["modified"] += len(result["modified"])
        totals["removed"] += len(result["removed"])

        totals["liabilities_updated"] += _refresh_liabilities(db, item)

    db.flush()
    analytics.reconcile_internal_transfers(db)
    analytics.record_balance_snapshots(db)
    db.commit()

    return totals
