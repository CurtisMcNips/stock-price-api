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

FIX APPLIED:
  Original asset_type guard excluded "commodity", "etf", "forex" — but many
  assets in the system are tagged as these types even though they have
  underlying companies with earnings (e.g. COIN is tagged crypto but has earnings,
  shipping stocks may be tagged as commodity-adjacent).

  Changes:
  - Only hard-skip pure derivatives: futures (=F suffix), spot forex (=X suffix),
    ETF wrappers (explicit etf type), crypto tokens
  - Stocks, ADRs, shipping companies, defence primes all get fetched
  - Missing earnings date is now a soft signal (no penalty), not an error exit
  - Yahoo calendarEvents earningsDate empty list now handled gracefully
  - EPS surprise history alone (without upcoming date) is still useful output
"""

import logging
import os
from datetime import datetime, timezone
from typing import Optional

import httpx

from base import ResearchBot, BotResult

log = logging.getLogger("mb.bots.earnings")

FMP_API_KEY   = os.environ.get("FMP_API_KEY", "REi5YWMduTkssRQFsNyEONemYwbbSjro")
AV_API_KEY    = os.environ.get("AV_API_KEY",  "KH9A652VHUDYN4SK")

FMP_BASE      = "https://financialmodelingprep.com/api/v3"
AV_BASE       = "https://www.alphavantage.co/query"
YAHOO_SUMMARY = "https://query2.finance.yahoo.com/v10/finance/quoteSummary/{symbol}"
YAHOO_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
}
CACHE_TTL = 14400   # 4 hours


def _should_skip(ticker: str, asset_type: str) -> Optional[str]:
    """
    Return a skip reason string if this asset genuinely has no earnings,
    or None if it should be fetched.

    Only hard-skip:
      - Futures contracts (=F suffix): no corporate earnings
      - Spot forex (=X suffix): no earnings
      - Pure ETF wrappers (asset_type=etf AND ticker doesn't look like a company)
      - Pure crypto tokens (BTC-USD, ETH-USD style)
    """
    # Pure futures
    if ticker.endswith("=F"):
        return "Futures contract — no corporate earnings"
    # Spot forex
    if ticker.endswith("=X"):
        return "Forex pair — no earnings"
    # Crypto tokens
    if ticker.endswith("-USD") and asset_type == "crypto":
        return "Crypto token — no earnings"
    # Only skip ETFs if explicitly tagged AND not a known company ticker
    if asset_type == "etf" and ticker in (
        "SPY", "QQQ", "XLE", "XLK", "XLF", "GLD", "SLV", "USO", "BNO",
        "ITA", "XLB", "BDRY", "XLU", "XLRE", "MOO", "UUP", "XLY", "XLI",
    ):
        return "Index ETF — no earnings"
    # Everything else: attempt fetch
    return None


def _is_uk_ticker(ticker: str) -> bool:
    return ticker.endswith(".L") or ticker.endswith(".IL")


async def _fetch_fmp_earnings(ticker: str) -> Optional[dict]:
    """FMP — best for UK/EU tickers. Returns parsed earnings dict."""
    fmp_ticker = ticker.replace(".L", ".LSE").replace(".IL", ".LSE")
    try:
        async with httpx.AsyncClient(timeout=12) as client:
            cal_r  = await client.get(
                f"{FMP_BASE}/earning_calendar",
                params={"symbol": fmp_ticker, "apikey": FMP_API_KEY},
            )
            hist_r = await client.get(
                f"{FMP_BASE}/earnings-surprises/{fmp_ticker}",
                params={"apikey": FMP_API_KEY},
            )

        cal_data  = cal_r.json()  if cal_r.status_code  == 200 else []
        hist_data = hist_r.json() if hist_r.status_code == 200 else []

        # If both endpoints returned empty, no data available
        if not cal_data and not hist_data:
            return None

        days_to_earnings  = None
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

        # Only return if we got at least one useful data point
        if days_to_earnings is None and not eps_surprises:
            return None

        return {
            "days_to_earnings": days_to_earnings,
            "earnings_date":    earnings_date_str,
            "eps_surprises":    eps_surprises,
            "source":           "FMP",
        }
    except Exception as e:
        log.warning(f"FMP earnings failed for {ticker}: {e}")
        return None


async def _fetch_yahoo_earnings(ticker: str) -> Optional[dict]:
    """Yahoo Finance — primary for US stocks."""
    try:
        async with httpx.AsyncClient(timeout=12) as client:
            r = await client.get(
                YAHOO_SUMMARY.format(symbol=ticker),
                params={"modules": "calendarEvents,earningsHistory,defaultKeyStatistics"},
                headers=YAHOO_HEADERS,
            )
            if r.status_code != 200:
                return None
            data = r.json()

        result  = data.get("quoteSummary", {}).get("result", [{}])[0]
        cal     = result.get("calendarEvents", {})
        history = result.get("earningsHistory", {})
        stats   = result.get("defaultKeyStatistics", {})

        def rv(d, k):
            v = d.get(k, {}); return v.get("raw") if isinstance(v, dict) else v

        # ── Upcoming earnings date ────────────────────────────
        days_to_earnings  = None
        earnings_date_str = None
        dates = cal.get("earnings", {}).get("earningsDate", [])

        # dates can be empty list, a list of {raw: timestamp} dicts, or None
        if dates:
            for date_entry in dates:
                ts = None
                if isinstance(date_entry, dict):
                    ts = date_entry.get("raw")
                elif isinstance(date_entry, (int, float)):
                    ts = date_entry
                if ts:
                    try:
                        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
                        delta = (dt - datetime.now(tz=timezone.utc)).days
                        if delta >= 0:
                            days_to_earnings  = delta
                            earnings_date_str = dt.strftime("%d %b %Y")
                            break
                    except Exception:
                        continue

        # ── EPS surprise history ──────────────────────────────
        eps_surprises = []
        for q in history.get("history", [])[-4:]:
            actual   = rv(q, "epsActual")
            estimate = rv(q, "epsEstimate")
            if actual is not None and estimate is not None and estimate != 0:
                try:
                    surprise = round(((actual - estimate) / abs(estimate)) * 100, 1)
                    eps_surprises.append(surprise)
                except Exception:
                    continue

        # If no data at all, return None so caller can try next source
        if days_to_earnings is None and not eps_surprises:
            return None

        return {
            "days_to_earnings": days_to_earnings,
            "earnings_date":    earnings_date_str,
            "eps_surprises":    eps_surprises,
            "short_ratio":      rv(stats, "shortRatio"),
            "source":           "Yahoo Finance",
        }
    except Exception as e:
        log.warning(f"Yahoo earnings failed for {ticker}: {e}")
        return None


async def _fetch_av_earnings(ticker: str) -> Optional[dict]:
    """Alpha Vantage fallback — US stocks."""
    try:
        async with httpx.AsyncClient(timeout=12) as client:
            r = await client.get(AV_BASE, params={
                "function": "EARNINGS",
                "symbol":   ticker,
                "apikey":   AV_API_KEY,
            })
            if r.status_code != 200:
                return None
            data = r.json()

        quarterly = data.get("quarterlyEarnings", [])
        if not quarterly:
            return None

        eps_surprises = []
        for q in quarterly[:4]:
            try:
                pct = q.get("surprisePercentage")
                if pct is not None and pct != "None":
                    eps_surprises.append(round(float(pct), 1))
            except (ValueError, TypeError):
                continue

        if not eps_surprises:
            return None

        return {
            "days_to_earnings": None,
            "earnings_date":    None,
            "eps_surprises":    eps_surprises,
            "source":           "Alpha Vantage",
        }
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

        # Targeted skip — only pure non-earnings instruments
        skip_reason = _should_skip(ticker, asset_type)
        if skip_reason:
            return self._empty_result(ticker, skip_reason)

        # ── Source priority chain ─────────────────────────────
        earnings_data = None

        if _is_uk_ticker(ticker):
            # UK: FMP first (better LSE coverage), then Yahoo
            earnings_data = await _fetch_fmp_earnings(ticker)
            if not earnings_data:
                earnings_data = await _fetch_yahoo_earnings(ticker)
        else:
            # US/other: Yahoo first, then enrich with Alpha Vantage if no surprises
            earnings_data = await _fetch_yahoo_earnings(ticker)

            if not earnings_data or not earnings_data.get("eps_surprises"):
                av = await _fetch_av_earnings(ticker)
                if av and av.get("eps_surprises"):
                    if earnings_data:
                        earnings_data["eps_surprises"] = av["eps_surprises"]
                        earnings_data["source"] = "Yahoo + Alpha Vantage"
                    else:
                        earnings_data = av

            # Also try FMP as final fallback for unknown/missing
            if not earnings_data:
                earnings_data = await _fetch_fmp_earnings(ticker)

        if not earnings_data:
            return self._empty_result(ticker, "No earnings data available from any source")

        # ── Build signal ──────────────────────────────────────
        days_to_earnings  = earnings_data.get("days_to_earnings")
        earnings_date_str = earnings_data.get("earnings_date")
        eps_surprises     = earnings_data.get("eps_surprises", [])
        short_ratio       = earnings_data.get("short_ratio")
        source            = earnings_data.get("source", "Unknown")

        signal_inputs = {}
        if days_to_earnings is not None:
            signal_inputs["daysToEarnings"] = min(90, days_to_earnings)
        if eps_surprises:
            avg_surprise = sum(eps_surprises) / len(eps_surprises)
            signal_inputs["earningsBeat"] = round(max(-25, min(40, avg_surprise)), 1)

        # ── Factors ───────────────────────────────────────────
        bull_factors = []
        bear_factors = []

        # Earnings proximity
        if days_to_earnings is not None:
            if days_to_earnings <= 7:
                bull_factors.append(f"Earnings in {days_to_earnings} days — imminent catalyst")
            elif days_to_earnings <= 14:
                bull_factors.append(f"Earnings in {days_to_earnings} days ({earnings_date_str})")
            elif days_to_earnings <= 30:
                bull_factors.append(f"Earnings approaching — {days_to_earnings} days ({earnings_date_str})")
            else:
                bear_factors.append(f"Earnings {days_to_earnings} days away — no near-term date catalyst")
        else:
            # No upcoming date found — neutral, not a negative
            bull_factors.append("No upcoming earnings date confirmed yet")

        # EPS surprise history
        if eps_surprises:
            beats = sum(1 for s in eps_surprises if s > 0)
            total = len(eps_surprises)
            avg   = sum(eps_surprises) / total

            if beats == total:
                bull_factors.append(f"Beat estimates all {total}/{total} recent quarters (avg {avg:+.1f}%)")
            elif beats >= total * 0.75:
                bull_factors.append(f"Beat estimates {beats}/{total} recent quarters (avg {avg:+.1f}%)")
            elif beats <= total * 0.25:
                bear_factors.append(f"Missed estimates {total - beats}/{total} recent quarters (avg {avg:+.1f}%)")
            else:
                bull_factors.append(f"Mixed earnings record — {beats}/{total} beats (avg {avg:+.1f}%)")

            # Trend reversal signals
            if len(eps_surprises) >= 2:
                if eps_surprises[-1] < 0 and eps_surprises[-2] > 0:
                    bear_factors.append("Recent miss after prior beat — trend reversal risk")
                elif eps_surprises[-1] > 0 and eps_surprises[-2] < 0:
                    bull_factors.append("Beat after prior miss — positive earnings recovery")

        # Short interest
        if short_ratio:
            if short_ratio > 8:
                bear_factors.append(f"Short ratio {short_ratio:.1f} — elevated short interest")
            elif short_ratio < 2:
                bull_factors.append(f"Short ratio {short_ratio:.1f} — minimal short interest")

        if not bull_factors:
            bull_factors.append("No negative earnings surprises in recent history")
        if not bear_factors:
            bear_factors.append("Earnings catalyst timing uncertain — monitoring")

        # ── Confidence ────────────────────────────────────────
        confidence = 0.5
        if eps_surprises and days_to_earnings is not None:
            confidence = 0.90
        elif eps_surprises:
            confidence = 0.75
        elif days_to_earnings is not None:
            confidence = 0.65

        # ── Summary ───────────────────────────────────────────
        if days_to_earnings is not None and days_to_earnings <= 14:
            summary = f"Earnings in {days_to_earnings} days ({earnings_date_str}) — {source}"
        elif eps_surprises:
            beats   = sum(1 for s in eps_surprises if s > 0)
            avg     = sum(eps_surprises) / len(eps_surprises)
            summary = f"Beat {beats}/{len(eps_surprises)} recent qtrs (avg {avg:+.1f}%) — {source}"
        else:
            summary = f"Earnings data retrieved — {source}"

        return BotResult(
            bot_name=self.name,
            ticker=ticker,
            signal_inputs=signal_inputs,
            bull_factors=bull_factors[:3],
            bear_factors=bear_factors[:3],
            summary=summary,
            confidence=confidence,
            source=source,
            raw={
                "days_to_earnings": days_to_earnings,
                "earnings_date":    earnings_date_str,
                "eps_surprises":    eps_surprises,
                "short_ratio":      short_ratio,
            },
        )
