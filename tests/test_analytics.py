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
