import logging
from datetime import date, timedelta

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app import analytics, market_data, plaid_client
from app.database import get_db
from app.models import Account, CardLiability, Holding, InvestmentTransaction, PlaidItem, Security, Transaction
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


INVESTMENT_HISTORY_DAYS = 730  # Plaid serves up to 24 months of investment transactions
_CONSENT_ERRORS = {"ADDITIONAL_CONSENT_REQUIRED", "INVALID_PRODUCT"}
_UNSUPPORTED_ERRORS = {"PRODUCTS_NOT_SUPPORTED", "NO_INVESTMENT_ACCOUNTS", "PRODUCT_NOT_ENABLED"}


def _upsert_securities(db: Session, securities: list) -> dict[str, Security]:
    by_plaid_id: dict[str, Security] = {}
    for sec in securities:
        record = db.query(Security).filter_by(security_id=sec.security_id).one_or_none()
        if record is None:
            record = Security(security_id=sec.security_id)
            db.add(record)
        record.ticker_symbol = sec.ticker_symbol
        record.name = sec.name
        record.type = str(sec.type) if sec.type is not None else None
        by_plaid_id[sec.security_id] = record
    db.flush()
    return by_plaid_id


def _refresh_holdings(db: Session, item: PlaidItem) -> int:
    has_investment_accounts = any(a.type == "investment" for a in item.accounts)
    try:
        securities, holdings = plaid_client.get_holdings(item.access_token)
    except Exception as exc:
        code = plaid_client.plaid_error_code(exc)
        if code in _CONSENT_ERRORS and has_investment_accounts:
            item.investments_status = "consent_required"
        elif has_investment_accounts:
            item.investments_status = "unsupported" if code in _UNSUPPORTED_ERRORS else item.investments_status
        logger.info("Investments not available for %s: %s", item.institution_name, code or exc)
        return 0
    item.investments_status = "ok"

    security_by_plaid_id = _upsert_securities(db, securities)

    account_by_plaid_id = {a.account_id: a for a in item.accounts}
    updated = 0
    for holding in holdings:
        account = account_by_plaid_id.get(holding.account_id)
        security = security_by_plaid_id.get(holding.security_id)
        if account is None or security is None:
            continue
        record = db.query(Holding).filter_by(account_id=account.id, security_id=security.id).one_or_none()
        if record is None:
            record = Holding(account_id=account.id, security_id=security.id)
            db.add(record)
        record.quantity = holding.quantity
        record.institution_price = holding.institution_price
        record.institution_value = holding.institution_value
        record.cost_basis = holding.cost_basis
        updated += 1
    return updated


def _refresh_investment_transactions(db: Session, item: PlaidItem) -> int:
    if item.investments_status != "ok":
        return 0
    end = date.today()
    start = end - timedelta(days=INVESTMENT_HISTORY_DAYS)
    try:
        securities, txns = plaid_client.get_investment_transactions(item.access_token, start, end)
    except Exception as exc:
        logger.warning("Investment transactions failed for %s: %s", item.institution_name, plaid_client.plaid_error_code(exc) or exc)
        return 0

    security_by_plaid_id = _upsert_securities(db, securities)
    account_by_plaid_id = {a.account_id: a for a in item.accounts}
    count = 0
    for t in txns:
        account = account_by_plaid_id.get(t.account_id)
        if account is None:
            continue
        record = db.query(InvestmentTransaction).filter_by(investment_transaction_id=t.investment_transaction_id).one_or_none()
        if record is None:
            record = InvestmentTransaction(investment_transaction_id=t.investment_transaction_id, account_id=account.id)
            db.add(record)
        security = security_by_plaid_id.get(t.security_id) if t.security_id else None
        record.security_id = security.id if security else None
        record.date = t.date
        record.name = t.name or ""
        record.type = str(t.type)
        record.subtype = str(t.subtype)
        record.amount = t.amount or 0.0
        record.quantity = t.quantity
        record.price = t.price
        record.fees = t.fees
        count += 1
    return count


@router.post("/full")
def sync_full(db: Session = Depends(get_db)):
    items = db.query(PlaidItem).all()
    totals = {"added": 0, "modified": 0, "removed": 0, "liabilities_updated": 0, "holdings_updated": 0, "investment_transactions": 0}

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
        totals["holdings_updated"] += _refresh_holdings(db, item)
        totals["investment_transactions"] += _refresh_investment_transactions(db, item)

    db.flush()
    tickers = [sec.ticker_symbol for _, sec in analytics.priceable_holdings(db)]
    if tickers:
        prices = market_data.refresh_prices(db, tickers, date.today() - timedelta(days=analytics.PRICE_HISTORY_DAYS))
        totals["prices_updated"] = sum(prices.values())
    analytics.backfill_canonical_merchants(db)
    analytics.reconcile_internal_transfers(db)
    analytics.detect_duplicate_groups(db)
    analytics.record_balance_snapshots(db)
    db.commit()

    return totals
