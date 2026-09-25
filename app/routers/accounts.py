import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app import plaid_client
from app.database import get_db
from app.models import Account, CardLiability, Holding, PlaidItem
from app.schemas import AccountOut, InstitutionOut

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/accounts", tags=["accounts"])


@router.get("", response_model=list[AccountOut])
def list_accounts(db: Session = Depends(get_db)):
    accounts = db.query(Account).all()
    out = []
    for acct in accounts:
        data = AccountOut.model_validate(acct)
        data.institution_name = acct.item.institution_name
        out.append(data)
    return out


@router.get("/institutions", response_model=list[InstitutionOut])
def list_institutions(db: Session = Depends(get_db)):
    items = db.query(PlaidItem).all()
    return [
        InstitutionOut(item_id=item.item_id, institution_name=item.institution_name, account_count=len(item.accounts))
        for item in items
    ]


@router.delete("/institutions/{item_id}")
def disconnect_institution(item_id: str, db: Session = Depends(get_db)):
    item = db.query(PlaidItem).filter_by(item_id=item_id).one_or_none()
    if item is None:
        raise HTTPException(status_code=404, detail="Institution not found")

    try:
        plaid_client.remove_item(item.access_token)
    except Exception as exc:
        logger.warning("item_remove failed for %s, proceeding with local cleanup: %s", item.institution_name, exc)

    # CardLiability/Holding are plain FK columns (no ORM relationship/cascade),
    # so they'd otherwise survive as orphans once the PlaidItem->Account cascade
    # deletes the accounts they point to.
    account_ids = [a.id for a in item.accounts]
    if account_ids:
        db.query(CardLiability).filter(CardLiability.account_id.in_(account_ids)).delete(synchronize_session=False)
        db.query(Holding).filter(Holding.account_id.in_(account_ids)).delete(synchronize_session=False)

    db.delete(item)
    db.commit()
    return {"status": "ok"}


@router.post("/refresh")
def refresh_balances(db: Session = Depends(get_db)):
    items = db.query(PlaidItem).all()
    if not items:
        raise HTTPException(status_code=404, detail="No linked accounts yet")

    updated = 0
    for item in items:
        try:
            plaid_accounts = plaid_client.get_accounts(item.access_token)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Plaid error: {exc}") from exc

        by_account_id = {a.account_id: a for a in plaid_accounts}
        for account in item.accounts:
            plaid_acct = by_account_id.get(account.account_id)
            if not plaid_acct:
                continue
            account.current_balance = plaid_acct.balances.current
            account.available_balance = plaid_acct.balances.available
            updated += 1
    db.commit()
    return {"status": "ok", "accounts_updated": updated}
