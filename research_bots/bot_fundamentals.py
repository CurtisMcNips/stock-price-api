"""
Market Brain — 📈 Fundamentals Bot
─────────────────────────────────────
Data sources (priority order):
  1. FMP  — better UK/EU coverage, institutional ownership data
  2. Yahoo Finance — fallback for US stocks

Produces:
  signal_inputs.revGrowth   float (%)
  signal_inputs.debtRatio   float
  signal_inputs.shortInt    float (%)
"""

import logging
import os
from typing import Optional

import httpx

from base import ResearchBot, BotResult

log = logging.getLogger("mb.bots.fundamentals")

FMP_API_KEY   = os.environ.get("FMP_KEY", "REi5YWMduTkssRQFsNyEONemYwbbSjro")
FMP_BASE      = "https://financialmodelingprep.com/api/v3"
YAHOO_SUMMARY = "https://query2.finance.yahoo.com/v10/finance/quoteSummary/{symbol}"
YAHOO_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
}
CACHE_TTL = 14400

SECTOR_PE = {
    "Technology": 28.0, "Healthcare": 22.0, "Finance": 14.0,
    "Energy": 12.0, "Consumer": 20.0, "Industrials": 18.0,
    "Utilities": 16.0, "Real Estate": 30.0, "Materials": 16.0,
}


async def _fetch_fmp_fundamentals(ticker: str) -> Optional[dict]:
    """FMP fundamentals — better for UK and institutional ownership."""
    fmp_ticker = ticker.replace(".L", ".LSE").replace(".IL", ".LSE")
    try:
        async with httpx.AsyncClient(timeout=12) as client:
            # Key metrics
            km_r = await client.get(f"{FMP_BASE}/key-metrics-ttm/{fmp_ticker}",
                params={"apikey": FMP_API_KEY})
            # Financial growth
            gr_r = await client.get(f"{FMP_BASE}/financial-growth/{fmp_ticker}",
                params={"apikey": FMP_API_KEY, "limit": 2})
            # Income statement for margins
            is_r = await client.get(f"{FMP_BASE}/income-statement/{fmp_ticker}",
                params={"apikey": FMP_API_KEY, "limit": 2})

        km   = km_r.json()[0]   if km_r.status_code   == 200 and km_r.json()   else {}
        gr   = gr_r.json()[0]   if gr_r.status_code   == 200 and gr_r.json()   else {}
        inc  = is_r.json()      if is_r.status_code   == 200 else []

        if not km and not gr:
            return None

        rev_growth     = gr.get("revenueGrowth")          # e.g. 0.15 = 15%
        gross_margins  = km.get("grossProfitMarginTTM")
        profit_margins = km.get("netProfitMarginTTM")
        debt_to_equity = km.get("debtToEquityTTM")
        short_pct      = km.get("shortRatioTTM")
        trailing_pe    = km.get("peRatioTTM")
        forward_pe     = km.get("forwardPERatioTTM")
        roe            = km.get("roeTTM")
        current_ratio  = km.get("currentRatioTTM")

        # Revenue from income statements for trend
        rev_history = []
        for stmt in (inc[:3] if isinstance(inc, list) else []):
            rev = stmt.get("revenue")
            if rev:
                rev_history.append(rev)

        return {
            "rev_growth": rev_growth, "gross_margins": gross_margins,
            "profit_margins": profit_margins, "debt_to_equity": debt_to_equity,
            "short_pct": short_pct, "trailing_pe": trailing_pe,
            "forward_pe": forward_pe, "roe": roe, "current_ratio": current_ratio,
            "rev_history": rev_history, "source": "FMP",
        }
    except Exception as e:
        log.warning(f"FMP fundamentals failed for {ticker}: {e}")
        return None


async def _fetch_yahoo_fundamentals(ticker: str) -> Optional[dict]:
    """Yahoo Finance fundamentals fallback."""
    url    = YAHOO_SUMMARY.format(symbol=ticker)
    params = {"modules": "financialData,defaultKeyStatistics,summaryDetail,incomeStatementHistory"}
    try:
        async with httpx.AsyncClient(timeout=12) as client:
            r = await client.get(url, params=params, headers=YAHOO_HEADERS)
            if r.status_code != 200:
                return None
            data = r.json()

        result  = data.get("quoteSummary", {}).get("result", [{}])[0]
        fin     = result.get("financialData", {})
        stats   = result.get("defaultKeyStatistics", {})
        summary = result.get("summaryDetail", {})
        income  = result.get("incomeStatementHistory", {})

        def rv(d, k):
            v = d.get(k, {}); return v.get("raw") if isinstance(v, dict) else v

        rev_history = []
        for stmt in income.get("incomeStatementHistory", [])[:3]:
            rev = rv(stmt, "totalRevenue")
            if rev:
                rev_history.append(rev)

        return {
            "rev_growth":     rv(fin,     "revenueGrowth"),
            "gross_margins":  rv(fin,     "grossMargins"),
            "profit_margins": rv(fin,     "profitMargins"),
            "debt_to_equity": rv(fin,     "debtToEquity"),
            "short_pct":      rv(stats,   "shortPercentOfFloat"),
            "trailing_pe":    rv(summary, "trailingPE"),
            "forward_pe":     rv(summary, "forwardPE"),
            "roe":            rv(fin,     "returnOnEquity"),
            "current_ratio":  rv(fin,     "currentRatio"),
            "rev_history":    rev_history,
            "source":         "Yahoo Finance",
        }
    except Exception as e:
        log.warning(f"Yahoo fundamentals failed for {ticker}: {e}")
        return None


class FundamentalsBot(ResearchBot):

    @property
    def name(self) -> str:
        return "FundamentalsBot"

    @property
    def cache_ttl(self) -> int:
        return CACHE_TTL

    async def _fetch(self, ticker: str, asset_meta: dict) -> BotResult:
        asset_type = asset_meta.get("asset_type", "stock")
        if asset_type in ("crypto", "forex"):
            return self._empty_result(ticker, f"Fundamentals not applicable for {asset_type}")

        # FMP first for all tickers (better coverage), Yahoo as fallback
        data = await _fetch_fmp_fundamentals(ticker)
        if not data or data.get("rev_growth") is None:
            yahoo_data = await _fetch_yahoo_fundamentals(ticker)
            if yahoo_data:
                # Merge — fill any gaps from Yahoo
                for k, v in yahoo_data.items():
                    if not data or data.get(k) is None:
                        if not data:
                            data = yahoo_data
                            break
                        data[k] = v

        if not data:
            return self._empty_result(ticker, "No fundamental data available")

        rev_growth     = data.get("rev_growth")
        profit_margins = data.get("profit_margins")
        debt_to_equity = data.get("debt_to_equity")
        short_pct      = data.get("short_pct")
        trailing_pe    = data.get("trailing_pe")
        forward_pe     = data.get("forward_pe")
        roe            = data.get("roe")
        source         = data.get("source", "Unknown")
        sector         = asset_meta.get("sector", "")

        signal_inputs = {}
        if rev_growth is not None:
            # FMP returns as decimal (0.15), Yahoo too — convert to %
            rg = rev_growth * 100 if abs(rev_growth) < 5 else rev_growth
            signal_inputs["revGrowth"] = round(rg, 1)
        if debt_to_equity is not None:
            de = debt_to_equity / 100 if debt_to_equity > 5 else debt_to_equity
            signal_inputs["debtRatio"] = round(de, 2)
        if short_pct is not None:
            sp = short_pct * 100 if short_pct < 1 else short_pct
            signal_inputs["shortInt"] = round(sp, 1)

        bull_factors, bear_factors = [], []

        if rev_growth is not None:
            rg_pct = rev_growth * 100 if abs(rev_growth) < 5 else rev_growth
            if rg_pct > 20:
                bull_factors.append(f"Strong revenue growth +{rg_pct:.1f}% YoY")
            elif rg_pct > 8:
                bull_factors.append(f"Solid revenue growth +{rg_pct:.1f}% YoY")
            elif rg_pct > 0:
                bull_factors.append(f"Modest revenue growth +{rg_pct:.1f}% YoY")
            elif rg_pct < -10:
                bear_factors.append(f"Revenue declining {rg_pct:.1f}% YoY")
            else:
                bear_factors.append(f"Flat/declining revenue {rg_pct:+.1f}% YoY")

        if profit_margins is not None:
            pm = profit_margins * 100 if abs(profit_margins) < 2 else profit_margins
            if pm > 20:
                bull_factors.append(f"High profit margin {pm:.1f}% — strong pricing power")
            elif pm > 10:
                bull_factors.append(f"Healthy profit margin {pm:.1f}%")
            elif pm < 0:
                bear_factors.append(f"Negative profit margin {pm:.1f}% — not yet profitable")
            elif pm < 5:
                bear_factors.append(f"Thin profit margin {pm:.1f}% — limited buffer")

        if debt_to_equity is not None:
            de = debt_to_equity / 100 if debt_to_equity > 5 else debt_to_equity
            if de > 2.0:
                bear_factors.append(f"High debt-to-equity {de:.2f} — leverage risk")
            elif de > 1.0:
                bear_factors.append(f"Elevated debt-to-equity {de:.2f}")
            elif de < 0.3:
                bull_factors.append(f"Low debt-to-equity {de:.2f} — strong balance sheet")

        if short_pct is not None:
            sp = short_pct * 100 if short_pct < 1 else short_pct
            if sp > 20:
                bear_factors.append(f"Very high short interest {sp:.1f}% of float")
            elif sp > 10:
                bear_factors.append(f"Elevated short interest {sp:.1f}% of float")
            elif sp < 3:
                bull_factors.append(f"Low short interest {sp:.1f}% — little bearish conviction")

        if trailing_pe and sector in SECTOR_PE:
            avg = SECTOR_PE[sector]
            if trailing_pe < avg * 0.7:
                bull_factors.append(f"P/E {trailing_pe:.1f}x — discount to {sector} avg ({avg:.0f}x)")
            elif trailing_pe > avg * 1.5:
                bear_factors.append(f"P/E {trailing_pe:.1f}x — premium vs {sector} avg ({avg:.0f}x)")

        if roe is not None:
            roe_pct = roe * 100 if abs(roe) < 2 else roe
            if roe_pct > 20:
                bull_factors.append(f"High ROE {roe_pct:.1f}% — excellent capital efficiency")
            elif roe_pct < 0:
                bear_factors.append(f"Negative ROE {roe_pct:.1f}%")

        if not bull_factors:
            bull_factors.append("No major fundamental red flags detected")
        if not bear_factors:
            bear_factors.append("Valuation may be stretched relative to growth")

        parts = []
        if rev_growth is not None:
            rg_pct = rev_growth * 100 if abs(rev_growth) < 5 else rev_growth
            parts.append(f"Rev {rg_pct:+.1f}%")
        if profit_margins is not None:
            pm = profit_margins * 100 if abs(profit_margins) < 2 else profit_margins
            parts.append(f"Margin {pm:.1f}%")
        if short_pct is not None:
            sp = short_pct * 100 if short_pct < 1 else short_pct
            parts.append(f"Short {sp:.1f}%")
        summary = (" · ".join(parts) + f" ({source})") if parts else f"Fundamentals retrieved ({source})"

        return BotResult(
            bot_name=self.name, ticker=ticker, signal_inputs=signal_inputs,
            bull_factors=bull_factors[:3], bear_factors=bear_factors[:3],
            summary=summary, confidence=0.85 if rev_growth is not None else 0.5,
            source=source,
            raw={"rev_growth": rev_growth, "profit_margins": profit_margins,
                 "debt_to_equity": debt_to_equity, "short_pct": short_pct,
                 "trailing_pe": trailing_pe, "roe": roe},
        )
