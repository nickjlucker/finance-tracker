import statistics
from collections import Counter, defaultdict
from datetime import date, timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

from app import market_data
from app.merchant import normalize_merchant
from app.models import Account, BalanceSnapshot, CardLiability, Holding, InvestmentTransaction, RecurringOverride, Security, SecurityPrice, Transaction

ASSET_TYPES = {"depository", "investment"}
LIABILITY_TYPES = {"credit", "loan"}
NEAR_TERM_LIABILITY_TYPES = {"credit"}

# Plaid's own dedicated categories for these are unambiguous about economic meaning,
# so no matching is required to treat them as internal (non-spend) transfers.
TIER1_CREDIT_CARD_PAYMENT_CATEGORIES = {"LOAN_PAYMENTS_CREDIT_CARD_PAYMENT"}
TIER1_INTERNAL_CATEGORIES = TIER1_CREDIT_CARD_PAYMENT_CATEGORIES | {
    "TRANSFER_OUT_INVESTMENT_AND_RETIREMENT_FUNDS",
}

# Categories a card-side payment credit arrives under (issuer-dependent).
CARD_PAYMENT_CREDIT_PRIMARY = {"LOAN_PAYMENTS", "LOAN_DISBURSEMENTS", "TRANSFER_IN"}

TRANSFER_MATCH_AMOUNT_TOLERANCE = 1.0
TRANSFER_MATCH_DAY_WINDOW = 3

DUPLICATE_AMOUNT_TOLERANCE = 0.01
DUPLICATE_DAY_WINDOW = 2

# --- recurring classification -------------------------------------------------

OBLIGATION_ELIGIBLE_PRIMARY = {"RENT_AND_UTILITIES"}
OBLIGATION_ELIGIBLE_DETAILED = {
    "LOAN_PAYMENTS_MORTGAGE_PAYMENT",
    "LOAN_PAYMENTS_STUDENT_LOAN_PAYMENT",
    "LOAN_PAYMENTS_CAR_PAYMENT",
    "LOAN_PAYMENTS_OTHER_PAYMENT",
    "GENERAL_SERVICES_INSURANCE",
}
# Categories that are inherently variable-amount / lifestyle spend. A merchant in
# one of these can still be a "subscription" (flat price, e.g. Youtube Premium is
# ENTERTAINMENT) — the discretionary bucket only catches ones that AREN'T flat-price,
# so periodicity alone never promotes Amazon/Uber/groceries into a bill.
DISCRETIONARY_PRIMARY = {
    "FOOD_AND_DRINK",
    "GENERAL_MERCHANDISE",
    "TRANSPORTATION",
    "TRAVEL",
    "PERSONAL_CARE",
    "ENTERTAINMENT",
}

# Repeating fees and cash withdrawals are a habit, not a bill: a flat $2.50 ATM
# fee every two weeks must never read as a subscription.
NEVER_OBLIGATION_PRIMARY = {"BANK_FEES", "TRANSFER_OUT", "TRANSFER_IN"}

RECURRING_LOOKBACK_DAYS = 180
VALID_CADENCE_MIN_DAYS = 6
VALID_CADENCE_MAX_DAYS = 60
FIXED_OBLIGATION_CONFIDENCE_THRESHOLD = 0.75
RELIABLE_INCOME_CONFIDENCE_THRESHOLD = 0.6
PROBABLE_RECURRENCE_CONFIDENCE_THRESHOLD = 0.4

OBLIGATION_TIERS = {"fixed_obligation", "subscription"}
INCOME_TIER = "reliable_income"


def _is_tier2_category(category_detailed: str) -> bool:
    return (
        category_detailed.startswith("TRANSFER_IN_") or category_detailed.startswith("TRANSFER_OUT_")
    ) and category_detailed not in TIER1_INTERNAL_CATEGORIES


def backfill_canonical_merchants(db: Session) -> None:
    """Re-normalize every transaction so rule changes in app.merchant apply to history, not just new syncs."""
    for txn in db.query(Transaction).all():
        institution_name = ""
        if txn.account and txn.account.item:
            institution_name = txn.account.item.institution_name
        canonical = normalize_merchant(txn.name, txn.merchant_name, institution_name)
        if txn.canonical_merchant != canonical:
            txn.canonical_merchant = canonical


def _clear_orphaned_matches(db: Session) -> None:
    """Undo pairings whose partner was deleted (e.g. a pending row replaced on sync).

    Otherwise the survivor stays flagged internal forever and silently drops
    out of spend/income. It's re-evaluated from scratch by the passes below.
    """
    existing_ids = {row[0] for row in db.query(Transaction.id).all()}
    orphans = (
        db.query(Transaction)
        .filter(Transaction.matched_transaction_id.isnot(None), Transaction.matched_transaction_id.notin_(existing_ids))
        .all()
    )
    for txn in orphans:
        txn.matched_transaction_id = None
        txn.is_internal_transfer = False
        txn.is_credit_card_payment = False


def reconcile_internal_transfers(db: Session) -> None:
    db.flush()
    _clear_orphaned_matches(db)

    # No is_internal_transfer filter: rows flagged before is_credit_card_payment
    # existed still need that flag set.
    tier1_txns = db.query(Transaction).filter(Transaction.category_detailed.in_(TIER1_INTERNAL_CATEGORIES)).all()
    for txn in tier1_txns:
        txn.is_internal_transfer = True
        if txn.category_detailed in TIER1_CREDIT_CARD_PAYMENT_CATEGORIES:
            txn.is_credit_card_payment = True

    _reconcile_card_payments(db)

    candidates = (
        db.query(Transaction)
        .filter(
            Transaction.category_primary.in_(["TRANSFER_IN", "TRANSFER_OUT"]),
            Transaction.is_internal_transfer.is_(False),
            Transaction.matched_transaction_id.is_(None),
        )
        .all()
    )
    outflows = [t for t in candidates if t.amount > 0 and _is_tier2_category(t.category_detailed)]
    inflows = [t for t in candidates if t.amount < 0 and _is_tier2_category(t.category_detailed)]

    for out in outflows:
        for inflow in inflows:
            if inflow.matched_transaction_id is not None:
                continue
            if inflow.account_id == out.account_id:
                continue
            if abs(abs(out.amount) - abs(inflow.amount)) > TRANSFER_MATCH_AMOUNT_TOLERANCE:
                continue
            if abs((inflow.date - out.date).days) > TRANSFER_MATCH_DAY_WINDOW:
                continue
            out.is_internal_transfer = True
            inflow.is_internal_transfer = True
            out.matched_transaction_id = inflow.id
            inflow.matched_transaction_id = out.id
            break

    _reconcile_same_account_reversals(db)
    db.flush()


def _reconcile_card_payments(db: Session) -> None:
    """Flag the card-side leg of credit card payments.

    The checking-side leg is reliably LOAN_PAYMENTS_CREDIT_CARD_PAYMENT, but
    issuers label the card-side credit inconsistently (Chase posts it as
    LOAN_DISBURSEMENTS, Bank of America as TRANSFER_IN), so it's paired with
    the checking outflow by amount and date instead of trusted by category.
    """
    card_credits = (
        db.query(Transaction)
        .join(Account, Transaction.account_id == Account.id)
        .filter(
            Account.type == "credit",
            Transaction.amount < 0,
            Transaction.is_credit_card_payment.is_(False),
            Transaction.category_primary.in_(CARD_PAYMENT_CREDIT_PRIMARY),
        )
        .all()
    )
    if not card_credits:
        return
    bank_payments = (
        db.query(Transaction)
        .join(Account, Transaction.account_id == Account.id)
        .filter(
            Account.type == "depository",
            Transaction.amount > 0,
            Transaction.category_detailed.in_(TIER1_CREDIT_CARD_PAYMENT_CATEGORIES | {"TRANSFER_OUT_ACCOUNT_TRANSFER"}),
        )
        .all()
    )
    claimed = {t.matched_transaction_id for t in card_credits if t.matched_transaction_id}
    for credit in card_credits:
        match = next(
            (
                bank
                for bank in bank_payments
                if bank.id not in claimed
                and abs(bank.amount - abs(credit.amount)) <= TRANSFER_MATCH_AMOUNT_TOLERANCE
                and abs((bank.date - credit.date).days) <= TRANSFER_MATCH_DAY_WINDOW
            ),
            None,
        )
        # Unmatched credits still count when the issuer's own category says
        # payment: they were paid from an account that isn't linked here.
        if match is None and credit.category_detailed not in TIER1_CREDIT_CARD_PAYMENT_CATEGORIES:
            continue
        credit.is_internal_transfer = True
        credit.is_credit_card_payment = True
        if match is not None:
            claimed.add(match.id)
            match.is_internal_transfer = True
            match.is_credit_card_payment = True
            credit.matched_transaction_id = match.id
            match.matched_transaction_id = credit.id


def _reconcile_same_account_reversals(db: Session) -> None:
    """Catch same-account charge+reversal washes that don't use a TRANSFER_* category.

    Some institutions post a real obligation as a charge and then immediately
    reverse it on the same account via a separate mechanism — e.g. Bilt's rent
    flow posts a +$2100 RENT_AND_UTILITIES charge to the card and a same-day
    -$2100 credit categorized INCOME to settle it. Neither leg is a transfer
    category, so this can't rely on category at all — only the fact that an
    equal-and-opposite pair on the same account nets to zero real spend.
    """
    candidates = (
        db.query(Transaction)
        .filter(Transaction.is_internal_transfer.is_(False), Transaction.matched_transaction_id.is_(None))
        .all()
    )
    by_account = defaultdict(list)
    for txn in candidates:
        by_account[txn.account_id].append(txn)

    for txns in by_account.values():
        outflows = [t for t in txns if t.amount > 0]
        inflows = [t for t in txns if t.amount < 0]
        for out in outflows:
            for inflow in inflows:
                if inflow.matched_transaction_id is not None:
                    continue
                if abs(out.amount - abs(inflow.amount)) > TRANSFER_MATCH_AMOUNT_TOLERANCE:
                    continue
                if abs((inflow.date - out.date).days) > TRANSFER_MATCH_DAY_WINDOW:
                    continue
                out.is_internal_transfer = True
                inflow.is_internal_transfer = True
                out.matched_transaction_id = inflow.id
                inflow.matched_transaction_id = out.id
                break


def detect_duplicate_groups(db: Session) -> None:
    since = date.today() - timedelta(days=RECURRING_LOOKBACK_DAYS)
    txns = (
        db.query(Transaction)
        .filter(Transaction.date >= since, Transaction.amount > 0, Transaction.is_internal_transfer.is_(False))
        .order_by(Transaction.date)
        .all()
    )

    by_merchant = defaultdict(list)
    for txn in txns:
        by_merchant[txn.canonical_merchant or "Unknown"].append(txn)

    for merchant, group in by_merchant.items():
        if merchant == "Unknown":
            continue
        sorted_group = sorted(group, key=lambda t: t.date)
        for a, b in zip(sorted_group, sorted_group[1:]):
            if abs(a.amount - b.amount) < DUPLICATE_AMOUNT_TOLERANCE and abs((b.date - a.date).days) <= DUPLICATE_DAY_WINDOW:
                group_id = a.duplicate_group_id or b.duplicate_group_id or str(a.id)
                a.duplicate_group_id = group_id
                b.duplicate_group_id = group_id

    db.flush()


def spend_by_category(db: Session, start_date: date, end_date: date) -> list[tuple[str, float]]:
    return (
        db.query(Transaction.category_primary, func.sum(Transaction.amount))
        .filter(
            Transaction.date >= start_date,
            Transaction.date <= end_date,
            Transaction.amount > 0,
            Transaction.is_internal_transfer.is_(False),
        )
        .group_by(Transaction.category_primary)
        .all()
    )


def month_spend_by_category(db: Session) -> dict[str, float]:
    today = date.today()
    return {category: total for category, total in spend_by_category(db, today.replace(day=1), today)}


def _income_filters(query):
    # Real income (paychecks, deposits) always lands in a depository account.
    # A negative amount on a credit account is a payment/refund/statement-credit
    # received, not income — restricting to depository avoids counting those.
    return (
        query.join(Account, Transaction.account_id == Account.id)
        .filter(Account.type == "depository")
        .filter(Transaction.amount < 0)
        .filter(Transaction.is_internal_transfer.is_(False))
    )


def income_total(db: Session, start_date: date, end_date: date) -> float:
    query = db.query(func.sum(Transaction.amount)).filter(
        Transaction.date >= start_date, Transaction.date <= end_date
    )
    total = _income_filters(query).scalar() or 0.0
    return -total


def invested_amount(db: Session, start_date: date, end_date: date) -> float:
    total = (
        db.query(func.sum(Transaction.amount))
        .filter(
            Transaction.date >= start_date,
            Transaction.date <= end_date,
            Transaction.category_detailed == "TRANSFER_OUT_INVESTMENT_AND_RETIREMENT_FUNDS",
        )
        .scalar()
        or 0.0
    )
    return total


def asset_breakdown(db: Session) -> dict[str, float]:
    accounts = db.query(Account).all()
    cash = sum(a.current_balance or 0.0 for a in accounts if a.type == "depository")
    investments = sum(a.current_balance or 0.0 for a in accounts if a.type == "investment")
    debt = sum(a.current_balance or 0.0 for a in accounts if a.type in LIABILITY_TYPES)
    near_term_debt = sum(a.current_balance or 0.0 for a in accounts if a.type in NEAR_TERM_LIABILITY_TYPES)
    total_assets = cash + investments
    return {
        "cash": cash,
        "investments": investments,
        "total_assets": total_assets,
        "debt": debt,
        "net_worth": total_assets - debt,
        "liquid_position": cash - near_term_debt,
    }


def interest_paid_ttm(db: Session) -> float:
    since = date.today() - timedelta(days=365)
    return (
        db.query(func.sum(Transaction.amount))
        .filter(Transaction.category_detailed == "BANK_FEES_INTEREST_CHARGE", Transaction.date >= since)
        .scalar()
        or 0.0
    )


# --- recurring / obligation classification ------------------------------------


def _confidence_components(txns: list[Transaction]) -> dict | None:
    if len(txns) < 2:
        return None
    amounts = [t.amount for t in txns]
    dates = sorted(t.date for t in txns)
    gaps = [(dates[i + 1] - dates[i]).days for i in range(len(dates) - 1)]

    mean_gap = statistics.mean(gaps) if gaps else 0.0
    interval_consistency = 0.0
    if len(gaps) >= 2 and mean_gap > 0:
        interval_consistency = max(0.0, min(1.0, 1 - statistics.pstdev(gaps) / mean_gap))

    mean_amount = statistics.mean(amounts)
    amount_consistency = 1.0
    if mean_amount:
        amount_consistency = max(0.0, min(1.0, 1 - statistics.pstdev(amounts) / abs(mean_amount)))

    count = len(txns)
    span_days = (dates[-1] - dates[0]).days
    return {
        "interval_consistency": interval_consistency,
        "amount_consistency": amount_consistency,
        "count_factor": min(count / 6, 1.0),
        "duration_factor": min(span_days / 90, 1.0),
        "mean_gap": mean_gap,
        "mean_amount": mean_amount,
        "count": count,
        "span_days": span_days,
        "last_date": dates[-1],
    }


def _is_obligation_eligible(category_primary: str, category_detailed: str) -> bool:
    return category_primary in OBLIGATION_ELIGIBLE_PRIMARY or category_detailed in OBLIGATION_ELIGIBLE_DETAILED


def _category_prior(category_primary: str, category_detailed: str) -> float:
    if _is_obligation_eligible(category_primary, category_detailed):
        return 1.0
    if category_primary in DISCRETIONARY_PRIMARY:
        return 0.2
    return 0.6


def _confidence_score(comp: dict, category_primary: str, category_detailed: str) -> float:
    return (
        0.30 * comp["interval_consistency"]
        + 0.25 * comp["amount_consistency"]
        + 0.15 * comp["count_factor"]
        + 0.20 * _category_prior(category_primary, category_detailed)
        + 0.10 * comp["duration_factor"]
    )


def _valid_cadence(comp: dict) -> bool:
    return VALID_CADENCE_MIN_DAYS <= comp["mean_gap"] <= VALID_CADENCE_MAX_DAYS


def _classify_spend_group(comp: dict, category_primary: str, category_detailed: str, confidence: float) -> str:
    has_enough_history = comp["count"] >= 3 and comp["span_days"] >= 45
    obligation_eligible = _is_obligation_eligible(category_primary, category_detailed)
    discretionary = category_primary in DISCRETIONARY_PRIMARY

    if category_primary in NEVER_OBLIGATION_PRIMARY:
        return "recurring_discretionary" if comp["count"] >= 3 else "not_recurring"

    if (
        obligation_eligible
        and has_enough_history
        and _valid_cadence(comp)
        and confidence >= FIXED_OBLIGATION_CONFIDENCE_THRESHOLD
    ):
        return "fixed_obligation"

    is_flat_subscription = (
        _valid_cadence(comp) and comp["amount_consistency"] >= 0.9 and comp["interval_consistency"] >= 0.7 and comp["count"] >= 2
    )
    if is_flat_subscription:
        return "subscription"

    if discretionary and comp["count"] >= 3:
        return "recurring_discretionary"

    if confidence >= PROBABLE_RECURRENCE_CONFIDENCE_THRESHOLD or (comp["count"] >= 2 and _valid_cadence(comp)):
        return "probable_recurrence"

    return "not_recurring"


def _classify_income_group(comp: dict, confidence: float) -> str:
    has_enough_history = comp["count"] >= 3 and comp["span_days"] >= 45
    if _valid_cadence(comp) and has_enough_history and confidence >= RELIABLE_INCOME_CONFIDENCE_THRESHOLD:
        return "reliable_income"
    if comp["count"] >= 2:
        return "probable_income"
    return "not_recurring"


def analyze_recurring(db: Session, direction: str = "spend", lookback_days: int = RECURRING_LOOKBACK_DAYS) -> list[dict]:
    since = date.today() - timedelta(days=lookback_days)
    query = db.query(Transaction).filter(Transaction.date >= since, Transaction.is_internal_transfer.is_(False))
    if direction == "spend":
        query = query.filter(Transaction.amount > 0)
    else:
        # Real income only ever lands in a depository account (see _income_filters).
        query = _income_filters(query)
    txns = query.all()

    groups = defaultdict(list)
    for txn in txns:
        groups[txn.canonical_merchant or "Unknown"].append(txn)

    overrides = {o.canonical_merchant: o.user_classification for o in db.query(RecurringOverride).all()}

    results = []
    for merchant, group in groups.items():
        if merchant == "Unknown":
            continue
        comp = _confidence_components(group)
        if comp is None:
            continue

        cash_count = sum(1 for t in group if t.account is not None and t.account.type == "depository")
        paid_from = "cash" if cash_count * 2 >= len(group) else "card"

        category_counts = Counter((t.category_primary, t.category_detailed) for t in group)
        dominant_primary, dominant_detailed = category_counts.most_common(1)[0][0]
        confidence = _confidence_score(comp, dominant_primary, dominant_detailed)

        if direction == "spend":
            heuristic_tier = _classify_spend_group(comp, dominant_primary, dominant_detailed, confidence)
        else:
            heuristic_tier = _classify_income_group(comp, confidence)

        override = overrides.get(merchant)
        next_expected_date = (
            comp["last_date"] + timedelta(days=round(comp["mean_gap"])) if comp["mean_gap"] else None
        )

        results.append(
            {
                "canonical_merchant": merchant,
                "tier": override or heuristic_tier,
                "heuristic_tier": heuristic_tier,
                "overridden": override is not None,
                "confidence": round(confidence, 3),
                "average_amount": abs(comp["mean_amount"]),
                "cadence_days": round(comp["mean_gap"]) if comp["mean_gap"] else None,
                "transaction_count": comp["count"],
                "last_date": comp["last_date"],
                "next_expected_date": next_expected_date,
                "category_primary": dominant_primary,
                "paid_from": paid_from,
            }
        )

    return sorted(results, key=lambda r: -r["average_amount"])


def set_recurring_override(db: Session, canonical_merchant: str, classification: str) -> None:
    record = db.query(RecurringOverride).filter_by(canonical_merchant=canonical_merchant).one_or_none()
    if record is None:
        record = RecurringOverride(canonical_merchant=canonical_merchant, user_classification=classification)
        db.add(record)
    else:
        record.user_classification = classification


def clear_recurring_override(db: Session, canonical_merchant: str) -> bool:
    record = db.query(RecurringOverride).filter_by(canonical_merchant=canonical_merchant).one_or_none()
    if record is None:
        return False
    db.delete(record)
    return True


def _project_occurrences(item: dict, horizon_days: int, today: date):
    occ_date = item["next_expected_date"]
    cadence = item["cadence_days"]
    if not occ_date or not cadence:
        return
    while occ_date and (occ_date - today).days <= horizon_days:
        days_out = (occ_date - today).days
        if days_out >= 0:
            yield occ_date
        occ_date = occ_date + timedelta(days=cadence)


def confirmed_obligations(db: Session) -> list[dict]:
    return [r for r in analyze_recurring(db, "spend") if r["tier"] in OBLIGATION_TIERS]


def expected_income(db: Session) -> list[dict]:
    return [r for r in analyze_recurring(db, "income") if r["tier"] == INCOME_TIER]


# --- flagged transactions ------------------------------------------------------


def detect_flagged_transactions(db: Session) -> list[dict]:
    since = date.today() - timedelta(days=RECURRING_LOOKBACK_DAYS)
    txns = (
        db.query(Transaction)
        .filter(Transaction.date >= since, Transaction.amount > 0, Transaction.is_internal_transfer.is_(False))
        .order_by(Transaction.date)
        .all()
    )

    by_merchant = defaultdict(list)
    for txn in txns:
        by_merchant[txn.canonical_merchant or "Unknown"].append(txn)

    flags = []
    for merchant, group in by_merchant.items():
        if merchant == "Unknown" or len(group) < 4:
            continue
        amounts = [t.amount for t in group]
        mean = statistics.mean(amounts)
        stdev = statistics.pstdev(amounts)
        for txn in group:
            if stdev == 0:
                if txn.amount != mean:
                    flags.append(
                        {
                            "transaction_id": txn.transaction_id,
                            "date": txn.date,
                            "name": txn.canonical_merchant or txn.name,
                            "amount": txn.amount,
                            "reason": f"price changed for {merchant} (was always ${mean:,.2f})",
                        }
                    )
            else:
                z_score = (txn.amount - mean) / stdev
                if z_score > 2.5 and (txn.amount - mean) >= max(10.0, mean * 0.15):
                    flags.append(
                        {
                            "transaction_id": txn.transaction_id,
                            "date": txn.date,
                            "name": txn.canonical_merchant or txn.name,
                            "amount": txn.amount,
                            "reason": f"unusually high charge for {merchant} (typically ~${mean:,.2f})",
                        }
                    )

    duplicate_groups = defaultdict(list)
    for txn in txns:
        if txn.duplicate_group_id:
            duplicate_groups[txn.duplicate_group_id].append(txn)
    for group_id, group in duplicate_groups.items():
        latest = max(group, key=lambda t: t.date)
        flags.append(
            {
                "transaction_id": latest.transaction_id,
                "date": latest.date,
                "name": latest.canonical_merchant or latest.name,
                "amount": latest.amount,
                "reason": f"possible duplicate charge ({latest.canonical_merchant}, ${latest.amount:,.2f} within {DUPLICATE_DAY_WINDOW} days)",
            }
        )

    fee_txns = (
        db.query(Transaction)
        .filter(Transaction.category_detailed == "BANK_FEES_ATM_FEES", Transaction.date >= since)
        .all()
    )
    if len(fee_txns) >= 3:
        total = sum(t.amount for t in fee_txns)
        flags.append(
            {
                "transaction_id": None,
                "date": max(t.date for t in fee_txns),
                "name": "ATM fees",
                "amount": total,
                "reason": (
                    f"{len(fee_txns)} ATM fees totaling ${total:,.2f} in the last 6 months"
                    " — avoidable with in-network ATMs"
                ),
            }
        )

    return sorted(flags, key=lambda f: f["date"], reverse=True)


# --- critical alerts / liquidity breakdown -------------------------------------


def compute_liquidity_breakdown(db: Session, days: int = 14) -> dict:
    # The minimum has to come from walking the days in order: income landing
    # after a bill can't cover it, so cash - bills + income overstates it.
    forecast = forecast_cash_flow(db, days=days)
    cash = asset_breakdown(db)["cash"]
    obligations_total = forecast["total_expected_bills"]
    income_total_projected = forecast["total_expected_income"]
    projected_minimum = forecast["lowest_balance"]
    card_payments_total = forecast["total_card_payments"]
    everyday_total = forecast["total_everyday_spending"]

    severity = None
    if projected_minimum < 0:
        severity = "critical"
    elif projected_minimum < 500:
        severity = "warning"

    return {
        "available_cash": cash,
        "confirmed_obligations_14d": obligations_total,
        "card_payments_14d": card_payments_total,
        "everyday_spending_14d": everyday_total,
        "expected_income_14d": income_total_projected,
        "projected_minimum_cash": projected_minimum,
        "severity": severity,
    }


def compute_critical_alerts(db: Session) -> list[dict]:
    alerts = []
    today = date.today()

    for account in db.query(Account).filter(Account.type == "credit").all():
        if account.current_balance is None or account.available_balance is None:
            continue
        limit = account.current_balance + account.available_balance
        if limit <= 0:
            continue
        utilization = account.current_balance / limit
        if utilization >= 0.9:
            alerts.append({"severity": "critical", "message": f"{account.name} is at {utilization:.0%} credit utilization"})
        elif utilization >= 0.7:
            alerts.append({"severity": "warning", "message": f"{account.name} is at {utilization:.0%} credit utilization"})

    for liability in db.query(CardLiability).all():
        account = db.get(Account, liability.account_id)
        name = account.name if account else "Card"
        if liability.is_overdue:
            alerts.append({"severity": "critical", "message": f"{name} payment is overdue"})
        elif liability.next_payment_due_date is not None:
            days_until = (liability.next_payment_due_date - today).days
            if 0 <= days_until <= 7:
                alerts.append(
                    {
                        "severity": "warning",
                        "message": f"{name} payment of ${liability.minimum_payment_amount or 0:,.2f} due {liability.next_payment_due_date.isoformat()}",
                    }
                )

    interest = interest_paid_ttm(db)
    if interest > 0:
        alerts.append({"severity": "warning", "message": f"${interest:,.2f} in interest charged over the last 12 months"})

    severity_rank = {"critical": 0, "warning": 1, "info": 2}
    return sorted(alerts, key=lambda a: severity_rank.get(a["severity"], 3))


# --- 30-day cash-flow forecast --------------------------------------------------


# Statement cycles close roughly this long before the payment due date; card
# payments posted after that point count toward the statement being forecast.
STATEMENT_CLOSE_BEFORE_DUE_DAYS = 25
EVERYDAY_SPEND_LOOKBACK_DAYS = 90


def _card_payment_history(db: Session, account_id: int) -> list[Transaction]:
    return (
        db.query(Transaction)
        .filter(Transaction.account_id == account_id, Transaction.is_credit_card_payment.is_(True), Transaction.amount < 0)
        .order_by(Transaction.date)
        .all()
    )


def card_display_name(account: Account) -> str:
    """"Chase card ··9018" rather than issuer names like "CREDIT CARD"."""
    institution = account.item.institution_name if account.item else ""
    base = f"{institution} card" if institution else account.name
    return f"{base} ··{account.mask}" if account.mask else base


def statement_remaining(liability: CardLiability, payment_history: list[Transaction], balance: float) -> float:
    """Statement balance still owed: the statement minus payments since it closed."""
    statement_close = liability.next_payment_due_date - timedelta(days=STATEMENT_CLOSE_BEFORE_DUE_DAYS)
    paid_since_close = sum(-t.amount for t in payment_history if t.date > statement_close)
    return min(balance, max(0.0, (liability.last_statement_balance or 0.0) - paid_since_close))


def projected_card_payments(db: Session, days: int, today: date | None = None) -> list[dict]:
    """Card payments expected to leave checking within the horizon.

    Uses the issuer's statement balance and due date when Plaid Liabilities
    provides them, net of payments already made since the statement closed.
    Cards without liability data are assumed paid in full on their usual
    payment rhythm.
    """
    today = today or date.today()
    liabilities = {l.account_id: l for l in db.query(CardLiability).all()}
    events = []
    for account in db.query(Account).filter(Account.type == "credit").all():
        balance = account.current_balance or 0.0
        if balance <= 0:
            continue
        history = _card_payment_history(db, account.id)
        liability = liabilities.get(account.id)

        if liability and liability.next_payment_due_date and liability.last_statement_balance is not None:
            due = liability.next_payment_due_date
            amount = statement_remaining(liability, history, balance)
            source = "statement"
        else:
            if len(history) >= 2:
                gaps = [(b.date - a.date).days for a, b in zip(history, history[1:])]
                due = history[-1].date + timedelta(days=round(statistics.median(gaps)))
            elif history:
                due = history[-1].date + timedelta(days=30)
            else:
                due = today + timedelta(days=STATEMENT_CLOSE_BEFORE_DUE_DAYS)
            due = max(due, today)
            amount = balance
            source = "balance"

        if amount > 0 and 0 <= (due - today).days <= days:
            events.append({"date": due, "label": f"{card_display_name(account)} payment", "amount": round(amount, 2), "source": source})
    return events


def everyday_checking_spend_rate(db: Session, exclude_merchants: set[str], today: date | None = None) -> float:
    """Average daily spending straight out of checking that isn't a known bill.

    Debit purchases, ATM withdrawals, Zelle and similar all drain checking
    without any bill to predict them, so they're modeled as a daily run rate.
    Card purchases are excluded: they reach checking via the card payment.
    """
    today = today or date.today()
    first = db.query(func.min(Transaction.date)).scalar()
    if first is None:
        return 0.0
    since = max(first, today - timedelta(days=EVERYDAY_SPEND_LOOKBACK_DAYS))
    window_days = max((today - since).days, 1)
    rows = (
        db.query(Transaction.canonical_merchant, Transaction.amount)
        .join(Account, Transaction.account_id == Account.id)
        .filter(
            Account.type == "depository",
            Transaction.amount > 0,
            Transaction.date >= since,
            Transaction.date <= today,
            Transaction.is_internal_transfer.is_(False),
            Transaction.is_credit_card_payment.is_(False),
        )
        .all()
    )
    total = sum(amount for merchant, amount in rows if merchant not in exclude_merchants)
    return total / window_days


def forecast_cash_flow(db: Session, days: int = 30, safety_floor: float = 500.0) -> dict:
    today = date.today()
    cash = asset_breakdown(db)["cash"]

    events: list[dict] = []
    bills = confirmed_obligations(db)
    # Bills charged to a card don't touch checking until the card is paid, and
    # that payment is already counted below; only cash-paid bills apply directly.
    for item in bills:
        if item.get("paid_from") == "card":
            continue
        for occ_date in _project_occurrences(item, days, today):
            events.append({"date": occ_date, "label": item["canonical_merchant"], "amount": -item["average_amount"], "kind": "bill"})
    for item in expected_income(db):
        for occ_date in _project_occurrences(item, days, today):
            events.append({"date": occ_date, "label": item["canonical_merchant"], "amount": item["average_amount"], "kind": "income"})
    for payment in projected_card_payments(db, days, today):
        events.append({"date": payment["date"], "label": payment["label"], "amount": -payment["amount"], "kind": "card_payment"})

    cash_bill_merchants = {b["canonical_merchant"] for b in bills if b.get("paid_from") != "card"}
    daily_everyday = everyday_checking_spend_rate(db, cash_bill_merchants, today)

    daily_delta: dict[date, float] = defaultdict(float)
    for event in events:
        daily_delta[event["date"]] += event["amount"]

    # Items expected today haven't posted yet (they'd have moved next_expected_date
    # forward), so apply them to day 0 just like the totals below count them.
    running = cash + daily_delta.get(today, 0.0)
    series = [{"date": today, "projected_balance": running}]
    lowest_balance = running
    lowest_date = today
    for i in range(1, days + 1):
        d = today + timedelta(days=i)
        running += daily_delta.get(d, 0.0) - daily_everyday
        series.append({"date": d, "projected_balance": running})
        if running < lowest_balance:
            lowest_balance = running
            lowest_date = d

    def total(kind: str) -> float:
        return sum(abs(e["amount"]) for e in events if e["kind"] == kind)

    return {
        "series": series,
        "events": sorted(events, key=lambda e: e["date"]),
        "lowest_balance": lowest_balance,
        "lowest_balance_date": lowest_date,
        "total_expected_income": total("income"),
        "total_expected_bills": total("bill"),
        "total_card_payments": total("card_payment"),
        "total_everyday_spending": daily_everyday * days,
        "everyday_daily_rate": daily_everyday,
        "discretionary_buffer": max(0.0, lowest_balance),
        "excess_liquidity": max(0.0, lowest_balance - safety_floor),
        "safety_floor": safety_floor,
    }


# --- daily spending -------------------------------------------------------------


def daily_spending(
    db: Session,
    account: Account | None = None,
    start: date | None = None,
    end: date | None = None,
    include_bills: bool = False,
) -> dict:
    """Spending per day, split by category, for the daily spending charts.

    Fixed bills (rent, insurance, phone) are left out by default: one $2,100
    rent day would flatten every other day in the chart, and they aren't
    day-to-day choices. The amount left out is reported so nothing is hidden.
    """
    today = date.today()
    end = min(end or today, today)
    start = start or end - timedelta(days=29)
    query = db.query(Transaction).filter(
        Transaction.date >= start,
        Transaction.date <= end,
        Transaction.amount > 0,
        Transaction.is_internal_transfer.is_(False),
    )
    if account is not None:
        query = query.filter(Transaction.account_id == account.id)
    txns = query.all()

    bill_merchants = {r["canonical_merchant"] for r in analyze_recurring(db, "spend") if r["tier"] == "fixed_obligation"}
    by_day: dict[date, dict[str, float]] = {start + timedelta(days=i): defaultdict(float) for i in range((end - start).days + 1)}
    counts: Counter = Counter()
    excluded = 0.0
    for t in txns:
        if not include_bills and t.canonical_merchant in bill_merchants:
            excluded += t.amount
            continue
        by_day[t.date][t.category_primary] += t.amount
        counts[t.date] += 1

    days = [
        {
            "date": d,
            "total": round(sum(cats.values()), 2),
            "count": counts[d],
            "categories": {k: round(v, 2) for k, v in sorted(cats.items(), key=lambda kv: -kv[1])},
        }
        for d, cats in sorted(by_day.items())
    ]
    category_totals: dict[str, float] = defaultdict(float)
    for day in days:
        for k, v in day["categories"].items():
            category_totals[k] += v
    total = sum(d["total"] for d in days)
    highest = max(days, key=lambda d: d["total"], default=None)
    return {
        "start_date": start,
        "end_date": end,
        "days": days,
        "category_totals": [{"name": k, "amount": round(v, 2)} for k, v in sorted(category_totals.items(), key=lambda kv: -kv[1])],
        "total": round(total, 2),
        "average_per_day": round(total / len(days), 2) if days else 0.0,
        "highest_day": {"date": highest["date"], "total": highest["total"]} if highest and highest["total"] > 0 else None,
        "no_spend_days": sum(1 for d in days if d["total"] == 0),
        "bills_excluded": round(excluded, 2),
        "include_bills": include_bills,
    }


# --- investments ----------------------------------------------------------------

CONTRIBUTION_SUBTYPES = {"deposit", "contribution", "transfer"}
WITHDRAWAL_SUBTYPES = {"withdrawal", "distribution"}
INCOME_SUBTYPES = {"dividend", "qualified dividend", "non-qualified dividend", "interest", "long-term capital gain", "short-term capital gain"}
SECURITY_TYPES = {"buy", "sell", "transfer"}


def xirr(flows: list[tuple[date, float]]) -> float | None:
    """Annualized money-weighted return. Flows: money in negative, value out positive."""
    if len(flows) < 2 or not any(v > 0 for _, v in flows) or not any(v < 0 for _, v in flows):
        return None
    t0 = min(d for d, _ in flows)

    def npv(rate: float) -> float:
        return sum(v / (1 + rate) ** ((d - t0).days / 365.0) for d, v in flows)

    lo, hi = -0.99, 10.0
    if npv(lo) * npv(hi) > 0:
        return None
    for _ in range(200):  # bisection: robust where Newton can diverge
        mid = (lo + hi) / 2
        if npv(lo) * npv(mid) <= 0:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


EMPLOYER_CONTRIBUTION_MARKERS = ("CO CONTR", "EMPLOYER", "ER CONTR", "COMPANY MATCH", "MATCH")


def is_employer_contribution(t: "InvestmentTransaction") -> bool:
    name = (t.name or "").upper()
    return any(marker in name for marker in EMPLOYER_CONTRIBUTION_MARKERS)


def adds_shares(t: "InvestmentTransaction") -> bool:
    """Trades and in-kind contributions change a position's share count."""
    kind, subtype = (t.type or "").lower(), (t.subtype or "").lower()
    return bool(t.security_id is not None and t.quantity and (kind in SECURITY_TYPES or subtype == "contribution"))


def _classify_investment_txn(t: "InvestmentTransaction") -> str:
    subtype = (t.subtype or "").lower()
    kind = (t.type or "").lower()
    if kind == "fee":
        return "fee"
    # Contributions can land as cash (HSA deposits) or straight into a fund
    # (401k payroll contributions carry the fund's security and share count).
    if kind in {"cash", "transfer"} and (t.security_id is None or subtype in {"contribution", "deposit"}):
        if subtype in WITHDRAWAL_SUBTYPES or (subtype in CONTRIBUTION_SUBTYPES and t.amount > 0):
            return "withdrawal"
        if subtype in CONTRIBUTION_SUBTYPES:
            return "contribution"
    if subtype in INCOME_SUBTYPES:
        return "income"
    if kind == "buy":
        return "buy"
    if kind == "sell":
        return "sell"
    return "other"


PRICE_HISTORY_DAYS = 730


def priceable_holdings(db: Session, account_ids: list[int] | None = None) -> list[tuple[Holding, Security]]:
    query = db.query(Holding, Security).join(Security, Holding.security_id == Security.id)
    if account_ids is not None:
        query = query.filter(Holding.account_id.in_(account_ids))
    return [
        (h, sec)
        for h, sec in query.all()
        if sec.ticker_symbol and (sec.type or "").lower() not in market_data.UNPRICEABLE_TYPES
    ]


def portfolio_value_history(db: Session, account_ids: list[int], start: date, end: date | None = None) -> dict:
    """Daily value of investment accounts from share counts x market closes.

    Share counts are rolled back through the brokerage's trades, so with a
    complete trade history this is the account's real value each day. Without
    trades it prices today's positions historically (as if held throughout),
    which the caller labels. Positions with no market price (options, cash)
    are held at today's value.
    """
    end = end or date.today()
    rows = db.query(Holding, Security).join(Security, Holding.security_id == Security.id).filter(Holding.account_id.in_(account_ids)).all()
    if not rows:
        return {"series": {}, "priced_share": 0.0, "unpriced": []}
    accounts = db.query(Account).filter(Account.id.in_(account_ids)).all()
    holdings_value = sum(h.institution_value or 0.0 for h, _ in rows)
    cash_now = sum(a.current_balance or 0.0 for a in accounts) - holdings_value

    trades = (
        db.query(InvestmentTransaction)
        .filter(InvestmentTransaction.account_id.in_(account_ids), InvestmentTransaction.date > start)
        .all()
    )
    later_qty: dict[int, dict[date, float]] = defaultdict(lambda: defaultdict(float))
    later_cash: dict[date, float] = defaultdict(float)
    for t in trades:
        if adds_shares(t):
            later_qty[t.security_id][t.date] += t.quantity
        later_cash[t.date] += t.amount  # Plaid: positive = cash left the account

    priced, unpriced_value, unpriced = [], 0.0, []
    for h, sec in rows:
        prices = (
            market_data.price_series(db, sec.ticker_symbol, start, end)
            if sec.ticker_symbol and (sec.type or "").lower() not in market_data.UNPRICEABLE_TYPES
            else {}
        )
        if prices:
            priced.append((h, sec, prices))
        else:
            unpriced_value += h.institution_value or 0.0
            unpriced.append(sec.ticker_symbol or sec.name or "position")

    series: dict[date, float] = {}
    day = end
    qty = {h.security_id: h.quantity or 0.0 for h, _, _ in priced}
    cash = cash_now
    while day >= start:
        value = cash + unpriced_value
        for h, sec, prices in priced:
            if day in prices:
                value += qty[h.security_id] * prices[day]
        series[day] = value
        # Step to the previous day: undo this day's trades.
        for h, _, _ in priced:
            qty[h.security_id] -= later_qty[h.security_id].get(day, 0.0)
        cash += later_cash.get(day, 0.0)
        day -= timedelta(days=1)
    priced_value = sum(h.institution_value or 0.0 for h, _, _ in priced)
    return {
        "series": dict(sorted(series.items())),
        "priced_share": priced_value / holdings_value if holdings_value else 0.0,
        "unpriced": unpriced,
    }


def _price_return(db: Session, ticker: str, days: int, today: date) -> float | None:
    prices = market_data.price_series(db, ticker, today - timedelta(days=days), today)
    if not prices:
        return None
    first = prices.get(today - timedelta(days=days))
    last = prices.get(today) or prices[max(prices)]
    return (last / first - 1) if first else None


def investment_performance(db: Session) -> dict:
    """What went into your investment accounts and what it has grown to."""
    accounts = db.query(Account).filter(Account.type == "investment").all()
    ids = [a.id for a in accounts]
    items = {a.item_id: a.item for a in accounts if a.item}
    value = sum(a.current_balance or 0.0 for a in accounts)
    today = date.today()

    holdings_rows = (
        db.query(Holding, Security, Account)
        .join(Security, Holding.security_id == Security.id)
        .join(Account, Holding.account_id == Account.id)
        .filter(Holding.account_id.in_(ids))
        .all()
        if ids
        else []
    )
    holdings = []
    for h, sec, acct in holdings_rows:
        v = h.institution_value or 0.0
        gain = v - h.cost_basis if h.cost_basis is not None else None
        holdings.append(
            {
                "ticker": sec.ticker_symbol or (sec.type or "").title() or "Position",
                "name": sec.name,
                "type": sec.type,
                "account_name": acct.name,
                "quantity": h.quantity,
                "price": h.institution_price,
                "value": round(v, 2),
                "cost_basis": h.cost_basis,
                "gain": round(gain, 2) if gain is not None else None,
                "gain_pct": round(gain / h.cost_basis, 4) if gain is not None and h.cost_basis else None,
                "weight": round(v / value, 4) if value else None,
            }
        )
    for (h, sec, _), out in zip(holdings_rows, holdings):
        priceable = sec.ticker_symbol and (sec.type or "").lower() not in market_data.UNPRICEABLE_TYPES
        out["return_1m"] = _price_return(db, sec.ticker_symbol, 30, today) if priceable else None
        out["return_1y"] = _price_return(db, sec.ticker_symbol, 365, today) if priceable else None
    holdings.sort(key=lambda h: -h["value"])
    with_basis = [h for h in holdings if h["cost_basis"] is not None]
    cost_basis_total = sum(h["cost_basis"] for h in with_basis)
    unrealized = sum(h["gain"] for h in with_basis) if with_basis else None

    txns = (
        db.query(InvestmentTransaction).filter(InvestmentTransaction.account_id.in_(ids)).order_by(InvestmentTransaction.date).all()
        if ids
        else []
    )
    flows: list[tuple[date, float]] = []  # + contributed, - withdrawn
    dividends = fees = 0.0
    buys = sells = 0
    quantity_change: dict[int, float] = defaultdict(float)
    for t in txns:
        kind = _classify_investment_txn(t)
        if kind == "contribution":
            flows.append((t.date, abs(t.amount)))
        elif kind == "withdrawal":
            flows.append((t.date, -abs(t.amount)))
        elif kind == "income":
            dividends += abs(t.amount)
        elif kind == "fee":
            fees += abs(t.amount)
        elif kind == "buy":
            buys += 1
        elif kind == "sell":
            sells += 1
        if adds_shares(t):
            quantity_change[t.security_id] += t.quantity

    # Bank-side transfers into investing cover brokerages that don't report
    # their deposits (Webull shares balance and holdings but not cash moves).
    # A transfer that matches a brokerage-reported deposit is the same money.
    reported = list(flows)
    bank_added = 0
    for day, amount in (
        db.query(Transaction.date, Transaction.amount)
        .filter(Transaction.category_detailed == INVESTMENT_CONTRIBUTION_CATEGORY, Transaction.pending.is_(False))
        .order_by(Transaction.date)
    ):
        if any(abs(v - amount) <= 1.0 and abs((d - day).days) <= 5 for d, v in reported):
            continue
        flows.append((day, amount))
        bank_added += 1
    flows.sort(key=lambda f: f[0])
    source = "brokerage" if reported and not bank_added else ("bank_transfers" if bank_added and not reported else "mixed")

    net_contributed = sum(v for _, v in flows)

    # Complete history = every current position is explained by buys inside the
    # window (quantities roll back to zero). Only then is value - contributions
    # the whole gain and the annualized return meaningful.
    full_history = False
    if txns and holdings_rows:
        full_history = all(abs((h.quantity or 0.0) - quantity_change.get(h.security_id, 0.0)) < 1e-6 for h, _, _ in holdings_rows)
    annualized = xirr([(d, -v) for d, v in flows] + [(today, value)]) if full_history else None

    cumulative, running = [], 0.0
    for d, v in flows:
        running += v
        cumulative.append({"date": d, "net_contributed": round(running, 2)})

    snaps = (
        db.query(BalanceSnapshot.recorded_at, func.sum(BalanceSnapshot.balance))
        .filter(BalanceSnapshot.account_id.in_(ids))
        .group_by(BalanceSnapshot.recorded_at)
        .order_by(BalanceSnapshot.recorded_at)
        .all()
        if ids
        else []
    )

    priced = portfolio_value_history(db, ids, today - timedelta(days=PRICE_HISTORY_DAYS), today) if ids else {"series": {}, "priced_share": 0.0, "unpriced": []}
    priced_points = [{"date": d, "value": round(v, 2)} for d, v in priced["series"].items()]
    priced_returns = {}
    for label, days in (("1m", 30), ("3m", 91), ("6m", 182), ("1y", 365), ("2y", 730)):
        past = priced["series"].get(today - timedelta(days=days))
        now_v = priced["series"].get(today)
        if past and now_v:
            priced_returns[label] = round(now_v / past - 1, 4)

    statuses = {i.investments_status for i in items.values()}
    access = "consent_required" if "consent_required" in statuses else ("ok" if "ok" in statuses else (statuses.pop() if statuses else None))
    return {
        "access": access,
        "items_needing_consent": [{"item_id": i.item_id, "institution_name": i.institution_name} for i in items.values() if i.investments_status == "consent_required"],
        "accounts": [{"name": a.name, "institution_name": a.item.institution_name if a.item else "", "balance": a.current_balance} for a in accounts],
        "value": round(value, 2),
        "holdings": holdings,
        "cost_basis_total": round(cost_basis_total, 2) if with_basis else None,
        "unrealized_gain": round(unrealized, 2) if unrealized is not None else None,
        "net_contributed": round(net_contributed, 2),
        "contributions_source": source,
        "contributions": cumulative,
        "history_start": flows[0][0] if flows else (txns[0].date if txns else None),
        "full_history": full_history,
        "total_gain": round(value - net_contributed, 2) if full_history else None,
        "annualized_return": round(annualized, 4) if annualized is not None else None,
        "dividends": round(dividends, 2),
        "fees": round(fees, 2),
        "trade_counts": {"buys": buys, "sells": sells},
        "value_history": [{"date": d, "value": round(v, 2)} for d, v in snaps],
        "priced_history": priced_points,
        # "trades": share counts follow the brokerage's trade history, so the
        # line is the real account value; "current_positions": today's shares
        # priced back in time (as if held throughout).
        "priced_history_mode": "trades" if full_history else "current_positions",
        "priced_share": round(priced["priced_share"], 4),
        "unpriced": priced["unpriced"],
        "priced_returns": priced_returns,
        "price_source": _latest_price_source(db),
    }


def _latest_price_source(db: Session) -> str | None:
    row = db.query(SecurityPrice.source).order_by(SecurityPrice.date.desc()).first()
    return row[0] if row else None



# --- per-account summary (Transactions page) ------------------------------------

ACCOUNT_SUMMARY_DEFAULT_DAYS = 30
INTEREST_CATEGORY = "BANK_FEES_INTEREST_CHARGE"


def _window_spend(db: Session, account_ids: list[int], start: date, end: date) -> list[Transaction]:
    return (
        db.query(Transaction)
        .filter(
            Transaction.account_id.in_(account_ids),
            Transaction.date >= start,
            Transaction.date <= end,
            Transaction.amount > 0,
            Transaction.is_internal_transfer.is_(False),
        )
        .all()
    )


def _window_inflows(db: Session, account_ids: list[int], start: date, end: date) -> list[Transaction]:
    return (
        db.query(Transaction)
        .filter(
            Transaction.account_id.in_(account_ids),
            Transaction.date >= start,
            Transaction.date <= end,
            Transaction.amount < 0,
            Transaction.is_internal_transfer.is_(False),
        )
        .all()
    )


def _top(rows: list[Transaction], key, limit: int = 5) -> list[dict]:
    totals: dict[str, float] = defaultdict(float)
    counts: Counter = Counter()
    for t in rows:
        k = key(t)
        totals[k] += t.amount
        counts[k] += 1
    grand = sum(totals.values()) or 1.0
    ranked = sorted(totals.items(), key=lambda kv: -kv[1])[:limit]
    return [{"name": k, "amount": round(v, 2), "count": counts[k], "share": round(v / grand, 4)} for k, v in ranked]


def account_summary(db: Session, account: Account | None, start: date | None = None, end: date | None = None) -> dict:
    """Figures for the Transactions page header: one account, or all of them."""
    today = date.today()
    end = min(end or today, today)
    start = start or end - timedelta(days=ACCOUNT_SUMMARY_DEFAULT_DAYS - 1)
    window_days = (end - start).days + 1
    prev_end = start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=window_days - 1)

    accounts = [account] if account else db.query(Account).all()
    ids = [a.id for a in accounts]

    spend = _window_spend(db, ids, start, end)
    inflows = _window_inflows(db, ids, start, end)
    prev_spend_total = sum(t.amount for t in _window_spend(db, ids, prev_start, prev_end))
    spend_total = sum(t.amount for t in spend)
    largest = max(spend, key=lambda t: t.amount, default=None)

    year_ago = today - timedelta(days=365)
    fees = (
        db.query(Transaction)
        .filter(Transaction.account_id.in_(ids), Transaction.category_primary == "BANK_FEES", Transaction.date >= year_ago, Transaction.amount > 0)
        .all()
    )
    interest_ttm = sum(t.amount for t in fees if t.category_detailed == INTEREST_CATEGORY)
    interest_window = sum(t.amount for t in fees if t.category_detailed == INTEREST_CATEGORY and start <= t.date <= end)
    fees_ttm = sum(t.amount for t in fees if t.category_detailed != INTEREST_CATEGORY)

    # Bills and subscriptions whose charges land on these accounts.
    recurring = []
    bill_items = [r for r in analyze_recurring(db, "spend") if r["tier"] in OBLIGATION_TIERS]
    if bill_items:
        since = today - timedelta(days=RECURRING_LOOKBACK_DAYS)
        on_accounts = {
            m
            for (m,) in db.query(Transaction.canonical_merchant)
            .filter(
                Transaction.account_id.in_(ids),
                Transaction.date >= since,
                Transaction.amount > 0,
                Transaction.is_internal_transfer.is_(False),
            )
            .distinct()
        }
        recurring = [
            {"name": r["canonical_merchant"], "amount": round(r["average_amount"], 2), "next_date": r["next_expected_date"], "tier": r["tier"]}
            for r in bill_items
            if r["canonical_merchant"] in on_accounts
        ]

    summary = {
        "scope": "account" if account else "all",
        "account_type": account.type if account else None,
        "account_name": card_display_name(account) if account and account.type == "credit" else (account.name if account else "All accounts"),
        "institution_name": account.item.institution_name if account and account.item else None,
        "start_date": start,
        "end_date": end,
        "window_days": window_days,
        "spend_total": round(spend_total, 2),
        "spend_prev_total": round(prev_spend_total, 2),
        "inflow_total": round(-sum(t.amount for t in inflows), 2),
        "transaction_count": len(spend) + len(inflows),
        "average_daily_spend": round(spend_total / window_days, 2),
        "largest_purchase": (
            {"name": largest.canonical_merchant or largest.name, "amount": largest.amount, "date": largest.date} if largest else None
        ),
        "top_categories": _top(spend, lambda t: t.category_primary),
        "top_merchants": _top(spend, lambda t: t.canonical_merchant or t.name),
        "interest_ttm": round(interest_ttm, 2),
        "interest_window": round(interest_window, 2),
        "fees_ttm": round(fees_ttm, 2),
        "recurring": sorted(recurring, key=lambda r: -r["amount"]),
        "balance": round(sum(a.current_balance or 0.0 for a in accounts), 2) if account else None,
        "available": account.available_balance if account else None,
        "credit": None,
        "cash": None,
        "investment": None,
    }

    if account is None:
        return summary

    if account.type == "credit":
        balance = account.current_balance or 0.0
        limit = balance + account.available_balance if account.available_balance is not None else None
        liability = db.query(CardLiability).filter_by(account_id=account.id).one_or_none()
        history = _card_payment_history(db, account.id)
        last_txn_payment = history[-1] if history else None
        due = liability.next_payment_due_date if liability else None
        summary["credit"] = {
            "owed": round(balance, 2),
            "credit_limit": round(limit, 2) if limit else None,
            "available_credit": account.available_balance,
            "utilization": round(balance / limit, 4) if limit else None,
            "statement_balance": liability.last_statement_balance if liability else None,
            "statement_remaining": (
                round(statement_remaining(liability, history, balance), 2)
                if liability and liability.next_payment_due_date and liability.last_statement_balance is not None
                else None
            ),
            "minimum_payment": liability.minimum_payment_amount if liability else None,
            "due_date": due,
            "days_until_due": (due - today).days if due else None,
            "is_overdue": bool(liability.is_overdue) if liability else False,
            "apr": liability.apr_purchase if liability else None,
            "last_payment_amount": (liability.last_payment_amount if liability and liability.last_payment_amount else (-last_txn_payment.amount if last_txn_payment else None)),
            "last_payment_date": (liability.last_payment_date if liability and liability.last_payment_date else (last_txn_payment.date if last_txn_payment else None)),
            "has_statement_data": liability is not None,
        }
    elif account.type == "depository":
        card_payments = (
            db.query(func.sum(Transaction.amount))
            .filter(
                Transaction.account_id == account.id,
                Transaction.is_credit_card_payment.is_(True),
                Transaction.amount > 0,
                Transaction.date >= start,
                Transaction.date <= end,
            )
            .scalar()
            or 0.0
        )
        last_deposit = (
            db.query(Transaction)
            .filter(Transaction.account_id == account.id, Transaction.amount < 0, Transaction.is_internal_transfer.is_(False))
            .order_by(Transaction.date.desc(), Transaction.amount)
            .first()
        )
        paycheck_merchants = {r["canonical_merchant"] for r in expected_income(db)}
        last_paycheck = (
            db.query(Transaction)
            .filter(
                Transaction.account_id == account.id,
                Transaction.amount < 0,
                Transaction.canonical_merchant.in_(paycheck_merchants),
            )
            .order_by(Transaction.date.desc())
            .first()
            if paycheck_merchants
            else None
        )
        summary["cash"] = {
            "last_paycheck": (
                {"name": last_paycheck.canonical_merchant, "amount": -last_paycheck.amount, "date": last_paycheck.date}
                if last_paycheck
                else None
            ),
            "money_in": summary["inflow_total"],
            "money_out": summary["spend_total"],
            "net": round(summary["inflow_total"] - summary["spend_total"], 2),
            "card_payments": round(card_payments, 2),
            "last_deposit": (
                {"name": last_deposit.canonical_merchant or last_deposit.name, "amount": -last_deposit.amount, "date": last_deposit.date}
                if last_deposit
                else None
            ),
        }
    elif account.type == "investment":
        holdings = db.query(Holding).filter_by(account_id=account.id).all()
        value = sum(h.institution_value or 0.0 for h in holdings)
        basis = sum(h.cost_basis or 0.0 for h in holdings if h.cost_basis is not None)
        contributions = (
            db.query(func.sum(Transaction.amount))
            .filter(
                Transaction.category_detailed == "TRANSFER_OUT_INVESTMENT_AND_RETIREMENT_FUNDS",
                Transaction.date >= today - timedelta(days=365),
            )
            .scalar()
            or 0.0
        )
        summary["investment"] = {
            "holdings_count": len(holdings),
            "holdings_value": round(value, 2),
            "cost_basis": round(basis, 2) if basis else None,
            "unrealized_gain": round(value - basis, 2) if basis else None,
            "contributions_12m": round(contributions, 2),
        }
    return summary


# --- balance history / projection ----------------------------------------------


def record_balance_snapshots(db: Session) -> None:
    today = date.today()
    for account in db.query(Account).all():
        existing = db.query(BalanceSnapshot).filter_by(account_id=account.id, recorded_at=today).one_or_none()
        if existing is None:
            db.add(BalanceSnapshot(account_id=account.id, balance=account.current_balance or 0.0, recorded_at=today))
        else:
            existing.balance = account.current_balance or 0.0


def _snapshot_net_worth(db: Session) -> dict[date, float]:
    rows = (
        db.query(BalanceSnapshot, Account.type)
        .join(Account, BalanceSnapshot.account_id == Account.id)
        .all()
    )
    by_date: dict[date, float] = defaultdict(float)
    for snapshot, account_type in rows:
        sign = -1 if account_type in LIABILITY_TYPES else 1
        by_date[snapshot.recorded_at] += sign * snapshot.balance
    return by_date


INVESTMENT_CONTRIBUTION_CATEGORY = "TRANSFER_OUT_INVESTMENT_AND_RETIREMENT_FUNDS"


def reconstructed_net_worth(db: Session, anchor: date | None = None) -> dict[date, float]:
    """Daily net worth before snapshots existed, rebuilt from transactions.

    Starts from the balances on the anchor day (the first real snapshot, so
    the estimate joins the real history without a jump) and walks backwards, undoing each day's
    posted transactions: a checking outflow is added back, a card purchase
    comes off the amount owed. Accounts with no transactions of their own
    (a brokerage that only reports a balance) are only moved by the
    contributions sent to them, so market gains aren't reconstructed.
    """
    first = db.query(func.min(Transaction.date)).scalar()
    if first is None:
        return {}
    accounts = db.query(Account).all()
    anchor_balances = {}
    if anchor is None:
        anchor = db.query(func.min(BalanceSnapshot.recorded_at)).scalar()
    if anchor is not None:
        anchor_balances = {
            snap.account_id: snap.balance for snap in db.query(BalanceSnapshot).filter(BalanceSnapshot.recorded_at == anchor)
        }
    today = anchor or date.today()

    posted = (
        db.query(Transaction.account_id, Transaction.date, func.sum(Transaction.amount))
        .filter(Transaction.pending.is_(False), Transaction.date <= today)
        .group_by(Transaction.account_id, Transaction.date)
        .all()
    )
    daily: dict[int, dict[date, float]] = defaultdict(dict)
    for account_id, day, total in posted:
        daily[account_id][day] = total

    contributions: dict[date, float] = defaultdict(float)
    for day, total in (
        db.query(Transaction.date, func.sum(Transaction.amount))
        .filter(Transaction.category_detailed == INVESTMENT_CONTRIBUTION_CATEGORY, Transaction.pending.is_(False))
        .group_by(Transaction.date)
    ):
        contributions[day] += total
    balance_only_investments = [a for a in accounts if a.type in ASSET_TYPES - {"depository"} and not daily.get(a.id)]

    balances = {a.id: anchor_balances.get(a.id, a.current_balance or 0.0) for a in accounts}

    # Brokerages with market prices: use the priced value for each day, shifted
    # so it meets the anchor snapshot exactly (no jump where real data starts).
    priced_investments: dict[int, dict[date, float]] = {}
    for a in accounts:
        if a.type != "investment":
            continue
        priced = portfolio_value_history(db, [a.id], first, today)["series"]
        if priced and today in priced:
            shift = balances[a.id] - priced[today]
            has_trades = (
                db.query(InvestmentTransaction.id)
                .filter(InvestmentTransaction.account_id == a.id, InvestmentTransaction.type.in_(["buy", "sell"]))
                .first()
                is not None
            )
            adjusted = {}
            for d, v in priced.items():
                # Without the brokerage's trades, today's shares are assumed held
                # all along; money deposited after day d wasn't invested yet (and
                # is already added back to checking), so take it out.
                later = 0.0 if has_trades else sum(amt for cd, amt in contributions.items() if d < cd <= today)
                adjusted[d] = v + shift - later
            priced_investments[a.id] = adjusted
    balance_only_investments = [a for a in balance_only_investments if a.id not in priced_investments]
    result: dict[date, float] = {}
    day = today
    while day >= first:
        net = 0.0
        for a in accounts:
            if a.id in priced_investments:
                net += priced_investments[a.id].get(day, balances[a.id])
                continue
            net += -balances[a.id] if a.type in LIABILITY_TYPES else balances[a.id]
        result[day] = net
        # Step to the end of the previous day by undoing today's activity.
        for a in accounts:
            amount = daily[a.id].get(day, 0.0)
            if a.type in LIABILITY_TYPES:
                balances[a.id] -= amount
            elif a.type == "depository":
                balances[a.id] += amount
        if balance_only_investments and contributions.get(day):
            share = contributions[day] / len(balance_only_investments)
            for a in balance_only_investments:
                balances[a.id] -= share
        day -= timedelta(days=1)
    return result


def net_worth_history(db: Session) -> list[dict]:
    """Daily net worth: real snapshots where synced, reconstructed before that."""
    snapshots = _snapshot_net_worth(db)
    reconstructed = reconstructed_net_worth(db)
    first_snapshot = min(snapshots) if snapshots else None
    points = {d: {"date": d, "net_worth": v, "estimated": True} for d, v in reconstructed.items() if first_snapshot is None or d < first_snapshot}
    for d, v in snapshots.items():
        points[d] = {"date": d, "net_worth": v, "estimated": False}
    return [points[d] for d in sorted(points)]


def net_worth_projection_series(current: float, monthly_net_savings: float, annual_return_pct: float, years: int = 10, start: date | None = None) -> list[dict]:
    """Month-by-month projection for charting, same math as compute_net_worth_projection."""
    start = start or date.today()
    rate = annual_return_pct / 100 / 12
    value = current
    series = []
    for month in range(1, years * 12 + 1):
        value = value * (1 + rate) + monthly_net_savings
        y, m = divmod(start.month - 1 + month, 12)
        series.append({"date": date(start.year + y, m + 1, min(start.day, 28)), "net_worth": value})
    return series


def history_data_confidence(history: list[dict]) -> str:
    if not history:
        return "provisional"
    span_days = (history[-1]["date"] - history[0]["date"]).days
    if span_days < 30:
        return "provisional"
    if span_days < 90:
        return "limited"
    return "established"


def trailing_monthly_net_savings(db: Session, lookback_days: int = 90) -> float:
    today = date.today()
    first_txn_date = db.query(func.min(Transaction.date)).scalar()
    if first_txn_date is None:
        return 0.0
    since = max(today - timedelta(days=lookback_days), first_txn_date)
    days = max((today - since).days, 1)

    spend = sum(total for _, total in spend_by_category(db, since, today))
    income = income_total(db, since, today)
    net_per_day = (income - spend) / days
    return net_per_day * 30.44


def compute_net_worth_projection(
    current_net_worth: float,
    monthly_net_savings: float,
    annual_return_pct: float,
    years_list: tuple[int, ...] = (1, 3, 5, 10),
) -> list[dict]:
    monthly_rate = annual_return_pct / 100 / 12
    results = []
    for years in years_list:
        months = years * 12
        if monthly_rate == 0:
            future_value = current_net_worth + monthly_net_savings * months
        else:
            future_value = current_net_worth * (1 + monthly_rate) ** months + monthly_net_savings * (
                ((1 + monthly_rate) ** months - 1) / monthly_rate
            )
        results.append({"years": years, "projected_value": future_value})
    return results


def monthly_income_spend_savings(db: Session, months: int = 6) -> list[dict]:
    today = date.today()
    results = []
    first_txn_date = db.query(func.min(Transaction.date)).scalar()
    month_cursor = today.replace(day=1)
    for _ in range(months):
        if month_cursor.month == 12:
            next_month_start = month_cursor.replace(year=month_cursor.year + 1, month=1, day=1)
        else:
            next_month_start = month_cursor.replace(month=month_cursor.month + 1, day=1)
        # Months before any synced history would chart as empty bars.
        if first_txn_date is None or next_month_start <= first_txn_date:
            break
        month_end = min(today, next_month_start - timedelta(days=1))

        spend_rows = spend_by_category(db, month_cursor, month_end)
        month_spend = round(sum(total for _, total in spend_rows), 2)
        month_income = round(income_total(db, month_cursor, month_end), 2) + 0.0  # + 0.0 turns -0.0 into 0.0
        results.append(
            {
                "month": month_cursor.strftime("%Y-%m"),
                "income": month_income,
                "spend": month_spend,
                "savings": round(month_income - month_spend, 2) + 0.0,
                "partial": month_cursor <= first_txn_date or month_end == today,
            }
        )
        month_cursor = (month_cursor - timedelta(days=1)).replace(day=1)

    return list(reversed(results))
