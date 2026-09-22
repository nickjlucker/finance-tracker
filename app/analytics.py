import statistics
from collections import defaultdict
from datetime import date, timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import Account, BalanceSnapshot, CardLiability, Transaction

ASSET_TYPES = {"depository", "investment"}
LIABILITY_TYPES = {"credit", "loan"}

# Plaid's own dedicated categories for these are unambiguous about economic meaning,
# so no matching is required to treat them as internal (non-spend) transfers.
TIER1_INTERNAL_CATEGORIES = {
    "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT",
    "TRANSFER_OUT_INVESTMENT_AND_RETIREMENT_FUNDS",
}

TRANSFER_MATCH_AMOUNT_TOLERANCE = 1.0
TRANSFER_MATCH_DAY_WINDOW = 3


def _is_tier2_category(category_detailed: str) -> bool:
    return (
        category_detailed.startswith("TRANSFER_IN_") or category_detailed.startswith("TRANSFER_OUT_")
    ) and category_detailed not in TIER1_INTERNAL_CATEGORIES


def reconcile_internal_transfers(db: Session) -> None:
    db.flush()

    tier1_txns = (
        db.query(Transaction)
        .filter(
            Transaction.category_detailed.in_(TIER1_INTERNAL_CATEGORIES),
            Transaction.is_internal_transfer.is_(False),
        )
        .all()
    )
    for txn in tier1_txns:
        txn.is_internal_transfer = True

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


def income_total(db: Session, start_date: date, end_date: date) -> float:
    total = (
        db.query(func.sum(Transaction.amount))
        .filter(
            Transaction.date >= start_date,
            Transaction.date <= end_date,
            Transaction.amount < 0,
            Transaction.is_internal_transfer.is_(False),
        )
        .scalar()
        or 0.0
    )
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


def cash_debt_net(db: Session) -> dict[str, float]:
    accounts = db.query(Account).all()
    cash = sum(a.current_balance or 0.0 for a in accounts if a.type == "depository")
    investments = sum(a.current_balance or 0.0 for a in accounts if a.type == "investment")
    debt = sum(a.current_balance or 0.0 for a in accounts if a.type in LIABILITY_TYPES)
    return {
        "cash": cash,
        "investments": investments,
        "debt": debt,
        "net_position": cash + investments - debt,
    }


def interest_paid_ttm(db: Session) -> float:
    since = date.today() - timedelta(days=365)
    return (
        db.query(func.sum(Transaction.amount))
        .filter(Transaction.category_detailed == "BANK_FEES_INTEREST_CHARGE", Transaction.date >= since)
        .scalar()
        or 0.0
    )


def detect_recurring_bills(db: Session) -> list[dict]:
    since = date.today() - timedelta(days=120)
    txns = (
        db.query(Transaction)
        .filter(Transaction.date >= since, Transaction.amount > 0, Transaction.is_internal_transfer.is_(False))
        .all()
    )

    groups = defaultdict(list)
    for txn in txns:
        key = (txn.merchant_name or txn.name).strip().upper()
        groups[key].append(txn)

    results = []
    for key, group in groups.items():
        group.sort(key=lambda t: t.date)
        clusters: list[list[Transaction]] = []
        for txn in group:
            for cluster in clusters:
                avg = sum(t.amount for t in cluster) / len(cluster)
                if avg and abs(txn.amount - avg) / avg <= 0.05:
                    cluster.append(txn)
                    break
            else:
                clusters.append([txn])

        for cluster in clusters:
            if len(cluster) < 3:
                continue
            dates = sorted(t.date for t in cluster)
            gaps = [(dates[i + 1] - dates[i]).days for i in range(len(dates) - 1)]
            avg_gap = sum(gaps) / len(gaps)
            if not (20 <= avg_gap <= 40):
                continue
            avg_amount = sum(t.amount for t in cluster) / len(cluster)
            last_date = dates[-1]
            results.append(
                {
                    "merchant": key.title(),
                    "average_amount": avg_amount,
                    "cadence_days": round(avg_gap),
                    "last_date": last_date,
                    "next_expected_date": last_date + timedelta(days=round(avg_gap)),
                }
            )

    return sorted(results, key=lambda r: -r["average_amount"])


def detect_flagged_transactions(db: Session) -> list[dict]:
    since = date.today() - timedelta(days=180)
    txns = (
        db.query(Transaction)
        .filter(Transaction.date >= since, Transaction.amount > 0, Transaction.is_internal_transfer.is_(False))
        .order_by(Transaction.date)
        .all()
    )

    by_merchant = defaultdict(list)
    for txn in txns:
        key = (txn.merchant_name or txn.name).strip().upper()
        by_merchant[key].append(txn)

    flags = []
    flagged_dupe_ids = set()
    for key, group in by_merchant.items():
        # Need enough history to know what "usual" looks like for this merchant.
        if len(group) >= 4:
            amounts = [t.amount for t in group]
            mean = statistics.mean(amounts)
            stdev = statistics.pstdev(amounts)
            for txn in group:
                if stdev == 0:
                    # Perfectly consistent price historically (e.g. a subscription) —
                    # any change at all (in either direction) is worth surfacing.
                    if txn.amount != mean:
                        flags.append(
                            {
                                "transaction_id": txn.transaction_id,
                                "date": txn.date,
                                "name": txn.name,
                                "amount": txn.amount,
                                "reason": f"price changed for {key.title()} (was always ${mean:,.2f})",
                            }
                        )
                else:
                    # Only flag unusually *high* charges — a cheaper-than-usual purchase
                    # isn't a financial risk worth surfacing. Require both a large z-score
                    # and a meaningful dollar swing so naturally variable merchants
                    # (Amazon, rideshare, groceries) don't spam on ordinary variance.
                    z_score = (txn.amount - mean) / stdev
                    if z_score > 2.5 and (txn.amount - mean) >= max(10.0, mean * 0.15):
                        flags.append(
                            {
                                "transaction_id": txn.transaction_id,
                                "date": txn.date,
                                "name": txn.name,
                                "amount": txn.amount,
                                "reason": f"unusually high charge for {key.title()} (typically ~${mean:,.2f})",
                            }
                        )

        sorted_group = sorted(group, key=lambda t: t.date)
        for a, b in zip(sorted_group, sorted_group[1:]):
            if a.transaction_id in flagged_dupe_ids or b.transaction_id in flagged_dupe_ids:
                continue
            if abs(a.amount - b.amount) < 0.01 and abs((b.date - a.date).days) <= 2:
                flags.append(
                    {
                        "transaction_id": b.transaction_id,
                        "date": b.date,
                        "name": b.name,
                        "amount": b.amount,
                        "reason": f"possible duplicate charge ({key.title()}, ${b.amount:,.2f} within 2 days)",
                    }
                )
                flagged_dupe_ids.add(a.transaction_id)
                flagged_dupe_ids.add(b.transaction_id)

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
                    {"severity": "warning", "message": f"{name} payment of ${liability.minimum_payment_amount or 0:,.2f} due {liability.next_payment_due_date.isoformat()}"}
                )

    interest = interest_paid_ttm(db)
    if interest > 0:
        alerts.append({"severity": "warning", "message": f"${interest:,.2f} in interest charged over the last 12 months"})

    bills = detect_recurring_bills(db)
    upcoming = sum(
        b["average_amount"]
        for b in bills
        if b["next_expected_date"] and 0 <= (b["next_expected_date"] - today).days <= 14
    )
    cash = cash_debt_net(db)["cash"]
    if upcoming > 0 and upcoming > cash:
        alerts.append(
            {
                "severity": "critical",
                "message": f"${upcoming:,.2f} in bills due within 2 weeks exceeds your ${cash:,.2f} cash on hand",
            }
        )

    severity_rank = {"critical": 0, "warning": 1, "info": 2}
    return sorted(alerts, key=lambda a: severity_rank.get(a["severity"], 3))


def record_balance_snapshots(db: Session) -> None:
    today = date.today()
    for account in db.query(Account).all():
        existing = db.query(BalanceSnapshot).filter_by(account_id=account.id, recorded_at=today).one_or_none()
        if existing is None:
            db.add(BalanceSnapshot(account_id=account.id, balance=account.current_balance or 0.0, recorded_at=today))
        else:
            existing.balance = account.current_balance or 0.0


def net_worth_history(db: Session) -> list[dict]:
    rows = (
        db.query(BalanceSnapshot, Account.type)
        .join(Account, BalanceSnapshot.account_id == Account.id)
        .all()
    )
    by_date: dict[date, float] = defaultdict(float)
    for snapshot, account_type in rows:
        sign = -1 if account_type in LIABILITY_TYPES else 1
        by_date[snapshot.recorded_at] += sign * snapshot.balance
    return [{"date": d, "net_worth": v} for d, v in sorted(by_date.items())]


def trailing_monthly_net_savings(db: Session, lookback_days: int = 90) -> float:
    today = date.today()
    first_txn_date = db.query(func.min(Transaction.date)).scalar()
    if first_txn_date is None:
        return 0.0
    since = max(today - timedelta(days=lookback_days), first_txn_date)
    days = max((today - since).days, 1)

    spend = (
        db.query(func.sum(Transaction.amount))
        .filter(
            Transaction.date >= since,
            Transaction.date <= today,
            Transaction.amount > 0,
            Transaction.is_internal_transfer.is_(False),
        )
        .scalar()
        or 0.0
    )
    income = (
        db.query(func.sum(Transaction.amount))
        .filter(
            Transaction.date >= since,
            Transaction.date <= today,
            Transaction.amount < 0,
            Transaction.is_internal_transfer.is_(False),
        )
        .scalar()
        or 0.0
    )
    net_per_day = (-income - spend) / days
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
