from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app import plaid_client
from app.database import get_db
from app.models import Account, PlaidItem
from app.schemas import LinkTokenCreateResponse, PublicTokenExchangeRequest

router = APIRouter(prefix="/api/link", tags=["link"])

DEMO_USER_ID = "local-user"


@router.post("/token", response_model=LinkTokenCreateResponse)
def create_token():
    try:
        link_token = plaid_client.create_link_token(DEMO_USER_ID)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Plaid error: {exc}") from exc
    return LinkTokenCreateResponse(link_token=link_token)


@router.post("/token/update/{item_id}", response_model=LinkTokenCreateResponse)
def create_update_token(item_id: str, product: str = "investments", db: Session = Depends(get_db)):
    item = db.query(PlaidItem).filter_by(item_id=item_id).one_or_none()
    if item is None:
        raise HTTPException(status_code=404, detail="Institution not found")
    try:
        link_token = plaid_client.create_update_link_token(DEMO_USER_ID, item.access_token, [product])
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Plaid error: {exc}") from exc
    return LinkTokenCreateResponse(link_token=link_token)


@router.post("/exchange")
def exchange_token(payload: PublicTokenExchangeRequest, db: Session = Depends(get_db)):
    try:
        access_token, item_id = plaid_client.exchange_public_token(payload.public_token)
        institution_name = plaid_client.get_institution_name(access_token)
        plaid_accounts = plaid_client.get_accounts(access_token)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Plaid error: {exc}") from exc

    item = PlaidItem(item_id=item_id, access_token=access_token, institution_name=institution_name)
    db.add(item)
    db.flush()

    for acct in plaid_accounts:
        balances = acct.balances
        db.add(
            Account(
                account_id=acct.account_id,
                item_id=item.id,
                name=acct.name,
                official_name=acct.official_name,
                mask=acct.mask,
                type=str(acct.type),
                subtype=str(acct.subtype) if acct.subtype else "",
                current_balance=balances.current,
                available_balance=balances.available,
                iso_currency_code=balances.iso_currency_code,
            )
        )
    db.commit()
    return {"status": "ok", "institution_name": institution_name, "accounts_linked": len(plaid_accounts)}
