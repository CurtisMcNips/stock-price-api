"""
Market Brain — 🎯 Analyst Bot
───────────────────────────────
Data sources (priority order):
  1. FMP — upgrade/downgrade history, individual analyst ratings
  2. Yahoo Finance — fallback consensus + price targets

Produces:
  signal_inputs.sentiment  (mild, from consensus direction)
  Bull/bear factors: consensus rating, price target upside, upgrade history
"""

import logging
import os
from typing import Optional

import httpx

from base import ResearchBot, BotResult

log = logging.getLogger("mb.bots.analyst")

FMP_API_KEY   = os.environ.get("FMP_KEY", "REi5YWMduTkssRQFsNyEONemYwbbSjro")
FMP_BASE      = "https://financialmodelingprep.com/api/v3"
YAHOO_SUMMARY = "https://query2.finance.yahoo.com/v10/finance/quoteSummary/{symbol}"
YAHOO_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
}
CACHE_TTL = 14400

MEAN_REC_LABELS = {
    (1.0, 1.5): ("Strong Buy",    1.0),
    (1.5, 2.0): ("Buy",           0.75),
    (2.0, 2.5): ("Moderate Buy",  0.6),
    (2.5, 3.0): ("Hold",          0.5),
    (3.0, 3.5): ("Moderate Sell", 0.4),
    (3.5, 5.0): ("Sell",          0.2),
}

def _mean_rec_label(score: float) -> tuple:
    for (low, high), (label, sig) in MEAN_REC_LABELS.items():
        if low <= score < high:
            return label, sig
    return "Hold", 0.5


async def _fetch_fmp_analyst(ticker: str) -> Optional[dict]:
    """FMP analyst data — includes upgrade/downgrade history."""
    fmp_ticker = ticker.replace(".L", ".LSE").replace(".IL", ".LSE")
    try:
        async with httpx.AsyncClient(timeout=12) as client:
            # Analyst estimates
            est_r = await client.get(f"{FMP_BASE}/analyst-stock-recommendations/{fmp_ticker}",
                params={"apikey": FMP_API_KEY, "limit": 10})
            # Price targets
            pt_r  = await client.get(f"{FMP_BASE}/price-target/{fmp_ticker}",
                params={"apikey": FMP_API_KEY})
            # Upgrades/downgrades
            ud_r  = await client.get(f"{FMP_BASE}/upgrades-downgrades/{fmp_ticker}",
                params={"apikey": FMP_API_KEY, "limit": 5})

        est_data = est_r.json() if est_r.status_code == 200 else []
        pt_data  = pt_r.json()  if pt_r.status_code  == 200 else []
        ud_data  = ud_r.json()  if ud_r.status_code  == 200 else []

        if not est_data and not pt_data:
            return None

        # Consensus from recent recommendations
        buy = hold = sell = 0
        for rec in est_data[:10]:
            rating = (rec.get("analystRatingsStrongBuy", 0) +
                      rec.get("analystRatingsBuy", 0))
            hold  += rec.get("analystRatingsHold", 0)
            sell  += (rec.get("analystRatingsSell", 0) +
                      rec.get("analystRatingsStrongSell", 0))
            buy   += rating
        total = buy + hold + sell

        # Price targets
        targets     = [pt.get("priceTarget") for pt in pt_data[:5] if pt.get("priceTarget")]
        target_mean = sum(targets) / len(targets) if targets else None

        # Recent upgrades/downgrades
        upgrades   = [u for u in ud_data if "upgrade"   in (u.get("action", "") or "").lower()]
        downgrades = [u for u in ud_data if "downgrade" in (u.get("action", "") or "").lower()]

        return {
            "buy": buy, "hold": hold, "sell": sell, "total": total,
            "target_mean": target_mean, "targets": targets,
            "upgrades": upgrades[:2], "downgrades": downgrades[:2],
            "source": "FMP",
        }
    except Exception as e:
        log.warning(f"FMP analyst failed for {ticker}: {e}")
        return None


async def _fetch_yahoo_analyst(ticker: str) -> Optional[dict]:
    """Yahoo Finance analyst fallback."""
    url    = YAHOO_SUMMARY.format(symbol=ticker)
    params = {"modules": "financialData,recommendationTrend,defaultKeyStatistics,summaryDetail"}
    try:
        async with httpx.AsyncClient(timeout=12) as client:
            r = await client.get(url, params=params, headers=YAHOO_HEADERS)
            if r.status_code != 200:
                return None
            data = r.json()

        result = data.get("quoteSummary", {}).get("result", [{}])[0]
        fin    = result.get("financialData", {})
        rec    = result.get("recommendationTrend", {})
        stats  = result.get("defaultKeyStatistics", {})
        summ   = result.get("summaryDetail", {})

        def rv(d, k):
            v = d.get(k, {}); return v.get("raw") if isinstance(v, dict) else v

        trend   = rec.get("trend", [{}])
        current = trend[0] if trend else {}
        sb  = current.get("strongBuy",  0)
        b   = current.get("buy",        0)
        h   = current.get("hold",       0)
        s   = current.get("sell",       0)
        ss  = current.get("strongSell", 0)
        total = sb + b + h + s + ss

        return {
            "buy": sb + b, "hold": h, "sell": s + ss, "total": total,
            "target_mean": rv(fin, "targetMeanPrice"),
            "target_high": rv(fin, "targetHighPrice"),
            "target_low":  rv(fin, "targetLowPrice"),
            "current_price": rv(fin, "currentPrice"),
            "num_analysts": rv(fin, "numberOfAnalystOpinions"),
            "trailing_pe": rv(summ, "trailingPE"),
            "forward_pe":  rv(summ, "forwardPE"),
            "upgrades": [], "downgrades": [],
            "source": "Yahoo Finance",
        }
    except Exception as e:
        log.warning(f"Yahoo analyst failed for {ticker}: {e}")
        return None


class AnalystBot(ResearchBot):

    @property
    def name(self) -> str:
        return "AnalystBot"

    @property
    def cache_ttl(self) -> int:
        return CACHE_TTL

    async def _fetch(self, ticker: str, asset_meta: dict) -> BotResult:
        asset_type = asset_meta.get("asset_type", "stock")
        if asset_type in ("crypto", "forex"):
            return self._empty_result(ticker, f"Analyst ratings not applicable for {asset_type}")

        # FMP first, Yahoo fallback
        data = await _fetch_fmp_analyst(ticker)
        if not data or data.get("total", 0) == 0:
            data = await _fetch_yahoo_analyst(ticker)

        if not data:
            return self._empty_result(ticker, "No analyst data available")

        buy   = data.get("buy",   0)
        hold  = data.get("hold",  0)
        sell  = data.get("sell",  0)
        total = data.get("total", 0)
        target_mean   = data.get("target_mean")
        current_price = data.get("current_price")
        upgrades      = data.get("upgrades",   [])
        downgrades    = data.get("downgrades", [])
        trailing_pe   = data.get("trailing_pe")
        forward_pe    = data.get("forward_pe")
        source        = data.get("source", "Unknown")

        # Consensus score
        if total > 0:
            mean_score = (buy * 1.5 + hold * 3 + sell * 4.5) / total
            consensus_label, consensus_signal = _mean_rec_label(mean_score)
        else:
            consensus_label, consensus_signal = "Hold", 0.5

        upside_pct = None
        if current_price and target_mean and current_price > 0:
            upside_pct = (target_mean - current_price) / current_price * 100

        bull_factors, bear_factors = [], []

        # Consensus
        if total > 0:
            if consensus_label in ("Strong Buy", "Buy", "Moderate Buy"):
                bull_factors.append(f"Analyst consensus: {consensus_label} ({buy}/{total} analysts bullish)")
            elif consensus_label in ("Sell", "Moderate Sell"):
                bear_factors.append(f"Analyst consensus: {consensus_label} ({sell}/{total} bearish)")
            else:
                bull_factors.append(f"Analyst consensus: {consensus_label} — {total} analysts covering")

        # Price target
        if upside_pct is not None:
            if upside_pct > 25:
                bull_factors.append(f"Analyst avg target {target_mean:.2f} — {upside_pct:.1f}% upside")
            elif upside_pct > 10:
                bull_factors.append(f"Analyst avg target implies {upside_pct:.1f}% upside potential")
            elif upside_pct < -10:
                bear_factors.append(f"Analyst avg target {target_mean:.2f} — {abs(upside_pct):.1f}% downside implied")

        # Recent upgrades/downgrades (FMP advantage)
        if upgrades:
            firms = ", ".join(u.get("gradingCompany", "Analyst") for u in upgrades[:2])
            bull_factors.append(f"Recent upgrade(s): {firms}")
        if downgrades:
            firms = ", ".join(d.get("gradingCompany", "Analyst") for d in downgrades[:2])
            bear_factors.append(f"Recent downgrade(s): {firms}")

        # PE trend
        if trailing_pe and forward_pe:
            if forward_pe < trailing_pe * 0.85:
                bull_factors.append(f"Forward P/E {forward_pe:.1f}x below trailing {trailing_pe:.1f}x — earnings growth expected")
            elif forward_pe > trailing_pe * 1.15:
                bear_factors.append(f"Forward P/E {forward_pe:.1f}x above trailing — earnings expected to decline")

        if not bull_factors:
            bull_factors.append(f"No dominant sell ratings — {total} analysts covering")
        if not bear_factors:
            bear_factors.append("Price target upside may be limited at current levels")

        if upside_pct is not None:
            summary = f"{consensus_label} consensus, {upside_pct:+.1f}% price target ({source})"
        elif total > 0:
            summary = f"{consensus_label} — {total} analysts ({source})"
        else:
            summary = "No analyst coverage found"

        signal_inputs = {}
        if total >= 3:
            signal_inputs["sentiment"] = round((consensus_signal - 0.5) * 0.6, 3)

        return BotResult(
            bot_name=self.name, ticker=ticker, signal_inputs=signal_inputs,
            bull_factors=bull_factors[:3], bear_factors=bear_factors[:3],
            summary=summary, confidence=0.8 if total >= 3 else 0.4,
            source=source,
            raw={"consensus": consensus_label, "total": total, "buy": buy,
                 "hold": hold, "sell": sell, "target_mean": target_mean,
                 "upside_pct": round(upside_pct, 1) if upside_pct else None},
        )
