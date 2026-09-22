from datetime import date

from pydantic import BaseModel, ConfigDict


class LinkTokenCreateResponse(BaseModel):
    link_token: str


class PublicTokenExchangeRequest(BaseModel):
    public_token: str


class AccountOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    account_id: str
    name: str
    official_name: str | None
    mask: str | None
    type: str
    subtype: str
    current_balance: float | None
    available_balance: float | None
    iso_currency_code: str | None
    institution_name: str = ""


class TransactionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    transaction_id: str
    name: str
    merchant_name: str | None
    amount: float
    iso_currency_code: str | None
    date: date
    pending: bool
    category_primary: str
    category_detailed: str
    account_name: str = ""


class BudgetIn(BaseModel):
    category_primary: str
    monthly_limit: float


class BudgetOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    category_primary: str
    monthly_limit: float
    spent: float = 0.0


class SyncResult(BaseModel):
    added: int
    modified: int
    removed: int
