"""Wealth plan: per-paycheck investing targets, the cash buffer, spending
guardrails, and the opportunities that move the needle most.

Everything is measured per pay period (payday to the day before the next
payday), because that is when money actually arrives and can be moved.
"""

import json
from collections import defaultdict
from datetime import date, timedelta

from sqlalchemy.orm import Session

from app import analytics
from app.models import Account, AppSetting, Holding, InvestmentTransaction, Security, Transaction

# 2026 IRS limits (self-only HSA coverage; under-50 catch-up not included).
LIMIT_401K_2026 = 24_500
LIMIT_HSA_SELF_2026 = 4_400
LIMIT_IRA_2026 = 7_500
PAYCHECKS_PER_YEAR = 26

# Spending levers from the audit. Each matches transactions by Plaid detailed
# category or canonical merchant; caps are monthly and user-editable.
GUARDRAILS = [
    {
        "key": "amazon",
        "label": "Amazon & online shopping",
        "detailed": ["GENERAL_MERCHANDISE_ONLINE_MARKETPLACES"],
        "cap": 250,
        "tip": "Put non-essentials in the cart and buy them 7 days later if you still want them.",
    },
    {
        "key": "groceries",
        "label": "Groceries",
        "detailed": ["FOOD_AND_DRINK_GROCERIES"],
        "cap": 450,
        "tip": "One planned weekly shop instead of near-daily top-ups; a lower-cost store for staples.",
    },
    {
        "key": "dining",
        "label": "Dining, takeout & delivery",
        "detailed": [
            "FOOD_AND_DRINK_RESTAURANT",
            "FOOD_AND_DRINK_FAST_FOOD",
            "FOOD_AND_DRINK_COFFEE",
            "FOOD_AND_DRINK_BEER_WINE_AND_LIQUOR",
            "FOOD_AND_DRINK_OTHER_FOOD_AND_DRINK",
        ],
        "cap": 350,
        "tip": "Pick two nights a week to eat out and plan the rest.",
    },
    {
        "key": "rideshare",
        "label": "Rideshare",
        "detailed": ["TRANSPORTATION_TAXIS_AND_RIDE_SHARES"],
        "cap": 75,
        "tip": "Default to driving or transit for routine trips.",
    },
    {
        "key": "fun",
        "label": "Games & entertainment",
        "detailed": ["ENTERTAINMENT_VIDEO_GAMES", "ENTERTAINMENT_OTHER_ENTERTAINMENT", "ENTERTAINMENT_SPORTING_EVENTS_AMUSEMENT_PARKS_AND_MUSEUMS"],
        "cap": 40,
        "tip": "One planned purchase a month; skip in-game and marketplace buys.",
    },
    {
        "key": "shopping",
        "label": "Other shopping & vape",
        "detailed": [
            "GENERAL_MERCHANDISE_CLOTHING_AND_ACCESSORIES",
            "GENERAL_MERCHANDISE_OTHER_GENERAL_MERCHANDISE",
            "GENERAL_MERCHANDISE_TOBACCO_AND_VAPE",
            "GENERAL_MERCHANDISE_ELECTRONICS",
            "GENERAL_MERCHANDISE_DEPARTMENT_STORES",
            "GENERAL_MERCHANDISE_SPORTING_GOODS",
        ],
        "cap": 120,
        "tip": "Wishlist it and revisit at the start of next month.",
    },
    {
        "key": "cash",
        "label": "ATM cash",
        "detailed": ["TRANSFER_OUT_WITHDRAWAL"],
        "cap": 60,
        "tip": "Cash is untracked spending; use a card so it shows up here.",
    },
]

HSA_ELIGIBLE_DETAILED = {
    "MEDICAL_DENTAL_CARE",
    "MEDICAL_EYE_CARE",
    "MEDICAL_PHARMACIES_AND_SUPPLEMENTS",
    "MEDICAL_PRIMARY_CARE",
    "MEDICAL_NURSING_CARE",
    "MEDICAL_OTHER_MEDICAL",
}

DEFAULT_SETTINGS = {
    "invest_min_per_paycheck": 500.0,
    "invest_stretch_per_paycheck": 750.0,
    "emergency_months": 1.0,
    "emergency_target": None,  # None = derive from essential monthly costs
    "guardrail_caps": {g["key"]: g["cap"] for g in GUARDRAILS},
}

BASELINE_DAYS = 91  # ~13 weeks, 6.5 pay periods


# --- settings --------------------------------------------------------------------


def get_settings(db: Session) -> dict:
    row = db.get(AppSetting, "wealth_plan")
    stored = json.loads(row.value) if row and row.value else {}
    settings = {**DEFAULT_SETTINGS, **stored}
    settings["guardrail_caps"] = {**DEFAULT_SETTINGS["guardrail_caps"], **stored.get("guardrail_caps", {})}
    return settings


def update_settings(db: Session, patch: dict) -> dict:
    current = get_settings(db)
    for key, value in patch.items():
        if key == "guardrail_caps" and isinstance(value, dict):
            current["guardrail_caps"].update({k: float(v) for k, v in value.items() if k in current["guardrail_caps"]})
        elif key in DEFAULT_SETTINGS:
            current[key] = value
    row = db.get(AppSetting, "wealth_plan")
    if row is None:
        row = AppSetting(key="wealth_plan")
        db.add(row)
    row.value = json.dumps(current)
    db.flush()  # later reads in the same session must see the new values
    return current


# --- pay periods ----------------------------------------------------------------


def paychecks(db: Session) -> tuple[list[Transaction], date | None]:
    """Posted paychecks (oldest first) and the next expected payday."""
    income_items = analytics.expected_income(db)
    merchants = {r["canonical_merchant"] for r in income_items}
    if not merchants:
        return [], None
    txns = (
        db.query(Transaction)
        .join(Account, Transaction.account_id == Account.id)
        .filter(Account.type == "depository", Transaction.amount < 0, Transaction.canonical_merchant.in_(merchants))
        .order_by(Transaction.date)
        .all()
    )
    upcoming = [r["next_expected_date"] for r in income_items if r["next_expected_date"]]
    return txns, (min(upcoming) if upcoming else None)


def _investment_accounts(db: Session) -> dict[str, list[int]]:
    kinds: dict[str, list[int]] = defaultdict(list)
    for a in db.query(Account).filter(Account.type == "investment").all():
        sub = (a.subtype or "").lower()
        kind = "hsa" if sub == "hsa" else "retirement" if sub in {"401k", "403b", "457b", "ira", "roth", "roth 401k"} else "taxable"
        kinds[kind].append(a.id)
    return kinds


def _period_investing(db: Session, start: date, end: date, kinds: dict[str, list[int]]) -> dict:
    """Money that went to investing in [start, end], split by where it came from."""
    from_checking = sum(
        t.amount
        for t in db.query(Transaction).filter(
            Transaction.category_detailed == analytics.INVESTMENT_CONTRIBUTION_CATEGORY,
            Transaction.date >= start,
            Transaction.date <= end,
            Transaction.pending.is_(False),
        )
    )
    out = {"from_checking": round(from_checking, 2), "retirement_payroll": 0.0, "hsa_you": 0.0, "employer": 0.0}
    for kind, ids in kinds.items():
        if kind == "taxable" or not ids:
            continue
        for t in db.query(InvestmentTransaction).filter(
            InvestmentTransaction.account_id.in_(ids), InvestmentTransaction.date >= start, InvestmentTransaction.date <= end
        ):
            if analytics._classify_investment_txn(t) != "contribution":
                continue
            amount = abs(t.amount)
            if analytics.is_employer_contribution(t):
                out["employer"] += amount
            elif kind == "hsa":
                out["hsa_you"] += amount
            else:
                out["retirement_payroll"] += amount
    return {k: round(v, 2) for k, v in out.items()}


def _period_spend(db: Session, start: date, end: date) -> float:
    return sum(t.amount for t in analytics._window_spend(db, [a.id for a in db.query(Account).all()], start, end))


def pay_period_history(db: Session, settings: dict, count: int = 8) -> list[dict]:
    checks, next_payday = paychecks(db)
    if not checks:
        return []
    today = date.today()
    kinds = _investment_accounts(db)
    paydays = sorted({t.date for t in checks})
    periods = []
    for i, payday in enumerate(paydays[-count:]):
        idx = paydays.index(payday)
        nxt = paydays[idx + 1] if idx + 1 < len(paydays) else (next_payday or payday + timedelta(days=14))
        end = nxt - timedelta(days=1)
        complete = end < today
        take_home = -sum(t.amount for t in checks if t.date == payday)
        investing = _period_investing(db, payday, min(end, today), kinds)
        invested = investing["from_checking"]
        if not complete:
            status = "stretch" if invested >= settings["invest_stretch_per_paycheck"] else "in_progress"
        elif invested >= settings["invest_stretch_per_paycheck"]:
            status = "stretch"
        elif invested >= settings["invest_min_per_paycheck"]:
            status = "hit"
        else:
            status = "missed"
        spend = _period_spend(db, payday, min(end, today))
        periods.append(
            {
                "start": payday,
                "end": end,
                "complete": complete,
                "take_home": round(take_home, 2),
                "spend": round(spend, 2),
                "left_over": round(take_home - spend, 2),
                "investing": investing,
                "invested_from_pay": invested,
                "status": status,
            }
        )
    # The next paycheck is due (today or overdue) but hasn't posted: the new
    # pay period has started, so show it rather than the one that just ended.
    if next_payday and next_payday <= today and (not periods or next_payday > periods[-1]["start"]):
        investing = _period_investing(db, next_payday, today, kinds)
        periods.append(
            {
                "start": next_payday,
                "end": next_payday + timedelta(days=13),
                "complete": False,
                "expected": True,
                "take_home": round(sum(p["take_home"] for p in periods[-4:]) / max(len(periods[-4:]), 1), 2),
                "spend": round(_period_spend(db, next_payday, today), 2),
                "left_over": 0.0,
                "investing": investing,
                "invested_from_pay": investing["from_checking"],
                "status": "stretch" if investing["from_checking"] >= settings["invest_stretch_per_paycheck"] else "in_progress",
            }
        )
    return periods


def _streak(periods: list[dict]) -> int:
    streak = 0
    for p in reversed([p for p in periods if p["complete"]]):
        if p["status"] in {"hit", "stretch"}:
            streak += 1
        else:
            break
    return streak


# --- guardrails, buffer, opportunities --------------------------------------------


def _monthly(amount: float, days: int) -> float:
    return amount / max(days, 1) * 30.4


def guardrails(db: Session, settings: dict) -> list[dict]:
    today = date.today()
    base_start = today - timedelta(days=BASELINE_DAYS - 1)
    month_start = today.replace(day=1)
    days_in_month = ((month_start + timedelta(days=32)).replace(day=1) - month_start).days
    ids = [a.id for a in db.query(Account).all()]
    baseline_txns = analytics._window_spend(db, ids, base_start, today)
    out = []
    for g in GUARDRAILS:
        cats = set(g["detailed"])
        base = sum(t.amount for t in baseline_txns if t.category_detailed in cats)
        mtd_txns = [t for t in baseline_txns if t.category_detailed in cats and t.date >= month_start]
        mtd = sum(t.amount for t in mtd_txns)
        cap = float(settings["guardrail_caps"].get(g["key"], g["cap"]))
        baseline_monthly = _monthly(base, BASELINE_DAYS)
        pace = mtd / today.day * days_in_month if today.day else mtd
        top = defaultdict(float)
        for t in baseline_txns:
            if t.category_detailed in cats:
                top[t.canonical_merchant or t.name] += t.amount
        out.append(
            {
                "key": g["key"],
                "label": g["label"],
                "tip": g["tip"],
                "cap": cap,
                "baseline_monthly": round(baseline_monthly, 2),
                "month_to_date": round(mtd, 2),
                "month_pace": round(pace, 2),
                "status": "over" if mtd > cap else ("at_risk" if pace > cap * 1.05 else "ok"),
                "monthly_savings_if_held": round(max(0.0, baseline_monthly - cap), 2),
                "top_merchants": [
                    {"name": k, "monthly": round(_monthly(v, BASELINE_DAYS), 2)} for k, v in sorted(top.items(), key=lambda kv: -kv[1])[:3]
                ],
            }
        )
    return out


def essential_monthly(db: Session, settings: dict) -> dict:
    """Bills plus a groceries allowance: what one month of 'must pay' costs."""
    bills = sum(
        r["average_amount"] * 30.4 / r["cadence_days"]
        for r in analytics.analyze_recurring(db, "spend")
        if r["tier"] == "fixed_obligation" and r["cadence_days"]
    )
    groceries = float(settings["guardrail_caps"].get("groceries", 450))
    return {"bills": round(bills, 2), "groceries": groceries, "total": round(bills + groceries, 2)}


def _hsa_reimbursable(db: Session) -> dict:
    since = date.today() - timedelta(days=365)
    rows = (
        db.query(Transaction)
        .join(Account, Transaction.account_id == Account.id)
        .filter(
            Transaction.category_detailed.in_(HSA_ELIGIBLE_DETAILED),
            Transaction.amount > 0,
            Transaction.date >= since,
            Transaction.is_internal_transfer.is_(False),
            Account.subtype != "hsa",
        )
        .order_by(Transaction.amount.desc())
        .all()
    )
    return {
        "total": round(sum(t.amount for t in rows), 2),
        "items": [{"date": t.date, "name": t.canonical_merchant or t.name, "amount": t.amount} for t in rows[:6]],
    }


def _next_three_paycheck_month(last_payday: date | None) -> date | None:
    if last_payday is None:
        return None
    day = last_payday
    for _ in range(60):
        day += timedelta(days=14)
        month_paydays = [day + timedelta(days=14 * k) for k in range(-3, 4)]
        if sum(1 for d in month_paydays if d.month == day.month and d.year == day.year) >= 3:
            return day.replace(day=1)
    return None


def build_plan(db: Session) -> dict:
    settings = get_settings(db)
    today = date.today()
    periods = pay_period_history(db, settings)
    checks, next_payday = paychecks(db)
    posted = [p for p in periods if not p.get("expected")][-6:]
    avg_check = sum(p["take_home"] for p in posted) / len(posted) if posted else 0.0
    for p in periods:
        if p.get("expected"):
            p["take_home"] = round(avg_check, 2)
    monthly_take_home = avg_check * PAYCHECKS_PER_YEAR / 12

    ids = [a.id for a in db.query(Account).all()]
    base_start = today - timedelta(days=BASELINE_DAYS - 1)
    monthly_spend = _monthly(sum(t.amount for t in analytics._window_spend(db, ids, base_start, today)), BASELINE_DAYS)

    rails = guardrails(db, settings)
    lever_savings = sum(r["monthly_savings_if_held"] for r in rails)
    min_monthly = settings["invest_min_per_paycheck"] * PAYCHECKS_PER_YEAR / 12
    stretch_monthly = settings["invest_stretch_per_paycheck"] * PAYCHECKS_PER_YEAR / 12
    surplus_now = monthly_take_home - monthly_spend
    surplus_with_levers = surplus_now + lever_savings

    essentials = essential_monthly(db, settings)
    buffer_target = float(settings["emergency_target"] or round(essentials["total"] * float(settings["emergency_months"]), -1))
    cash = analytics.asset_breakdown(db)["cash"]
    forecast = analytics.forecast_cash_flow(db, days=30)
    hsa = _hsa_reimbursable(db)

    # What to do with the next/current paycheck.
    buffer_gap = max(0.0, buffer_target - cash)
    room_after_bills = forecast["lowest_balance"] - buffer_target
    # "1 month, then invest": the per-paycheck target fills any buffer gap
    # first, and whatever is left of it goes to investing.
    to_buffer = min(settings["invest_min_per_paycheck"], buffer_gap)
    to_invest = settings["invest_min_per_paycheck"] - to_buffer
    if buffer_gap == 0 and room_after_bills > settings["invest_min_per_paycheck"]:
        to_invest = min(settings["invest_stretch_per_paycheck"], room_after_bills)
    action = "invest" if to_buffer == 0 else ("split" if to_invest > 0 else "build_buffer")
    amount = to_buffer + to_invest
    this_period = periods[-1] if periods else None

    # Opportunities, largest first.
    kinds = _investment_accounts(db)
    opportunities = []
    if hsa["total"] > 0 and buffer_gap > 0:
        opportunities.append(
            {
                "key": "hsa_reimburse",
                "title": f"Reimburse {len(hsa['items'])} medical expense{'s' if len(hsa['items']) != 1 else ''} from your HSA",
                "amount": hsa["total"],
                "detail": "Tax-free, one time. Covers most of your cash-buffer gap so paychecks can go to investing sooner. Keep the receipts; check each item is a qualified expense.",
                "items": hsa["items"],
            }
        )
    idle_hsa = sum(
        h.institution_value or 0.0
        for h, sec in db.query(Holding, Security).join(Security, Holding.security_id == Security.id).filter(Holding.account_id.in_(kinds.get("hsa", [])))
        if (sec.type or "").lower() == "cash"
    )
    if idle_hsa >= 200:
        opportunities.append(
            {
                "key": "hsa_idle_cash",
                "title": "Invest the cash sitting in your HSA",
                "amount": round(idle_hsa, 2),
                "detail": "It's in a money-market fund earning cash rates. Keep what you plan to reimburse; invest the rest in your HSA's funds.",
            }
        )
    year_start = today.replace(month=1, day=1)
    ytd = _period_investing(db, year_start, today, kinds)
    # Average payroll contributions only over pay periods the brokerage has
    # history for; earlier periods would read as $0 and drag the pace down.
    first_contribution = (
        db.query(InvestmentTransaction.date)
        .filter(InvestmentTransaction.account_id.in_(kinds.get("hsa", []) + kinds.get("retirement", [])))
        .order_by(InvestmentTransaction.date)
        .first()
    )
    covered = [p for p in periods if first_contribution and p["end"] >= first_contribution[0]][-6:]
    complete_covered = [p for p in covered if p["complete"]] or covered

    def per_check(key: str) -> float:
        return sum(p["investing"][key] for p in complete_covered) / len(complete_covered) if complete_covered else 0.0

    payroll_per_check = per_check("retirement_payroll")
    employer_hsa_per_check = per_check("employer")
    hsa_you_per_check = per_check("hsa_you")
    if kinds.get("hsa"):
        employer_annual = employer_hsa_per_check * PAYCHECKS_PER_YEAR
        room = max(0.0, LIMIT_HSA_SELF_2026 - employer_annual - hsa_you_per_check * PAYCHECKS_PER_YEAR)
        if room > 100:
            opportunities.append(
                {
                    "key": "hsa_payroll",
                    "title": f"Add ~${room / PAYCHECKS_PER_YEAR:,.0f} per paycheck to your HSA through payroll",
                    "amount": round(room, 2),
                    "detail": f"You contribute almost nothing yourself; your employer adds about ${employer_annual:,.0f}/yr. The 2026 self-only limit is ${LIMIT_HSA_SELF_2026:,}. Payroll HSA money skips income and FICA tax, so it costs less take-home than it adds.",
                }
            )
    if kinds.get("retirement"):
        annual = payroll_per_check * PAYCHECKS_PER_YEAR
        if annual < LIMIT_401K_2026:
            opportunities.append(
                {
                    "key": "401k_room",
                    "title": f"401(k): on pace for about ${annual:,.0f} this year",
                    "amount": round(LIMIT_401K_2026 - annual, 2),
                    "detail": f"The 2026 limit is ${LIMIT_401K_2026:,}. First confirm you get the full employer match; each extra 1% of pay is pre-tax, so it trims take-home by less than it invests.",
                }
            )
    opportunities.append(
        {
            "key": "ira",
            "title": "Open a Roth IRA",
            "amount": LIMIT_IRA_2026,
            "detail": f"Up to ${LIMIT_IRA_2026:,} a year grows tax-free (income limits apply). A good home for the per-paycheck investing target at Fidelity.",
        }
    )
    three_check = _next_three_paycheck_month(checks[-1].date if checks else None)
    if three_check:
        opportunities.append(
            {
                "key": "third_paycheck",
                "title": f"{three_check:%B %Y} has three paychecks",
                "amount": round(avg_check, 2),
                "detail": "Your bills are sized for two. Send the whole third check to investing.",
            }
        )
    rent = next((r for r in analytics.analyze_recurring(db, "spend") if r["tier"] == "fixed_obligation" and r["category_primary"] == "RENT_AND_UTILITIES" and r["average_amount"] > 1000), None)
    if rent and monthly_take_home:
        share = rent["average_amount"] / monthly_take_home
        if share > 0.33:
            target = monthly_take_home * 0.30
            opportunities.append(
                {
                    "key": "housing",
                    "title": f"Rent is {share:.0%} of take-home",
                    "amount": round(rent["average_amount"] - target, 2),
                    "detail": f"The single biggest structural cost. At lease renewal, aim for about ${target:,.0f}/mo (30%): negotiate, add a roommate, or move. Each $100/mo less is $1,200/yr more to invest.",
                }
            )

    return {
        "settings": settings,
        "today": today,
        "next_payday": next_payday,
        "average_paycheck": round(avg_check, 2),
        "this_period": this_period,
        "action": {"kind": action, "amount": round(amount, 2), "to_buffer": round(to_buffer, 2), "to_invest": round(to_invest, 2)},
        "periods": periods,
        "streak": _streak(periods),
        "buffer": {
            "target": buffer_target,
            "cash": round(cash, 2),
            "gap": round(buffer_gap, 2),
            "essentials": essentials,
            "months": settings["emergency_months"],
        },
        "forecast_low": {"amount": round(forecast["lowest_balance"], 2), "date": forecast["lowest_balance_date"]},
        "gap": {
            "monthly_take_home": round(monthly_take_home, 2),
            "monthly_spend": round(monthly_spend, 2),
            "surplus_now": round(surplus_now, 2),
            "lever_savings": round(lever_savings, 2),
            "surplus_with_levers": round(surplus_with_levers, 2),
            "min_monthly": round(min_monthly, 2),
            "stretch_monthly": round(stretch_monthly, 2),
        },
        "guardrails": rails,
        "payroll": {
            "retirement_per_check": round(payroll_per_check, 2),
            "hsa_you_per_check": round(hsa_you_per_check, 2),
            "employer_per_check": round(employer_hsa_per_check, 2),
            "ytd": ytd,
        },
        "opportunities": opportunities,
    }
