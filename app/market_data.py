"""Historical daily closing prices from third-party market data providers.

Plaid reports today's holdings but not past prices, so charting how a
portfolio has grown needs a price history per ticker. Providers are tried in
order (Tiingo first when TIINGO_API_KEY is set, then Yahoo Finance, which
needs no key) and results are cached in the security_prices table so each
trading day is fetched once.

Closes are split-adjusted but not dividend-adjusted: multiplying today's
share count by a past close gives what that position was worth that day.
"""

import logging
from datetime import date, datetime, timedelta, timezone

import httpx
from sqlalchemy.orm import Session

from app.config import settings
from app.models import SecurityPrice

logger = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(15.0)
_HEADERS = {"User-Agent": "Mozilla/5.0 (personal finance tracker)"}
# Security types with no exchange-listed daily close to look up.
UNPRICEABLE_TYPES = {"derivative", "cash", "loan", "fixed income", "other"}


class ProviderError(Exception):
    pass


def _provider_symbol(ticker: str) -> str:
    # Plaid uses "BRK.B"; Yahoo and Tiingo use "BRK-B".
    return ticker.replace(".", "-").upper()


def _yahoo_closes(ticker: str, start: date, end: date) -> dict[date, float]:
    period1 = int(datetime(start.year, start.month, start.day, tzinfo=timezone.utc).timestamp())
    period2 = int(datetime(end.year, end.month, end.day, tzinfo=timezone.utc).timestamp()) + 86400
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{_provider_symbol(ticker)}"
    try:
        resp = httpx.get(url, params={"period1": period1, "period2": period2, "interval": "1d"}, headers=_HEADERS, timeout=_TIMEOUT)
        resp.raise_for_status()
        result = (resp.json().get("chart") or {}).get("result") or []
    except (httpx.HTTPError, ValueError) as exc:
        raise ProviderError(f"yahoo: {exc}") from exc
    if not result:
        raise ProviderError(f"yahoo: no data for {ticker}")
    chart = result[0]
    offset = chart.get("meta", {}).get("gmtoffset", 0)
    closes = (chart.get("indicators", {}).get("quote") or [{}])[0].get("close") or []
    out = {}
    for ts, close in zip(chart.get("timestamp") or [], closes):
        if close is None:
            continue
        # Timestamps are the session open in UTC; shift to exchange-local time for the trading date.
        day = datetime.fromtimestamp(ts + offset, tz=timezone.utc).date()
        out[day] = float(close)
    return out


def _tiingo_closes(ticker: str, start: date, end: date) -> dict[date, float]:
    if not settings.tiingo_api_key:
        raise ProviderError("tiingo: no API key")
    url = f"https://api.tiingo.com/tiingo/daily/{_provider_symbol(ticker)}/prices"
    params = {"startDate": start.isoformat(), "endDate": end.isoformat(), "token": settings.tiingo_api_key}
    try:
        resp = httpx.get(url, params=params, headers=_HEADERS, timeout=_TIMEOUT)
        resp.raise_for_status()
        rows = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise ProviderError(f"tiingo: {exc}") from exc
    # Tiingo's "close" is raw. Split-adjust it by walking back from the newest
    # day: every close before a split is divided by that split's factor.
    out, cumulative_split = {}, 1.0
    for row in sorted(rows, key=lambda r: r["date"], reverse=True):
        if row.get("close") is not None:
            out[date.fromisoformat(row["date"][:10])] = float(row["close"]) / cumulative_split
        cumulative_split *= float(row.get("splitFactor") or 1.0)
    return out


def _providers():
    providers = []
    if settings.tiingo_api_key:
        providers.append(("tiingo", _tiingo_closes))
    providers.append(("yahoo", _yahoo_closes))
    return providers


def fetch_closes(ticker: str, start: date, end: date) -> tuple[str, dict[date, float]]:
    errors = []
    for name, fetch in _providers():
        try:
            closes = fetch(ticker, start, end)
            if closes:
                return name, closes
            errors.append(f"{name}: empty")
        except ProviderError as exc:
            errors.append(str(exc))
    raise ProviderError("; ".join(errors))


def refresh_prices(db: Session, tickers: list[str], start: date, end: date | None = None) -> dict[str, int]:
    """Fill the price cache for each ticker from `start` to `end`; returns rows added per ticker."""
    end = end or date.today()
    added: dict[str, int] = {}
    for ticker in sorted(set(t for t in tickers if t)):
        cached = {row.date for row in db.query(SecurityPrice.date).filter(SecurityPrice.ticker == ticker, SecurityPrice.date >= start)}
        latest = max(cached) if cached else None
        # Re-fetch a few days back so late corrections replace provisional closes.
        fetch_from = start if latest is None or min(cached) > start + timedelta(days=7) else latest - timedelta(days=5)
        if fetch_from > end:
            continue
        try:
            source, closes = fetch_closes(ticker, fetch_from, end)
        except ProviderError as exc:
            logger.warning("No price history for %s: %s", ticker, exc)
            continue
        count = 0
        for day, close in closes.items():
            row = db.query(SecurityPrice).filter_by(ticker=ticker, date=day).one_or_none()
            if row is None:
                db.add(SecurityPrice(ticker=ticker, date=day, close=close, source=source))
                count += 1
            else:
                row.close, row.source = close, source
        added[ticker] = count
    db.flush()
    return added


def price_series(db: Session, ticker: str, start: date, end: date) -> dict[date, float]:
    """Daily closes with weekends/holidays forward-filled from the last trading day."""
    rows = (
        db.query(SecurityPrice.date, SecurityPrice.close)
        .filter(SecurityPrice.ticker == ticker, SecurityPrice.date <= end)
        .order_by(SecurityPrice.date)
        .all()
    )
    if not rows:
        return {}
    by_day = dict(rows)
    out, last = {}, None
    # Seed from the last close before the window so day one isn't empty.
    before = [c for d, c in rows if d < start]
    last = before[-1] if before else None
    day = start
    while day <= end:
        if day in by_day:
            last = by_day[day]
        if last is not None:
            out[day] = last
        day += timedelta(days=1)
    return out
