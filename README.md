# Personal Finance Tracker

FastAPI + Plaid app for linking bank accounts, syncing transactions, and tracking
budgets by category. Local SQLite storage, server-rendered UI.

## Setup

```bash
cd ~/finance-tracker
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# edit .env and fill in PLAID_CLIENT_ID / PLAID_SECRET from your Plaid dashboard
# (https://dashboard.plaid.com/team/keys). PLAID_ENV=sandbox works with test
# credentials (e.g. username "user_good", password "pass_good" at any sandbox
# institution) with no real bank account required.
```

## Run

```bash
source .venv/bin/activate
uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000

- **Dashboard** — connect an account via Plaid Link, see balances and spend by category.
- **Transactions** — search/filter synced transactions.
- **Budgets** — set a monthly limit per Plaid category, track spend against it.

Transactions are pulled with Plaid's `/transactions/sync` endpoint (cursor-based,
incremental) — hit "Sync transactions" after linking or whenever you want fresh data.

## Notes

- Storage is a single `finance.db` SQLite file (gitignored); delete it to reset.
- Access tokens are stored in plaintext in that file — fine for local personal use,
  not for anything shared or deployed as-is.
- Categories come from Plaid's `personal_finance_category` taxonomy (e.g.
  `FOOD_AND_DRINK`, `TRANSPORTATION`) rather than free-form tags.
