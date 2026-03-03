"""
Market Brain — 🎯 Analyst Bot
───────────────────────────────
Data sources (priority order):
  1. FMP — upgrade/downgrade history, individual analyst ratings
  2. Yahoo Finance — fallback consensus + price targets

Produces:
  signal_inputs.sentiment  (mild, from consensus direction)

FIX APPLIED:
  Original skipped asset_type "commodity" and "crypto" — but many commodity-adjacent
  stocks (e.g. FCX, NEM, VALE) have extensive analyst coverage and are tagged as
  "commodity" in the asset meta. Now only skips pure derivatives (futures, forex pairs,
  crypto tokens, index ETFs) — same pattern as EarningsBot fix.

  Also: FMP ticker format for UK stocks was not always correct. Added .LSE → normal
  Yahoo fallback path for UK tickers.

  Yahoo recommendationTrend returning empty for non-US tickers is now handled
  gracefully — falls back to financialData targetMeanPrice comparison only.
"""

import logging
import os
from typing import Optional

import httpx

from base import ResearchBot, BotResult

log = logging.getLogger("mb.bots.analyst")

FMP_API_KEY   = os.environ.get("FMP_API_KEY", "REi5YWMduTkssRQFsNyEONemYwbbSjro")
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


def _should_skip(ticker: str, asset_type: str) -> Optional[str]:
    """Only skip pure non-equity instruments."""
    if ticker.endswith("=F"):
        return "Futures contract — no analyst coverage"
    if ticker.endswith("=X"):
        return "Forex pair — no analyst coverage"
    if ticker.endswith("-USD") and asset_type == "crypto":
        return "Crypto token — no analyst ratings"
    if asset_type == "etf" and ticker in (
        "SPY", "QQQ", "XLE", "XLK", "XLF", "GLD", "SLV", "USO", "BNO",
        "ITA", "XLB", "BDRY", "XLU", "XLRE", "MOO", "UUP", "XLY", "XLI",
    ):
        return "Index ETF — no analyst ratings"
    return None


async def _fetch_fmp_analyst(ticker: str) -> Optional[dict]:
    """FMP analyst data — includes upgrade/downgrade history."""
    # FMP uses .LSE suffix for UK stocks
    fmp_ticker = ticker.replace(".L", ".LSE").replace(".IL", ".LSE")
    try:
        async with httpx.AsyncClient(timeout=12) as client:
            est_r = await client.get(
                f"{FMP_BASE}/analyst-stock-recommendations/{fmp_ticker}",
                params={"apikey": FMP_API_KEY, "limit": 10},
            )
            pt_r  = await client.get(
                f"{FMP_BASE}/price-target/{fmp_ticker}",
                params={"apikey": FMP_API_KEY},
            )
            ud_r  = await client.get(
                f"{FMP_BASE}/upgrades-downgrades/{fmp_ticker}",
                params={"apikey": FMP_API_KEY, "limit": 5},
            )

        est_data = est_r.json() if est_r.status_code == 200 else []
        pt_data  = pt_r.json()  if pt_r.status_code  == 200 else []
        ud_data  = ud_r.json()  if ud_r.status_code  == 200 else []

        # FMP returns {"Error Message": "..."} on bad key or unknown ticker
        if isinstance(est_data, dict) or isinstance(pt_data, dict):
            est_data = [] if isinstance(est_data, dict) else est_data
            pt_data  = [] if isinstance(pt_data,  dict) else pt_data

        if not est_data and not pt_data:
            return None

        buy = hold = sell = 0
        for rec in est_data[:10]:
            buy  += (rec.get("analystRatingsStrongBuy", 0) + rec.get("analystRatingsBuy", 0))
            hold += rec.get("analystRatingsHold", 0)
            sell += (rec.get("analystRatingsSell", 0) + rec.get("analystRatingsStrongSell", 0))
        total = buy + hold + sell

        targets     = [pt.get("priceTarget") for pt in pt_data[:5] if pt.get("priceTarget")]
        target_mean = sum(targets) / len(targets) if targets else None

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
    """Yahoo Finance analyst data."""
    url    = YAHOO_SUMMARY.format(symbol=ticker)
    params = {"modules": "financialData,recommendationTrend,defaultKeyStatistics,summaryDetail"}
    try:
        async with httpx.AsyncClient(timeout=12) as client:
            r = await client.get(url, params=params, headers=YAHOO_HEADERS)
            if r.status_code != 200:
                return None
            data = r.json()

        result = data.get("quoteSummary", {}).get("result", [{}])[0]
        if not result:
            return None

        fin   = result.get("financialData", {})
        rec   = result.get("recommendationTrend", {})
        stats = result.get("defaultKeyStatistics", {})
        summ  = result.get("summaryDetail", {})

        def rv(d, k):
            v = d.get(k, {}); return v.get("raw") if isinstance(v, dict) else v

        # Recommendation trend — may be empty for non-US tickers
        trend   = rec.get("trend", [{}])
        current = trend[0] if trend else {}
        sb  = current.get("strongBuy",  0) or 0
        b   = current.get("buy",        0) or 0
        h   = current.get("hold",       0) or 0
        s   = current.get("sell",       0) or 0
        ss  = current.get("strongSell", 0) or 0
        total = sb + b + h + s + ss

        target_mean    = rv(fin, "targetMeanPrice")
        target_high    = rv(fin, "targetHighPrice")
        target_low     = rv(fin, "targetLowPrice")
        current_price  = rv(fin, "currentPrice")
        num_analysts   = rv(fin, "numberOfAnalystOpinions") or total

        # If no recommendation trend but we have a price target, still useful
        if total == 0 and not target_mean:
            return None

        return {
            "buy":           sb + b,
            "hold":          h,
            "sell":          s + ss,
            "total":         total,
            "target_mean":   target_mean,
            "target_high":   target_high,
            "target_low":    target_low,
            "current_price": current_price,
            "num_analysts":  num_analysts,
            "trailing_pe":   rv(summ, "trailingPE"),
            "forward_pe":    rv(summ, "forwardPE"),
            "upgrades":      [],
            "downgrades":    [],
            "source":        "Yahoo Finance",
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

        skip_reason = _should_skip(ticker, asset_type)
        if skip_reason:
            return self._empty_result(ticker, skip_reason)

        # FMP first (better for UK/EU + upgrades), Yahoo fallback
        data = await _fetch_fmp_analyst(ticker)
        if not data or (data.get("total", 0) == 0 and not data.get("target_mean")):
            data = await _fetch_yahoo_analyst(ticker)

        if not data:
            return self._empty_result(ticker, "No analyst data available from any source")

        buy           = data.get("buy",           0) or 0
        hold          = data.get("hold",          0) or 0
        sell          = data.get("sell",          0) or 0
        total         = data.get("total",         0) or 0
        target_mean   = data.get("target_mean")
        current_price = data.get("current_price")
        upgrades      = data.get("upgrades",   [])
        downgrades    = data.get("downgrades", [])
        trailing_pe   = data.get("trailing_pe")
        forward_pe    = data.get("forward_pe")
        num_analysts  = data.get("num_analysts", total) or total
        source        = data.get("source", "Unknown")

        # ── Consensus score ───────────────────────────────────
        if total > 0:
            mean_score = (buy * 1.5 + hold * 3 + sell * 4.5) / total
            consensus_label, consensus_signal = _mean_rec_label(mean_score)
        else:
            consensus_label, consensus_signal = "Hold", 0.5

        # ── Price target upside ───────────────────────────────
        upside_pct = None
        if current_price and target_mean and current_price > 0:
            upside_pct = round((target_mean - current_price) / current_price * 100, 1)

        # ── Build factors ─────────────────────────────────────
        bull_factors = []
        bear_factors = []

        # Consensus
        if total > 0:
            analyst_str = f"{num_analysts} analyst{'s' if num_analysts != 1 else ''}"
            if consensus_label in ("Strong Buy", "Buy", "Moderate Buy"):
                bull_factors.append(f"Analyst consensus: {consensus_label} ({buy}/{total} bullish, {analyst_str})")
            elif consensus_label in ("Sell", "Moderate Sell"):
                bear_factors.append(f"Analyst consensus: {consensus_label} ({sell}/{total} bearish, {analyst_str})")
            else:
                bull_factors.append(f"Analyst consensus: {consensus_label} — {analyst_str} covering")
        elif target_mean:
            # Only price target, no rating count
            bull_factors.append(f"Analyst price target available: {target_mean:.2f}")

        # Price target
        if upside_pct is not None:
            if upside_pct > 25:
                bull_factors.append(f"Avg target {target_mean:.2f} — {upside_pct:.1f}% upside from current")
            elif upside_pct > 10:
                bull_factors.append(f"Avg target implies {upside_pct:.1f}% upside")
            elif upside_pct < -10:
                bear_factors.append(f"Avg target {target_mean:.2f} — {abs(upside_pct):.1f}% downside implied")
            else:
                bull_factors.append(f"Price target approx in line with current price ({upside_pct:+.1f}%)")

        # Upgrades / downgrades
        if upgrades:
            firms = ", ".join(u.get("gradingCompany", "Analyst") for u in upgrades[:2])
            bull_factors.append(f"Recent upgrade(s): {firms}")
        if downgrades:
            firms = ", ".join(d.get("gradingCompany", "Analyst") for d in downgrades[:2])
            bear_factors.append(f"Recent downgrade(s): {firms}")

        # PE expansion/contraction
        if trailing_pe and forward_pe:
            try:
                if forward_pe < trailing_pe * 0.85:
                    bull_factors.append(
                        f"Forward P/E {forward_pe:.1f}x < trailing {trailing_pe:.1f}x — earnings growth priced in"
                    )
                elif forward_pe > trailing_pe * 1.15:
                    bear_factors.append(
                        f"Forward P/E {forward_pe:.1f}x > trailing {trailing_pe:.1f}x — earnings expected to fall"
                    )
            except (TypeError, ZeroDivisionError):
                pass

        if not bull_factors:
            bull_factors.append(f"No dominant sell ratings found — {total or 0} analysts on record")
        if not bear_factors:
            bear_factors.append("Analyst price target upside may be limited at current levels")

        # ── Confidence ────────────────────────────────────────
        confidence = 0.8 if total >= 3 else (0.6 if total >= 1 or upside_pct is not None else 0.35)

        # ── Summary ───────────────────────────────────────────
        if upside_pct is not None and total > 0:
            summary = f"{consensus_label}, {upside_pct:+.1f}% target upside ({source})"
        elif upside_pct is not None:
            summary = f"Analyst target implies {upside_pct:+.1f}% upside ({source})"
        elif total > 0:
            summary = f"{consensus_label} — {total} analysts ({source})"
        else:
            summary = f"No analyst coverage found ({source})"

        # ── signal_inputs ─────────────────────────────────────
        signal_inputs = {}
        if total >= 3:
            signal_inputs["sentiment"] = round((consensus_signal - 0.5) * 0.6, 3)
        elif upside_pct is not None:
            # Upside-based signal when no ratings available
            signal_inputs["sentiment"] = round(max(-0.3, min(0.3, upside_pct / 100)), 3)

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
                "consensus":    consensus_label,
                "total":        total,
                "buy":          buy,
                "hold":         hold,
                "sell":         sell,
                "target_mean":  target_mean,
                "upside_pct":   upside_pct,
                "num_analysts": num_analysts,
            },
        )
