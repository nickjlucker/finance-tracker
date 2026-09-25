from datetime import date, timedelta
from types import SimpleNamespace

from app import analytics
from app.models import Account, CardLiability, Holding, PlaidItem, Security, Transaction
from app.routers.transactions import _upsert_transaction


def make_item(db, institution_name="Test Bank"):
    item = PlaidItem(item_id=f"item-{institution_name}", access_token="access-test", institution_name=institution_name)
    db.add(item)
    db.flush()
    return item


def make_account(db, item, account_id, name="Checking", acct_type="depository", subtype="checking", balance=1000.0, available=1000.0):
    account = Account(
        account_id=account_id,
        item_id=item.id,
        name=name,
        type=acct_type,
        subtype=subtype,
        current_balance=balance,
        available_balance=available,
    )
    db.add(account)
    db.flush()
    return account


def make_txn(
    db,
    account,
    transaction_id,
    amount,
    day,
    name="Test",
    merchant_name=None,
    category_primary="GENERAL_MERCHANDISE",
    category_detailed="",
    canonical_merchant=None,
):
    txn = Transaction(
        transaction_id=transaction_id,
        account_id=account.id,
        name=name,
        merchant_name=merchant_name,
        amount=amount,
        date=day,
        category_primary=category_primary,
        category_detailed=category_detailed,
        canonical_merchant=canonical_merchant or (merchant_name or name),
    )
    db.add(txn)
    db.flush()
    return txn


def test_internal_transfer_not_spend_or_income(db_session):
    item = make_item(db_session)
    checking = make_account(db_session, item, "acct-checking")
    card = make_account(db_session, item, "acct-card", name="Card", acct_type="credit", balance=200.0, available=800.0)

    today = date.today()
    make_txn(
        db_session, checking, "txn-payment-out", 300.0, today, name="Card Payment",
        category_primary="LOAN_PAYMENTS", category_detailed="LOAN_PAYMENTS_CREDIT_CARD_PAYMENT",
    )
    make_txn(
        db_session, card, "txn-payment-in", -300.0, today, name="Payment Received",
        category_primary="TRANSFER_IN", category_detailed="TRANSFER_IN_OTHER_TRANSFER_IN",
    )

    analytics.reconcile_internal_transfers(db_session)

    assert analytics.spend_by_category(db_session, today - timedelta(days=1), today) == []
    assert analytics.income_total(db_session, today - timedelta(days=1), today) == 0.0


def test_credit_card_payment_excluded_from_spend(db_session):
    item = make_item(db_session)
    checking = make_account(db_session, item, "acct-checking")
    today = date.today()
    make_txn(
        db_session, checking, "txn-cc-payment", 500.0, today,
        category_primary="LOAN_PAYMENTS", category_detailed="LOAN_PAYMENTS_CREDIT_CARD_PAYMENT",
    )

    analytics.reconcile_internal_transfers(db_session)

    assert analytics.spend_by_category(db_session, today - timedelta(days=1), today) == []
    txn = db_session.query(Transaction).filter_by(transaction_id="txn-cc-payment").one()
    assert txn.is_internal_transfer is True
    assert txn.is_credit_card_payment is True


def test_investment_transfer_excluded_from_spend(db_session):
    item = make_item(db_session)
    checking = make_account(db_session, item, "acct-checking")
    today = date.today()
    make_txn(
        db_session, checking, "txn-invest", 1000.0, today, name="To Webull",
        category_primary="TRANSFER_OUT", category_detailed="TRANSFER_OUT_INVESTMENT_AND_RETIREMENT_FUNDS",
    )

    analytics.reconcile_internal_transfers(db_session)

    assert analytics.spend_by_category(db_session, today - timedelta(days=1), today) == []
    assert analytics.invested_amount(db_session, today - timedelta(days=1), today) == 1000.0
    txn = db_session.query(Transaction).filter_by(transaction_id="txn-invest").one()
    assert txn.is_internal_transfer is True
    assert txn.is_credit_card_payment is False  # only card payments get this flag


def test_same_account_charge_reversal_wash_not_double_counted(db_session):
    # Reproduces Bilt's real rent mechanic: a charge and its same-day reversal
    # land on the SAME account under non-transfer categories (RENT_AND_UTILITIES /
    # INCOME), so this can only be caught by amount+date matching, not category.
    item = make_item(db_session, institution_name="Bilt Rewards")
    card = make_account(db_session, item, "acct-bilt-card", acct_type="credit", balance=0.0, available=5000.0)
    today = date.today()
    make_txn(
        db_session, card, "txn-rent-charge", 2100.0, today, name="Bilt Housing Payment",
        category_primary="RENT_AND_UTILITIES", category_detailed="RENT_AND_UTILITIES_RENT",
        canonical_merchant="Bilt Rent",
    )
    make_txn(
        db_session, card, "txn-rent-reversal", -2100.0, today, name="Payment - Bilt Housing",
        category_primary="INCOME", category_detailed="INCOME_OTHER_INCOME", canonical_merchant="Bilt Rent",
    )

    analytics.reconcile_internal_transfers(db_session)

    assert analytics.spend_by_category(db_session, today - timedelta(days=1), today) == []
    charge = db_session.query(Transaction).filter_by(transaction_id="txn-rent-charge").one()
    assert charge.is_internal_transfer is True


def test_duplicate_transactions_not_double_counted(db_session):
    item = make_item(db_session)
    checking = make_account(db_session, item, "acct-checking")
    today = date.today()
    make_txn(db_session, checking, "txn-dup-1", 32.05, today, merchant_name="Poquito Mas", canonical_merchant="Poquito Mas")
    make_txn(
        db_session, checking, "txn-dup-2", 32.05, today + timedelta(days=1),
        merchant_name="Poquito Mas", canonical_merchant="Poquito Mas",
    )

    analytics.detect_duplicate_groups(db_session)

    rows = db_session.query(Transaction).filter_by(canonical_merchant="Poquito Mas").all()
    group_ids = {t.duplicate_group_id for t in rows}
    assert len(group_ids) == 1
    assert None not in group_ids

    # Duplicate detection flags for review — it never removes or zeroes a real charge.
    spend = analytics.spend_by_category(db_session, today - timedelta(days=1), today + timedelta(days=1))
    assert sum(amount for _, amount in spend) == 64.10

    flags = analytics.detect_flagged_transactions(db_session)
    duplicate_flags = [f for f in flags if "duplicate" in f["reason"]]
    assert len(duplicate_flags) == 1  # one flag per group, not one per transaction


def test_total_assets_not_confused_with_cash(db_session):
    item = make_item(db_session)
    make_account(db_session, item, "acct-checking", balance=1000.0, available=1000.0)
    make_account(
        db_session, item, "acct-brokerage", acct_type="investment", subtype="brokerage",
        balance=5000.0, available=0.0,
    )

    breakdown = analytics.asset_breakdown(db_session)
    assert breakdown["cash"] == 1000.0
    assert breakdown["total_assets"] == 6000.0
    assert breakdown["total_assets"] != breakdown["cash"]


def test_net_worth_equals_assets_minus_liabilities(db_session):
    item = make_item(db_session)
    make_account(db_session, item, "acct-checking", balance=1000.0, available=1000.0)
    make_account(
        db_session, item, "acct-brokerage", acct_type="investment", subtype="brokerage",
        balance=5000.0, available=0.0,
    )
    make_account(db_session, item, "acct-card", acct_type="credit", balance=300.0, available=700.0)

    breakdown = analytics.asset_breakdown(db_session)
    assert breakdown["net_worth"] == breakdown["total_assets"] - breakdown["debt"]
    assert breakdown["net_worth"] == 6000.0 - 300.0
    assert breakdown["liquid_position"] == 1000.0 - 300.0


def test_brokerage_cash_not_double_counted_with_holdings(db_session):
    item = make_item(db_session)
    brokerage = make_account(
        db_session, item, "acct-brokerage", acct_type="investment", subtype="brokerage",
        balance=5000.0, available=100.0,
    )
    before = analytics.asset_breakdown(db_session)["total_assets"]

    security = Security(security_id="sec-1", ticker_symbol="VTI", name="Vanguard Total Stock", type="etf")
    db_session.add(security)
    db_session.flush()
    db_session.add(
        Holding(
            account_id=brokerage.id, security_id=security.id, quantity=10,
            institution_price=250.0, institution_value=2500.0, cost_basis=2000.0,
        )
    )
    db_session.flush()

    after = analytics.asset_breakdown(db_session)["total_assets"]
    assert after == before  # holdings feed allocation views only, never additive to net worth


def test_sync_upsert_idempotent(db_session):
    item = make_item(db_session)
    account = make_account(db_session, item, "acct-checking")
    account_by_plaid_id = {"acct-checking": account}

    fake_txn = SimpleNamespace(
        transaction_id="plaid-txn-1", account_id="acct-checking", name="Coffee Shop", merchant_name="Coffee Shop",
        amount=5.50, iso_currency_code="USD", date=date.today(), pending=False,
        personal_finance_category=SimpleNamespace(primary="FOOD_AND_DRINK", detailed="FOOD_AND_DRINK_COFFEE"),
    )

    _upsert_transaction(db_session, account_by_plaid_id, fake_txn)
    db_session.commit()  # first sync request ends with a commit, same as the real pipeline
    _upsert_transaction(db_session, account_by_plaid_id, fake_txn)  # a second, separate sync sees the same delta
    db_session.commit()

    rows = db_session.query(Transaction).filter_by(transaction_id="plaid-txn-1").all()
    assert len(rows) == 1


def test_disconnect_relink_does_not_duplicate_transactions(db_session):
    item = make_item(db_session, institution_name="Chase")
    account = make_account(db_session, item, "acct-chase")
    make_txn(db_session, account, "plaid-txn-shared", 42.00, date.today())
    db_session.commit()

    db_session.delete(item)  # disconnect: cascades to accounts + transactions
    db_session.commit()

    assert db_session.query(Transaction).filter_by(transaction_id="plaid-txn-shared").count() == 0
    assert db_session.query(Account).filter_by(account_id="acct-chase").count() == 0

    new_item = make_item(db_session, institution_name="Chase")  # relink: same item_id string is free again
    new_account = make_account(db_session, new_item, "acct-chase")
    make_txn(db_session, new_account, "plaid-txn-shared", 42.00, date.today())

    assert db_session.query(Transaction).filter_by(transaction_id="plaid-txn-shared").count() == 1


def test_discretionary_merchant_never_becomes_bill(db_session):
    item = make_item(db_session)
    checking = make_account(db_session, item, "acct-checking")
    base = date.today() - timedelta(days=150)
    amounts = [12.0, 45.0, 30.0, 60.0, 18.0, 50.0]
    for i, amt in enumerate(amounts):
        make_txn(
            db_session, checking, f"txn-amazon-{i}", amt, base + timedelta(days=i * 25),
            name="AMAZON MKTPL*XXXX", category_primary="GENERAL_MERCHANDISE",
            category_detailed="GENERAL_MERCHANDISE_ONLINE_MARKETPLACES", canonical_merchant="Amazon",
        )

    results = analytics.analyze_recurring(db_session, "spend")
    amazon = next(r for r in results if r["canonical_merchant"] == "Amazon")
    assert amazon["tier"] not in ("fixed_obligation", "subscription")


def test_fixed_obligation_detected_for_rent(db_session):
    item = make_item(db_session)
    checking = make_account(db_session, item, "acct-checking")
    base = date.today() - timedelta(days=150)
    for i in range(5):
        make_txn(
            db_session, checking, f"txn-rent-{i}", 2100.0, base + timedelta(days=i * 30),
            name="Bilt Rent", category_primary="RENT_AND_UTILITIES", category_detailed="RENT_AND_UTILITIES_RENT",
            canonical_merchant="Bilt Rent",
        )

    results = analytics.analyze_recurring(db_session, "spend")
    rent = next(r for r in results if r["canonical_merchant"] == "Bilt Rent")
    assert rent["tier"] == "fixed_obligation"


def test_insurance_detected_as_fixed_obligation(db_session):
    item = make_item(db_session)
    checking = make_account(db_session, item, "acct-checking")
    base = date.today() - timedelta(days=150)
    for i in range(5):
        make_txn(
            db_session, checking, f"txn-ins-{i}", 17.67, base + timedelta(days=i * 31),
            name="Millennial Specialty Insurance", category_primary="GENERAL_SERVICES",
            category_detailed="GENERAL_SERVICES_INSURANCE", canonical_merchant="Millennial Specialty Insurance",
        )

    results = analytics.analyze_recurring(db_session, "spend")
    ins = next(r for r in results if r["canonical_merchant"] == "Millennial Specialty Insurance")
    assert ins["tier"] == "fixed_obligation"


def test_subscription_detected_by_flat_price_regardless_of_category(db_session):
    item = make_item(db_session)
    checking = make_account(db_session, item, "acct-checking")
    base = date.today() - timedelta(days=90)
    for i in range(4):
        make_txn(
            db_session, checking, f"txn-yt-{i}", 15.99, base + timedelta(days=i * 30),
            name="Youtube Premium", category_primary="ENTERTAINMENT", category_detailed="ENTERTAINMENT_TV_AND_MOVIES",
            canonical_merchant="Youtube Premium",
        )

    results = analytics.analyze_recurring(db_session, "spend")
    yt = next(r for r in results if r["canonical_merchant"] == "Youtube Premium")
    assert yt["tier"] == "subscription"


def test_recurring_override_persists_over_heuristic(db_session):
    item = make_item(db_session)
    checking = make_account(db_session, item, "acct-checking")
    base = date.today() - timedelta(days=150)
    for i in range(6):
        make_txn(
            db_session, checking, f"txn-mystery-{i}", 40.0 + i, base + timedelta(days=i * 25),
            name="Mystery Merchant", category_primary="GENERAL_MERCHANDISE", canonical_merchant="Mystery Merchant",
        )

    analytics.set_recurring_override(db_session, "Mystery Merchant", "fixed_obligation")
    db_session.flush()

    results = analytics.analyze_recurring(db_session, "spend")
    result = next(r for r in results if r["canonical_merchant"] == "Mystery Merchant")
    assert result["tier"] == "fixed_obligation"
    assert result["overridden"] is True
    assert result["heuristic_tier"] != "fixed_obligation"  # the heuristic's own opinion is unchanged


def test_liquidity_minimum_respects_bill_before_paycheck_order(db_session):
    # Rent lands 3 days out, the paycheck 10 days out. Netting the 14-day
    # totals says cash stays positive; walking the days shows it goes negative.
    item = make_item(db_session)
    checking = make_account(db_session, item, "acct-checking", balance=1000.0, available=1000.0)
    today = date.today()
    for i in range(5):
        make_txn(
            db_session, checking, f"txn-rent-{i}", 2100.0, today - timedelta(days=27 + i * 30),
            name="Bilt Rent", category_primary="RENT_AND_UTILITIES", category_detailed="RENT_AND_UTILITIES_RENT",
        )
    for i in range(6):
        make_txn(
            db_session, checking, f"txn-pay-{i}", -2500.0, today - timedelta(days=4 + i * 14),
            name="Employer Payroll", category_primary="INCOME", category_detailed="INCOME_WAGES",
        )

    liquidity = analytics.compute_liquidity_breakdown(db_session)
    assert liquidity["confirmed_obligations_14d"] == 2100.0
    assert liquidity["expected_income_14d"] == 2500.0
    assert liquidity["projected_minimum_cash"] == 1000.0 - 2100.0
    assert liquidity["severity"] == "critical"


def test_merchant_name_keeps_plaid_casing():
    from app.merchant import normalize_merchant

    assert normalize_merchant("OPENAI *CHATGPT SUBSCR", "OpenAI") == "OpenAI"
    assert normalize_merchant("CVS", "CVS") == "CVS"
    assert normalize_merchant("GELSONS MKT #112", "Gelson's Markets") == "Gelson's Markets"


def test_merchant_raw_description_cleanup():
    from app.merchant import normalize_merchant

    payroll = "TLO INC DES:PAYROLL ID:0GZ99 A2B90JPM1 INDN:LUCKER, NICHOLAS CO ID:XXXXX89501 PPD"
    assert normalize_merchant(payroll, None) == "Tlo Inc"
    assert normalize_merchant("HANDEL'S HOMEMADE ICE CREAM", None) == "Handel's Homemade Ice Cream"
    assert normalize_merchant("Zelle Transfer Conf# RE4034KBF; KATHRYN GATHERUM-LUCKER", None) == "Zelle · Kathryn Gatherum-Lucker"
    assert normalize_merchant('Zelle payment from CONNOR TAYLOR for "dinner"; Conf# x1', None) == "Zelle · Connor Taylor"
    assert normalize_merchant("EFT 09/20 #XXXXX5997 WITHDRWL EFT Van Nuys CA FEE", None) == "ATM fee"
    assert normalize_merchant("EFT 09/20 #XXXXX5997 WITHDRWL EFT", None) == "ATM withdrawal"


def test_monthly_summary_skips_months_before_history(db_session):
    item = make_item(db_session)
    checking = make_account(db_session, item, "acct-checking")
    make_txn(db_session, checking, "txn-1", 25.0, date.today(), category_primary="FOOD_AND_DRINK")

    rows = analytics.monthly_income_spend_savings(db_session, months=6)
    assert len(rows) == 1
    assert rows[0]["spend"] == 25.0
    assert rows[0]["income"] == 0.0 and str(rows[0]["income"]) == "0.0"


def test_repeating_atm_fee_is_not_a_subscription(db_session):
    item = make_item(db_session)
    checking = make_account(db_session, item, "acct-checking")
    base = date.today() - timedelta(days=80)
    for i in range(5):
        make_txn(
            db_session, checking, f"txn-atm-{i}", 2.50, base + timedelta(days=i * 16),
            name="ATM fee", category_primary="BANK_FEES", category_detailed="BANK_FEES_ATM_FEES",
        )

    results = analytics.analyze_recurring(db_session, "spend")
    fee = next(r for r in results if r["canonical_merchant"] == "ATM fee")
    assert fee["tier"] not in analytics.OBLIGATION_TIERS


def test_card_side_payment_leg_paired_with_checking_payment(db_session):
    # Chase posts the card-side credit as LOAN_DISBURSEMENTS, not a payment
    # category, so it must be paired with the checking outflow by amount/date.
    item = make_item(db_session)
    checking = make_account(db_session, item, "acct-checking")
    card = make_account(db_session, item, "acct-card", name="Chase", acct_type="credit", subtype="credit card", balance=300.0)
    today = date.today()
    bank_leg = make_txn(
        db_session, checking, "txn-bank", 1800.0, today - timedelta(days=1), name="CHASE CREDIT CRD EPAY",
        category_primary="LOAN_PAYMENTS", category_detailed="LOAN_PAYMENTS_CREDIT_CARD_PAYMENT",
    )
    card_leg = make_txn(
        db_session, card, "txn-card", -1800.0, today - timedelta(days=2), name="Payment Thank You-Mobile",
        category_primary="LOAN_DISBURSEMENTS", category_detailed="LOAN_DISBURSEMENTS_OTHER_DISBURSEMENT",
    )
    refund = make_txn(db_session, card, "txn-refund", -55.21, today - timedelta(days=2), name="STATEMENT CREDIT", category_primary="OTHER")

    analytics.reconcile_internal_transfers(db_session)

    assert card_leg.is_credit_card_payment and card_leg.is_internal_transfer
    assert bank_leg.is_credit_card_payment
    assert card_leg.matched_transaction_id == bank_leg.id
    assert not refund.is_credit_card_payment


def test_orphaned_transfer_match_is_released(db_session):
    item = make_item(db_session)
    checking = make_account(db_session, item, "acct-checking")
    withdrawal = make_txn(db_session, checking, "txn-atm", 101.0, date.today(), category_primary="TRANSFER_OUT", category_detailed="TRANSFER_OUT_WITHDRAWAL")
    withdrawal.is_internal_transfer = True
    withdrawal.matched_transaction_id = 999_999  # partner removed by a later sync
    db_session.flush()

    analytics.reconcile_internal_transfers(db_session)

    assert withdrawal.matched_transaction_id is None
    assert withdrawal.is_internal_transfer is False


def test_forecast_card_bills_come_through_card_payment_not_directly(db_session):
    item = make_item(db_session)
    checking = make_account(db_session, item, "acct-checking", balance=1000.0)
    card = make_account(db_session, item, "acct-card", name="Card", acct_type="credit", subtype="credit card", balance=400.0)
    today = date.today()
    for i in range(4):
        make_txn(
            db_session, card, f"txn-phone-{i}", 50.0, today - timedelta(days=20 + i * 30),
            name="T-Mobile", category_primary="RENT_AND_UTILITIES", category_detailed="RENT_AND_UTILITIES_TELEPHONE",
        )
    db_session.add(CardLiability(account_id=card.id, last_statement_balance=400.0, next_payment_due_date=today + timedelta(days=5)))
    db_session.flush()

    forecast = analytics.forecast_cash_flow(db_session, days=30)
    kinds = {(e["kind"], e["label"]) for e in forecast["events"]}
    assert ("card_payment", "Test Bank card payment") in kinds
    assert not any(label == "T-Mobile" for _, label in kinds)  # already inside the card payment
    assert forecast["lowest_balance"] == 1000.0 - 400.0


def test_account_summary_for_credit_card(db_session):
    item = make_item(db_session, institution_name="Chase")
    checking = make_account(db_session, item, "acct-checking")
    card = make_account(db_session, item, "acct-card", name="CREDIT CARD", acct_type="credit", subtype="credit card", balance=500.0, available=4500.0)
    card.mask = "9018"
    today = date.today()
    make_txn(db_session, card, "t1", 120.0, today - timedelta(days=3), name="Gelson's", category_primary="FOOD_AND_DRINK")
    make_txn(db_session, card, "t2", 80.0, today - timedelta(days=5), name="Gelson's", category_primary="FOOD_AND_DRINK")
    make_txn(db_session, card, "t3", 40.0, today - timedelta(days=6), name="Uber", category_primary="TRANSPORTATION")
    make_txn(db_session, card, "t4", 12.34, today - timedelta(days=7), name="Interest", category_primary="BANK_FEES", category_detailed="BANK_FEES_INTEREST_CHARGE")
    make_txn(db_session, checking, "t5", 999.0, today - timedelta(days=2), name="Elsewhere", category_primary="FOOD_AND_DRINK")
    db_session.add(CardLiability(account_id=card.id, last_statement_balance=300.0, minimum_payment_amount=25.0, next_payment_due_date=today + timedelta(days=10)))
    db_session.flush()

    s = analytics.account_summary(db_session, card)

    assert s["account_name"] == "Chase card ··9018"
    assert s["credit"]["owed"] == 500.0
    assert s["credit"]["credit_limit"] == 5000.0
    assert s["credit"]["utilization"] == 0.1
    assert s["credit"]["days_until_due"] == 10
    assert s["credit"]["statement_remaining"] == 300.0
    assert s["interest_ttm"] == 12.34
    assert s["spend_total"] == 252.34  # checking's $999 isn't included
    assert s["top_categories"][0] == {"name": "FOOD_AND_DRINK", "amount": 200.0, "count": 2, "share": round(200 / 252.34, 4)}
    assert s["top_merchants"][0]["name"] == "Gelson's"


def test_reconstructed_net_worth_walks_back_from_first_snapshot(db_session):
    from app.models import BalanceSnapshot

    item = make_item(db_session)
    checking = make_account(db_session, item, "acct-checking", balance=1000.0)
    card = make_account(db_session, item, "acct-card", acct_type="credit", subtype="credit card", balance=200.0)
    today = date.today()
    make_txn(db_session, checking, "pay", -2000.0, today - timedelta(days=2), category_primary="INCOME")  # deposit
    make_txn(db_session, card, "buy", 50.0, today - timedelta(days=1))  # card purchase
    db_session.add_all([
        BalanceSnapshot(account_id=checking.id, balance=1000.0, recorded_at=today),
        BalanceSnapshot(account_id=card.id, balance=200.0, recorded_at=today),
    ])
    db_session.flush()

    history = {p["date"]: p for p in analytics.net_worth_history(db_session)}

    assert history[today]["net_worth"] == 800.0 and not history[today]["estimated"]
    # Points are end-of-day values.
    assert history[today - timedelta(days=1)]["net_worth"] == 800.0  # purchase already made
    assert history[today - timedelta(days=2)]["net_worth"] == 850.0  # after the deposit, before the purchase
    assert history[today - timedelta(days=2)]["estimated"]
    assert min(history) == today - timedelta(days=2)  # nothing before the first transaction


def test_daily_spending_splits_by_category_and_sets_bills_aside(db_session):
    item = make_item(db_session)
    checking = make_account(db_session, item, "acct-checking")
    today = date.today()
    for i in range(4):  # a monthly rent bill
        make_txn(
            db_session, checking, f"rent-{i}", 2100.0, today - timedelta(days=5 + i * 30),
            name="Bilt Rent", category_primary="RENT_AND_UTILITIES", category_detailed="RENT_AND_UTILITIES_RENT",
        )
    make_txn(db_session, checking, "coffee", 6.0, today - timedelta(days=1), name="Starbucks", category_primary="FOOD_AND_DRINK")
    make_txn(db_session, checking, "lunch", 14.0, today - timedelta(days=1), name="Cava", category_primary="FOOD_AND_DRINK")
    make_txn(db_session, checking, "uber", 20.0, today - timedelta(days=1), name="Uber", category_primary="TRANSPORTATION")
    make_txn(db_session, checking, "refund", -30.0, today - timedelta(days=1), name="Refund", category_primary="FOOD_AND_DRINK")

    everyday = analytics.daily_spending(db_session, start=today - timedelta(days=9), end=today)
    by_date = {d["date"]: d for d in everyday["days"]}
    assert len(everyday["days"]) == 10  # every day present, including no-spend days
    assert by_date[today - timedelta(days=1)]["categories"] == {"FOOD_AND_DRINK": 20.0, "TRANSPORTATION": 20.0}
    assert by_date[today - timedelta(days=5)]["total"] == 0  # rent set aside by default
    assert everyday["bills_excluded"] == 2100.0
    assert everyday["no_spend_days"] == 9

    with_bills = analytics.daily_spending(db_session, start=today - timedelta(days=9), end=today, include_bills=True)
    assert {d["date"]: d for d in with_bills["days"]}[today - timedelta(days=5)]["total"] == 2100.0


def test_xirr_matches_simple_compounding():
    start = date(2025, 1, 1)  # a 365-day year
    rate = analytics.xirr([(start, -1000.0), (date(2026, 1, 1), 1100.0)])
    assert abs(rate - 0.10) < 1e-4
    assert analytics.xirr([(start, -1000.0)]) is None  # nothing to compare against


def test_investment_performance_with_full_brokerage_history(db_session):
    from app.models import InvestmentTransaction

    item = make_item(db_session, institution_name="Webull")
    item.investments_status = "ok"
    brokerage = make_account(db_session, item, "acct-brokerage", name="Individual", acct_type="investment", subtype="brokerage", balance=12100.0)
    voo = Security(security_id="sec-voo", ticker_symbol="VOO", name="Vanguard S&P 500", type="etf")
    db_session.add(voo)
    db_session.flush()
    db_session.add(Holding(account_id=brokerage.id, security_id=voo.id, quantity=20.0, institution_price=600.0, institution_value=12000.0, cost_basis=10000.0))
    start = date.today() - timedelta(days=365)
    db_session.add_all([
        InvestmentTransaction(investment_transaction_id="d1", account_id=brokerage.id, date=start, type="cash", subtype="deposit", amount=-10000.0),
        InvestmentTransaction(investment_transaction_id="b1", account_id=brokerage.id, security_id=voo.id, date=start, type="buy", subtype="buy", amount=10000.0, quantity=20.0, price=500.0),
        InvestmentTransaction(investment_transaction_id="v1", account_id=brokerage.id, security_id=voo.id, date=start + timedelta(days=180), type="cash", subtype="dividend", amount=-100.0),
    ])
    db_session.flush()

    perf = analytics.investment_performance(db_session)

    assert perf["access"] == "ok"
    assert perf["net_contributed"] == 10000.0
    assert perf["full_history"] is True
    assert perf["total_gain"] == 2100.0  # value 12,100 - contributed 10,000
    assert perf["unrealized_gain"] == 2000.0
    assert perf["dividends"] == 100.0
    assert abs(perf["annualized_return"] - 0.21) < 0.005
    assert perf["holdings"][0]["ticker"] == "VOO" and perf["holdings"][0]["gain_pct"] == 0.2


def test_investment_performance_without_consent_falls_back_to_bank_transfers(db_session):
    item = make_item(db_session, institution_name="Webull")
    item.investments_status = "consent_required"
    make_account(db_session, item, "acct-brokerage", acct_type="investment", subtype="brokerage", balance=41000.0)
    bank_item = make_item(db_session, institution_name="Bank")
    checking = make_account(db_session, bank_item, "acct-checking")
    make_txn(db_session, checking, "to-webull", 1000.0, date.today() - timedelta(days=30), category_primary="TRANSFER_OUT", category_detailed="TRANSFER_OUT_INVESTMENT_AND_RETIREMENT_FUNDS")

    perf = analytics.investment_performance(db_session)

    assert perf["access"] == "consent_required"
    assert perf["items_needing_consent"][0]["institution_name"] == "Webull"
    assert perf["contributions_source"] == "bank_transfers" and perf["net_contributed"] == 1000.0
    assert perf["total_gain"] is None  # can't claim a gain without the full history


def test_price_series_forward_fills_weekends(db_session):
    from app import market_data
    from app.models import SecurityPrice

    fri, mon = date(2026, 9, 18), date(2026, 9, 21)
    db_session.add_all([SecurityPrice(ticker="NVDA", date=fri, close=100.0), SecurityPrice(ticker="NVDA", date=mon, close=110.0)])
    db_session.flush()

    series = market_data.price_series(db_session, "NVDA", fri, mon)

    assert series == {fri: 100.0, fri + timedelta(days=1): 100.0, fri + timedelta(days=2): 100.0, mon: 110.0}


def test_portfolio_value_history_prices_shares_and_rolls_back_trades(db_session):
    from app.models import InvestmentTransaction, SecurityPrice

    item = make_item(db_session, institution_name="Webull")
    brokerage = make_account(db_session, item, "acct-brokerage", acct_type="investment", subtype="brokerage", balance=1100.0)
    nvda = Security(security_id="sec-nvda", ticker_symbol="NVDA", name="NVIDIA", type="equity")
    option = Security(security_id="sec-opt", ticker_symbol="HIMS261016C00040000", name="HIMS call", type="derivative")
    db_session.add_all([nvda, option])
    db_session.flush()
    db_session.add_all([
        Holding(account_id=brokerage.id, security_id=nvda.id, quantity=10.0, institution_value=1080.0),
        Holding(account_id=brokerage.id, security_id=option.id, quantity=1.0, institution_value=20.0),
    ])
    today = date.today()
    d0, d1 = today - timedelta(days=2), today - timedelta(days=1)
    db_session.add_all([
        SecurityPrice(ticker="NVDA", date=d0, close=100.0),
        SecurityPrice(ticker="NVDA", date=d1, close=105.0),
        SecurityPrice(ticker="NVDA", date=today, close=108.0),
        # Bought 4 of the 10 shares yesterday with cash already in the account.
        InvestmentTransaction(investment_transaction_id="buy", account_id=brokerage.id, security_id=nvda.id, date=d1, type="buy", subtype="buy", amount=420.0, quantity=4.0, price=105.0),
    ])
    db_session.flush()

    result = analytics.portfolio_value_history(db_session, [brokerage.id], d0, today)
    series = result["series"]

    assert series[today] == 10 * 108.0 + 20.0  # cash 0 + shares + option held at today's value
    assert series[d1] == 10 * 105.0 + 20.0
    # Before the buy: 6 shares plus the $420 that was still cash.
    assert series[d0] == 6 * 100.0 + 420.0 + 20.0
    assert result["unpriced"] == ["HIMS261016C00040000"]


def _paycheck_history(db_session, checking, count=6, amount=2500.0):
    today = date.today()
    for i in range(count):
        make_txn(
            db_session, checking, f"pay-{i}", -amount, today - timedelta(days=2 + i * 14),
            name="Employer Payroll", category_primary="INCOME", category_detailed="INCOME_WAGES",
        )


def test_plan_scores_each_pay_period_against_targets(db_session):
    from app import plan

    item = make_item(db_session)
    checking = make_account(db_session, item, "acct-checking", balance=5000.0)
    _paycheck_history(db_session, checking)
    today = date.today()
    # $600 to investing in the latest *complete* period, $200 in the one before.
    make_txn(db_session, checking, "inv-a", 600.0, today - timedelta(days=15), category_primary="TRANSFER_OUT", category_detailed="TRANSFER_OUT_INVESTMENT_AND_RETIREMENT_FUNDS")
    make_txn(db_session, checking, "inv-b", 200.0, today - timedelta(days=29), category_primary="TRANSFER_OUT", category_detailed="TRANSFER_OUT_INVESTMENT_AND_RETIREMENT_FUNDS")
    analytics.reconcile_internal_transfers(db_session)

    periods = plan.pay_period_history(db_session, plan.get_settings(db_session))
    by_start = {p["start"]: p for p in periods}

    assert by_start[today - timedelta(days=16)]["status"] == "hit"  # $600 >= $500 minimum
    assert by_start[today - timedelta(days=30)]["status"] == "missed"
    assert by_start[today - timedelta(days=2)]["complete"] is False
    assert plan._streak(periods) == 1


def test_plan_splits_target_between_buffer_and_investing(db_session):
    from app import plan

    item = make_item(db_session)
    checking = make_account(db_session, item, "acct-checking", balance=1000.0)
    _paycheck_history(db_session, checking)
    plan.update_settings(db_session, {"emergency_target": 1200.0})

    result = plan.build_plan(db_session)

    assert result["buffer"]["gap"] == 200.0
    assert result["action"] == {"kind": "split", "amount": 500.0, "to_buffer": 200.0, "to_invest": 300.0}


def test_plan_counts_payroll_401k_and_separates_employer_hsa(db_session):
    from app import plan
    from app.models import InvestmentTransaction

    item = make_item(db_session, institution_name="Fidelity")
    k401 = make_account(db_session, item, "acct-401k", acct_type="investment", subtype="401k", balance=6000.0)
    hsa = make_account(db_session, item, "acct-hsa", acct_type="investment", subtype="hsa", balance=5000.0)
    fund = Security(security_id="sec-fund", ticker_symbol="JLGMX", name="JPM Large Cap Growth", type="mutual fund")
    db_session.add(fund)
    db_session.flush()
    day = date.today() - timedelta(days=3)
    db_session.add_all([
        InvestmentTransaction(investment_transaction_id="c1", account_id=k401.id, security_id=fund.id, date=day, type="cash", subtype="contribution", amount=-247.17, quantity=2.8, name="JPM LG CAP GROWTH R6 - contribution"),
        InvestmentTransaction(investment_transaction_id="e1", account_id=hsa.id, date=day, type="cash", subtype="deposit", amount=-100.0, name="CO CONTR CURRENT YR EMPLOYER CUR YR (Cash)"),
        InvestmentTransaction(investment_transaction_id="p1", account_id=hsa.id, date=day, type="cash", subtype="deposit", amount=-25.0, name="PARTIC CONTR CURRENT PARTICIPANT CUR YR (Cash)"),
    ])
    db_session.flush()

    out = plan._period_investing(db_session, day - timedelta(days=1), day, plan._investment_accounts(db_session))

    assert out == {"from_checking": 0, "retirement_payroll": 247.17, "hsa_you": 25.0, "employer": 100.0}
