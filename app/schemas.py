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
    canonical_merchant: str = ""
    is_internal_transfer: bool = False
    is_credit_card_payment: bool = False
    account_name: str = ""
    counterparty_account: str | None = None


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
    investments: float
    total_assets: float
    debt: float
    net_worth: float
    liquid_position: float
    last_synced: date | None = None
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


class RecurringItemOut(BaseModel):
    canonical_merchant: str
    tier: str
    heuristic_tier: str
    overridden: bool
    confidence: float
    average_amount: float
    cadence_days: int | None
    transaction_count: int
    last_date: date
    next_expected_date: date | None
    category_primary: str
    paid_from: str = "cash"


class RecurringOverrideIn(BaseModel):
    classification: str


class FlaggedTransactionOut(BaseModel):
    transaction_id: str | None
    date: date
    name: str
    amount: float
    reason: str


class NetWorthPointOut(BaseModel):
    date: date
    net_worth: float
    estimated: bool = False


class ProjectionPointOut(BaseModel):
    years: int
    projected_value: float


class NetWorthOut(BaseModel):
    history: list[NetWorthPointOut]
    tracking_since: date | None
    data_confidence: str
    monthly_net_savings: float
    annual_return_pct: float
    projection: list[ProjectionPointOut]
    projection_series: list[NetWorthPointOut]
    reconstructed_until: date | None = None


class LiquidityBreakdownOut(BaseModel):
    available_cash: float
    confirmed_obligations_14d: float
    card_payments_14d: float
    everyday_spending_14d: float
    expected_income_14d: float
    projected_minimum_cash: float
    severity: str | None


class AlertsOut(BaseModel):
    alerts: list[AlertOut]
    liquidity: LiquidityBreakdownOut


class CashFlowPointOut(BaseModel):
    date: date
    projected_balance: float


class CashFlowEventOut(BaseModel):
    date: date
    label: str
    amount: float
    kind: str


class CashFlowForecastOut(BaseModel):
    series: list[CashFlowPointOut]
    events: list[CashFlowEventOut]
    lowest_balance: float
    lowest_balance_date: date
    total_expected_income: float
    total_expected_bills: float
    total_card_payments: float
    total_everyday_spending: float
    everyday_daily_rate: float
    discretionary_buffer: float
    excess_liquidity: float
    safety_floor: float


class HoldingOut(BaseModel):
    account_name: str
    ticker_symbol: str | None
    security_name: str | None
    quantity: float | None
    institution_value: float | None
    cost_basis: float | None


class MonthlySummaryOut(BaseModel):
    month: str
    income: float
    spend: float
    savings: float
    partial: bool
