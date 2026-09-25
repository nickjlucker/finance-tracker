# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added
- **Wealth plan** (`/plan`, `GET /api/plan`, `PUT /api/plan/settings`):
  - Per-paycheck investing target (minimum and stretch, default $500 / $750)
    scored for every pay period, with an on-target streak.
  - "This paycheck" instruction: how much to move to savings vs. investing,
    filling a cash buffer (months of essential costs) first. Also shown at
    the top of the Dashboard.
  - Investing per pay period split into money moved from checking, 401(k)
    payroll contributions, your HSA contributions, and employer money.
  - The gap: monthly take-home vs. spending, what the guardrails free up,
    and how that compares to the minimum and stretch targets.
  - Spending guardrails from the audit (Amazon, groceries, dining,
    rideshare, games, other shopping, ATM cash) with editable monthly caps,
    month-to-date pace, and biggest merchants.
  - Opportunities: HSA-reimbursable medical costs, idle HSA cash, HSA and
    401(k) room against 2026 limits, Roth IRA, three-paycheck months, and
    rent as a share of take-home.
  - Settings stored in a new `app_settings` table.
- **Investments view** on the Dashboard: value, net contributed, total
  growth, money-weighted annualized return (XIRR), dividends and fees, a
  contributed-vs-value chart, and holdings with cost basis, gain, and
  weight. Growth and return are only claimed when the history is complete
  (every current position is explained by trades in the window).
  `GET /api/dashboard/investments`.
- **Grant investment access** for brokerages linked with balance access only
  (Webull): Plaid Link update mode requests the Investments product on the
  existing connection (`POST /api/link/token/update/{item_id}`), then syncs.
  Each institution's investments status is stored (`plaid_items.investments_status`).
- Sync pulls up to 24 months of investment transactions (buys, sells,
  dividends, deposits, fees) into a new `investment_transactions` table.
- The net worth chart says when a brokerage's market growth is missing.
- **Market data** (`app/market_data.py`): daily split-adjusted closes from
  Yahoo Finance (no key), or Tiingo first when `TIINGO_API_KEY` is set, with
  the other as fallback. Cached in `security_prices` and topped up on sync
  (2 years back).
- Portfolio value history from share counts × closes. With the brokerage's
  trades, share counts roll back through them (real account value);
  without, today's positions are priced back in time and labeled as such.
  Positions with no market price (options) are held at today's value.
- Investments card: value chart with 1M–2Y ranges, 1M/3M/1Y returns, and
  1M and 1Y price change per holding.
- Reconstructed net worth prices brokerage balances from market closes
  (shifted to meet the first real snapshot, and net of deposits made later)
  instead of holding them flat.
- **Daily spending by category** on the Dashboard (last 30 days) and the
  Transactions page (follows the account and date filters): stacked bars per
  day (per week past ~3 months) with a hover breakdown, an average line, a
  legend with category totals, and average per day, highest day, no-spend
  days, and the priciest weekday. Fixed bills are set aside by default (the
  amount is shown) with an "Include bills" toggle. Clicking a bar shows that
  day's transactions. Category colors come from a fixed, colorblind-validated
  8-slot order; everything else folds into "Other". `GET /api/transactions/daily`.

## 2026-09-25 (`4da6fbb`)

### Added
- **Merchant normalization** (`app/merchant.py`): strips ACH/processor noise
  (`DES:`, `ID:`, `PPD`, store numbers, `*REF` suffixes) and applies curated
  aliases (e.g. Bilt Rent, Uber) so the same merchant groups together.
  Stored on each transaction as `canonical_merchant`; existing rows are
  backfilled on sync.
- **Recurring activity analysis**, replacing the simple bills list. Groups by
  canonical merchant, scores confidence from cadence and amount regularity plus
  a category prior, and assigns a tier: `fixed_obligation`, `subscription`,
  `recurring_discretionary`, `probable_recurrence`, `not_recurring`,
  `reliable_income`, `probable_income`. The dashboard groups these as
  Income, Bills & subscriptions, and Maybe recurring; frequently visited
  merchants (groceries, dining, Amazon orders) sit in a collapsed
  "Frequent spending" section since they aren't bills.
  - `GET /api/dashboard/recurring/income`: recurring income streams.
  - `PUT` / `DELETE /api/dashboard/recurring/{merchant}/override`: manual
    reclassification, persisted in a new `recurring_overrides` table.
- **Cash-flow forecast** (`GET /api/dashboard/forecast`): 30-day projected
  checking balance built from four inputs: recurring paychecks; bills paid
  directly from checking; card payments due (Plaid statement balance and due
  date, net of payments made since the statement closed, or the card balance
  on its usual payment rhythm when no statement data exists); and everyday
  checking spending (ATM, Zelle, debit) at its 90-day daily average.
  Card-charged bills flow through the card payment instead of being
  subtracted twice. Returns every dated event plus per-input totals, the
  lowest point, a configurable safety floor, and excess liquidity.
- **Liquidity breakdown** on `GET /api/dashboard/alerts`: available cash,
  obligations and income due in the next 14 days, and projected minimum cash,
  computed by walking the days in order (the same projection as the forecast).
- **Monthly summary** (`GET /api/dashboard/monthly-summary`): income, spend,
  and savings per month.
- **Investment holdings** via Plaid Investments: new `securities` and
  `holdings` tables, refreshed during `/api/sync/full`, exposed at
  `GET /api/dashboard/holdings`.
- **Duplicate detection**: transactions that look like duplicates are grouped
  under `duplicate_group_id`.
- **Credit card payment flag** (`is_credit_card_payment`) and detection of
  same-account charge/reversal washes that don't use a `TRANSFER_*` category.
- Net worth response includes `data_confidence` (e.g. `provisional` while
  history is short).
- Dashboard UI: stat tiles for investments, total assets, debt, net worth,
  and liquid position; a liquidity breakdown; a forecast chart with an
  adjustable safety floor; income/spend/savings and net worth charts;
  recurring activity with override controls; and a holdings card.
- **UI redesign** across Dashboard, Transactions, and Budgets: a shared
  sidebar shell (collapses to a top bar on phones) with Sync, Connect account,
  and last-synced status; one stylesheet with light and automatic dark mode.
  - Dashboard is a grid: KPI row, 30-day forecast beside the 14-day cash check,
    income vs. spending beside a dated "Coming up" list, recurring and
    net worth beside spending by category and flagged transactions, and one
    Accounts card grouped by institution with inline disconnect confirmation.
  - Transactions are grouped by day with cleaned merchant names (raw bank
    description underneath), category badges, incoming amounts in green, an
    account filter, and live search.
  - Budgets shows pace against the month, amount left or over, edit/remove,
    and an "Unbudgeted spending" list with suggested limits.
- **Account metrics on the Transactions page** (`GET /api/transactions/summary`),
  following the account and date filters (default: last 30 days).
  - Credit cards: amount owed and utilization of the limit, payment due date
    with days left, statement balance still unpaid and minimum status, last
    payment, interest over 12 months and APR.
  - Checking: balance, money in with the last paycheck, money out plus card
    payments made, and fees.
  - Investment: balance, holdings value, unrealized gain, contributions.
  - Every account (and "all accounts"): spending vs. the previous period,
    average per day, largest purchase, recurring charges on the account, and
    top categories and merchants, each clickable to filter the table.
  - Dashboard account names link to `/transactions?account=<id>`.
- **Net worth over time** is explorable: 1M / 3M / 6M / 1Y / All ranges,
  an optional +1 / +5 / +10 year projection drawn as a dashed line, change
  over the selected range, and a hover readout. History before the first
  sync is reconstructed daily from transactions (anchored on the first real
  snapshot so the two join without a jump) and drawn lighter; brokerage
  balances only move by contributions there, since market gains can't be
  rebuilt. `GET /api/dashboard/networth` adds `estimated` per point,
  `projection_series`, and `reconstructed_until`.
- `last_synced` on the dashboard summary and `canonical_merchant` on
  transactions; transaction search also matches merchant names.
- Merchant cleanup turns Zelle descriptions into "Zelle · Name" and EFT
  withdrawal lines into "ATM withdrawal" / "ATM fee".
- Test suite (`tests/`) covering the analytics logic; `pytest` and `httpx`
  added to `requirements.txt`.

### Changed
- Dashboard summary now returns `total_assets`, `net_worth`, and
  `liquid_position` in place of `net_position` (`cash_debt_net` became
  `asset_breakdown`).
- `GET /api/dashboard/alerts` now returns `{alerts, liquidity}` instead of a
  bare list.
- `GET /api/dashboard/recurring` now returns `RecurringItemOut` (tier,
  confidence, override status) instead of `RecurringBillOut`.
- `PLAID_PRODUCTS` in `.env.example` now includes `liabilities,investments`.
- Startup migrations are table-driven and add the new transaction columns
  to existing databases.

### Fixed
- 401(k) payroll contributions that land directly in a fund (with shares)
  are counted as contributions; employer contributions are identified; bank
  transfers into investing are combined with brokerage-reported deposits
  without double counting.
- Credit card payments no longer look like income on the Transactions page.
  The card-side leg (which Chase labels `LOAN_DISBURSEMENTS` and Bank of
  America `TRANSFER_IN`) is paired with the checking payment by amount and
  date and flagged `is_credit_card_payment`. Both legs render as neutral
  "Payment to/from <account>" rows, and a toggle hides card payments and
  transfers.
- Transfer pairings whose partner was deleted on a later sync are released,
  so the surviving transaction counts as spend/income again.
- Bank fees and cash withdrawals can no longer be classified as bills or
  subscriptions (a flat recurring ATM fee looked like one).
- Disconnecting an institution now deletes its `CardLiability` and `Holding`
  rows instead of leaving them orphaned.

## 2026-09-22: Accounting fixes, net worth, and alerts (`1b5949f`)

### Fixed
- Total balance treated credit card debt as an asset. It is now split into
  cash, debt, and net position by account type.
- "Spent this month" double-counted credit card statement payments. Transfers
  are now classified: Plaid's dedicated card payment and investment
  categories are excluded, and ambiguous `TRANSFER_*` transactions are
  excluded only when matched to an opposite-direction transaction on another
  owned account.

### Added
- `POST /api/sync/full`: refreshes balances, syncs transactions, reconciles
  transfers, pulls Liabilities, and snapshots balances.
- Net worth history and projection, critical alerts, and flagged transactions.

## 2026-09-22: Initial release (`9399e14`)

### Added
- FastAPI + Plaid personal finance tracker: account linking, transaction
  sync, budgets, and a dashboard.
