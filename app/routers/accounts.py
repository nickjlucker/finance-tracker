from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app import plaid_client
from app.database import get_db
from app.models import Account, PlaidItem
from app.schemas import AccountOut

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
