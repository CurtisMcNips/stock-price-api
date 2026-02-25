"""
Market Brain — 💰 Earnings Bot
────────────────────────────────
Data sources (priority order):
  1. FMP  — best for UK/EU stocks (LSE coverage)
  2. Yahoo Finance — primary for US stocks
  3. Alpha Vantage — fallback if Yahoo rate limits

Produces:
  signal_inputs.daysToEarnings   days until next earnings (0-90)
  signal_inputs.earningsBeat     EPS surprise % (-25 to +40)
"""

import logging
import os
from datetime import datetime, timezone
from typing import Optional

import httpx

from base import ResearchBot, BotResult

log = logging.getLogger("mb.bots.earnings")

FMP_API_KEY   = os.environ.get("FMP_API_KEY", "REi5YWMduTkssRQFsNyEONemYwbbSjro")
AV_API_KEY = os.environ.get("ALPHA_VANTAGE_KEY", "KH9A652VHUDYN4SK")

FMP_BASE      = "https://financialmodelingprep.com/api/v3"
AV_BASE       = "https://www.alphavantage.co/query"
YAHOO_SUMMARY = "https://query2.finance.yahoo.com/v10/finance/quoteSummary/{symbol}"
YAHOO_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
}
CACHE_TTL = 14400   # 4 hours


def _is_uk_ticker(ticker: str) -> bool:
    return ticker.endswith(".L") or ticker.endswith(".IL")


async def _fetch_fmp_earnings(ticker: str) -> Optional[dict]:
    """FMP — best for UK/EU tickers. Returns parsed earnings dict."""
    fmp_ticker = ticker.replace(".L", ".LSE").replace(".IL", ".LSE")
    try:
        async with httpx.AsyncClient(timeout=12) as client:
            cal_r  = await client.get(f"{FMP_BASE}/earning_calendar",
                params={"symbol": fmp_ticker, "apikey": FMP_API_KEY})
            hist_r = await client.get(f"{FMP_BASE}/earnings-surprises/{fmp_ticker}",
                params={"apikey": FMP_API_KEY})
        cal_data  = cal_r.json()  if cal_r.status_code  == 200 else []
        hist_data = hist_r.json() if hist_r.status_code == 200 else []
        if not cal_data and not hist_data:
            return None

        days_to_earnings = None
        earnings_date_str = None
        now = datetime.now(tz=timezone.utc)
        for event in sorted(cal_data, key=lambda x: x.get("date", "")):
            try:
                dt    = datetime.strptime(event["date"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
                delta = (dt - now).days
                if delta >= 0:
                    days_to_earnings  = delta
                    earnings_date_str = dt.strftime("%d %b %Y")
                    break
            except Exception:
                continue

        eps_surprises = []
        for q in hist_data[:4]:
            actual   = q.get("actualEarningResult")
            estimate = q.get("estimatedEarning")
            if actual is not None and estimate is not None and estimate != 0:
                eps_surprises.append(round(((actual - estimate) / abs(estimate)) * 100, 1))

        return {"days_to_earnings": days_to_earnings, "earnings_date": earnings_date_str,
                "eps_surprises": eps_surprises, "source": "FMP"}
    except Exception as e:
        log.warning(f"FMP earnings failed for {ticker}: {e}")
        return None


async def _fetch_yahoo_earnings(ticker: str) -> Optional[dict]:
    """Yahoo Finance — primary for US stocks."""
    try:
        async with httpx.AsyncClient(timeout=12) as client:
            r = await client.get(YAHOO_SUMMARY.format(symbol=ticker),
                params={"modules": "calendarEvents,earningsHistory,defaultKeyStatistics"},
                headers=YAHOO_HEADERS)
            if r.status_code != 200:
                return None
            data = r.json()

        result  = data.get("quoteSummary", {}).get("result", [{}])[0]
        cal     = result.get("calendarEvents", {})
        history = result.get("earningsHistory", {})
        stats   = result.get("defaultKeyStatistics", {})

        def rv(d, k):
            v = d.get(k, {}); return v.get("raw") if isinstance(v, dict) else v

        days_to_earnings = None
        earnings_date_str = None
        dates = cal.get("earnings", {}).get("earningsDate", [])
        if dates:
            ts = dates[0].get("raw")
            if ts:
                dt = datetime.fromtimestamp(ts, tz=timezone.utc)
                days_to_earnings  = max(0, (dt - datetime.now(tz=timezone.utc)).days)
                earnings_date_str = dt.strftime("%d %b %Y")

        eps_surprises = []
        for q in history.get("history", [])[-4:]:
            actual = rv(q, "epsActual"); estimate = rv(q, "epsEstimate")
            if actual is not None and estimate is not None and estimate != 0:
                eps_surprises.append(round(((actual - estimate) / abs(estimate)) * 100, 1))

        return {"days_to_earnings": days_to_earnings, "earnings_date": earnings_date_str,
                "eps_surprises": eps_surprises, "short_ratio": rv(stats, "shortRatio"),
                "source": "Yahoo Finance"}
    except Exception as e:
        log.warning(f"Yahoo earnings failed for {ticker}: {e}")
        return None


async def _fetch_av_earnings(ticker: str) -> Optional[dict]:
    """Alpha Vantage fallback — US stocks, 25 req/day."""
    try:
        async with httpx.AsyncClient(timeout=12) as client:
            r = await client.get(AV_BASE, params={
                "function": "EARNINGS", "symbol": ticker, "apikey": AV_API_KEY})
            if r.status_code != 200:
                return None
            data = r.json()
        quarterly = data.get("quarterlyEarnings", [])
        if not quarterly:
            return None
        eps_surprises = []
        for q in quarterly[:4]:
            try:
                eps_surprises.append(round(float(q.get("surprisePercentage", 0)), 1))
            except (ValueError, TypeError):
                continue
        return {"days_to_earnings": None, "earnings_date": None,
                "eps_surprises": eps_surprises, "source": "Alpha Vantage"}
    except Exception as e:
        log.warning(f"Alpha Vantage earnings failed for {ticker}: {e}")
        return None


class EarningsBot(ResearchBot):
    """Fetches real earnings dates and EPS surprise history."""

    @property
    def name(self) -> str:
        return "EarningsBot"

    @property
    def cache_ttl(self) -> int:
        return CACHE_TTL

    async def _fetch(self, ticker: str, asset_meta: dict) -> BotResult:
        asset_type = asset_meta.get("asset_type", "stock")
        if asset_type in ("crypto", "forex", "commodity", "etf"):
            return self._empty_result(ticker, f"Earnings not applicable for {asset_type}")

        # Source priority chain
        earnings_data = None
        if _is_uk_ticker(ticker):
            earnings_data = await _fetch_fmp_earnings(ticker)
            if not earnings_data:
                earnings_data = await _fetch_yahoo_earnings(ticker)
        else:
            earnings_data = await _fetch_yahoo_earnings(ticker)
            if not earnings_data or not earnings_data.get("eps_surprises"):
                av = await _fetch_av_earnings(ticker)
                if av and av.get("eps_surprises"):
                    if earnings_data:
                        earnings_data["eps_surprises"] = av["eps_surprises"]
                        earnings_data["source"] = "Yahoo + Alpha Vantage"
                    else:
                        earnings_data = av

        if not earnings_data:
            return self._empty_result(ticker, "No earnings data available from any source")

        days_to_earnings  = earnings_data.get("days_to_earnings")
        earnings_date_str = earnings_data.get("earnings_date")
        eps_surprises     = earnings_data.get("eps_surprises", [])
        short_ratio       = earnings_data.get("short_ratio")
        source            = earnings_data.get("source", "Unknown")

        signal_inputs = {}
        if days_to_earnings is not None:
            signal_inputs["daysToEarnings"] = min(90, days_to_earnings)
        if eps_surprises:
            signal_inputs["earningsBeat"] = round(max(-25, min(40,
                sum(eps_surprises) / len(eps_surprises))), 1)

        bull_factors, bear_factors = [], []

        if days_to_earnings is not None:
            if days_to_earnings <= 7:
                bull_factors.append(f"Earnings in {days_to_earnings} days — high catalyst potential")
            elif days_to_earnings <= 14:
                bull_factors.append(f"Earnings approaching in {days_to_earnings} days ({earnings_date_str})")
            elif days_to_earnings <= 30:
                bull_factors.append(f"Earnings in {days_to_earnings} days — monitoring period")
            else:
                bear_factors.append(f"Earnings {days_to_earnings} days away — no near-term catalyst")

        if eps_surprises:
            beats = sum(1 for s in eps_surprises if s > 0)
            total = len(eps_surprises)
            avg   = sum(eps_surprises) / total
            if beats == total:
                bull_factors.append(f"Beat estimates all {total}/{total} recent quarters (avg +{avg:.1f}%)")
            elif beats >= total * 0.75:
                bull_factors.append(f"Beat estimates {beats}/{total} recent quarters (avg {avg:+.1f}%)")
            elif beats <= total * 0.25:
                bear_factors.append(f"Missed estimates {total-beats}/{total} recent quarters (avg {avg:+.1f}%)")
            if len(eps_surprises) >= 2 and eps_surprises[-1] < 0 and eps_surprises[-2] > 0:
                bear_factors.append("Recent miss after prior beat — trend reversal risk")
            elif len(eps_surprises) >= 2 and eps_surprises[-1] > 0 and eps_surprises[-2] < 0:
                bull_factors.append("Returned to beat after prior miss — positive recovery")

        if short_ratio:
            if short_ratio > 8:
                bear_factors.append(f"Short ratio {short_ratio:.1f} — elevated short interest")
            elif short_ratio < 2:
                bull_factors.append(f"Short ratio {short_ratio:.1f} — low short interest")

        if not bull_factors:
            bull_factors.append("No negative earnings surprises in recent history")
        if not bear_factors:
            bear_factors.append("Earnings catalyst timing uncertain")

        if days_to_earnings is not None and days_to_earnings <= 14:
            summary = f"Earnings in {days_to_earnings} days ({source})"
        elif eps_surprises:
            beats   = sum(1 for s in eps_surprises if s > 0)
            summary = f"Beat {beats}/{len(eps_surprises)} recent quarters ({source})"
        else:
            summary = f"Earnings data retrieved ({source})"

        return BotResult(
            bot_name=self.name, ticker=ticker, signal_inputs=signal_inputs,
            bull_factors=bull_factors[:3], bear_factors=bear_factors[:3],
            summary=summary, confidence=0.85 if eps_surprises else 0.5,
            source=source,
            raw={"days_to_earnings": days_to_earnings, "earnings_date": earnings_date_str,
                 "eps_surprises": eps_surprises, "short_ratio": short_ratio},
        )
