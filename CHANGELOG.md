# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

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
