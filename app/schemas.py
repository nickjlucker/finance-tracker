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


class DashboardSummary(BaseModel):
    cash: float
    debt: float
    investments: float
    net_position: float
    account_count: int
    month_spent: float
    month_income: float
    month_invested: float
    interest_paid_ttm: float
    spend_by_category: list[dict]
    budget_count: int


class InstitutionOut(BaseModel):
    item_id: str
    institution_name: str
    account_count: int


class AlertOut(BaseModel):
    severity: str
    message: str


class RecurringBillOut(BaseModel):
    merchant: str
    average_amount: float
    cadence_days: int
    last_date: date
    next_expected_date: date


class FlaggedTransactionOut(BaseModel):
    transaction_id: str | None
    date: date
    name: str
    amount: float
    reason: str


class NetWorthPointOut(BaseModel):
    date: date
    net_worth: float


class ProjectionPointOut(BaseModel):
    years: int
    projected_value: float


class NetWorthOut(BaseModel):
    history: list[NetWorthPointOut]
    tracking_since: date | None
    monthly_net_savings: float
    annual_return_pct: float
    projection: list[ProjectionPointOut]
