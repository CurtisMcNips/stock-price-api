"""
Market Brain — Priority Tiers
───────────────────────────────
Defines which assets get swept at which frequency.

Tier 1 — every 4 hours (watchlist, high-volume, crypto majors, indices)
Tier 2 — every 12 hours (top 500, ETFs, sector leaders)
Tier 3 — weekly or on-demand (illiquid, penny stocks, rarely viewed)

API budget estimate (per day, Tier 1 + 2 only):
  - GNews:         ~50 calls  (top 50 by watchlist frequency, 1×/day)
  - FMP:           ~180 calls (60 assets × 3 calls each, 1×/day)
  - Alpha Vantage: ~10 calls  (fallback only)
  - Polygon:       ~180 calls (60 assets × 3×/day)
  - FRED:          ~15 calls  (macro data, shared across all assets)
  - Yahoo:         ~200 calls (fallback, generous limits)
"""

import logging
from typing import Dict, List, Optional, Set

log = logging.getLogger("mb.tiers")

# ── Tier 1 — Always-on, high value ───────────────────────────
# Swept every 4 hours. Max 60 assets to stay within API budgets.
TIER1_STATIC: List[str] = [
    # US Mega Cap
    "NVDA", "AAPL", "MSFT", "GOOGL", "META", "AMZN", "TSLA", "AMD",
    # US Finance/Bank
    "JPM", "BAC", "GS", "MS",
    # US High-Growth
    "CRWD", "DDOG", "PLTR", "SOFI", "COIN", "HOOD", "MSTR",
    # UK/EU Blue Chips
    "SHEL.L", "AZN.L", "HSBA.L", "BP.L", "RIO.L", "GSK.L", "BAE.L",
    # European ADRs
    "ASML", "NVO", "SAP", "TSM",
    # Crypto
    "BTC-USD", "ETH-USD", "SOL-USD", "XRP-USD", "BNB-USD",
    "DOGE-USD", "ADA-USD", "AVAX-USD", "MATIC-USD", "DOT-USD",
    # Indices/ETFs
    "SPY", "QQQ", "GLD",
    # Volatile retail favourites
    "SOUN", "ASTS", "IONQ", "RIVN", "UPST", "GME", "AMC",
]

# ── Tier 2 — Daily sweep ──────────────────────────────────────
# Swept twice per day. Broader coverage.
TIER2_STATIC: List[str] = [
    # S&P 500 blue chips
    "JNJ", "PG", "KO", "PEP", "WMT", "COST", "HD", "LOW",
    "CVX", "XOM", "COP", "SLB", "HAL",
    "UNH", "LLY", "MRK", "PFE", "ABBV", "BMY", "AMGN",
    "V", "MA", "PYPL", "SQ", "AFRM",
    "NFLX", "DIS", "CMCSA", "T", "VZ",
    "ORCL", "CRM", "ADBE", "INTU", "NOW", "SNOW", "PLTR",
    "INTC", "AVGO", "QCOM", "MU", "TXN", "AMAT", "KLAC", "LRCX",
    "LMT", "RTX", "NOC", "GD", "BA",
    "CAT", "DE", "HON", "EMR", "ETN",
    "SHOP", "UBER", "LYFT", "ABNB", "BKNG", "EXPE",
    # More crypto
    "LTC-USD", "LINK-USD", "UNI-USD", "AAVE-USD",
    # More UK
    "ULVR.L", "DGE.L", "BATS.L", "VOD.L", "LLOY.L", "BARC.L",
    "BT.L", "MKS.L", "TSCO.L", "RR.L",
    # Asian
    "BABA", "TCEHY", "NIO", "BIDU", "JD", "PDD", "SE",
    "HDB", "INFY", "VALE", "PBR",
    # ETFs
    "IWM", "VTI", "ARKK", "XLF", "XLE", "XLK", "XLV",
    "SOXX", "IBIT", "FBTC",
]


class PriorityManager:
    """
    Manages asset tiers dynamically.
    Tier 1 starts with TIER1_STATIC but is extended by:
    - user watchlist items (always tier 1)
    - recently traded assets
    - recently viewed assets (promoted temporarily)
    """

    def __init__(self):
        self._tier1: Set[str] = set(TIER1_STATIC)
        self._tier2: Set[str] = set(TIER2_STATIC)
        self._tier3: Set[str] = set()
        self._view_counts: Dict[str, int] = {}
        self._watchlist: Set[str] = set()

    def promote(self, symbol: str, to_tier: int = 1):
        """Promote an asset to a higher tier temporarily."""
        sym = symbol.upper()
        if to_tier == 1:
            self._tier1.add(sym)
            self._tier2.discard(sym)
            self._tier3.discard(sym)
        elif to_tier == 2:
            if sym not in self._tier1:
                self._tier2.add(sym)
                self._tier3.discard(sym)

    def set_watchlist(self, symbols: List[str]):
        """All watchlist items are tier 1."""
        self._watchlist = {s.upper() for s in symbols}
        for s in self._watchlist:
            self.promote(s, 1)

    def record_view(self, symbol: str):
        """Record a user view — frequently viewed assets get promoted."""
        sym = symbol.upper()
        self._view_counts[sym] = self._view_counts.get(sym, 0) + 1
        if self._view_counts[sym] >= 3:
            self.promote(sym, 1)
        elif self._view_counts[sym] >= 1:
            self.promote(sym, 2)

    def load_universe(self, symbols: List[str]):
        """Assign any universe symbols not already tiered to tier 3."""
        for sym in symbols:
            s = sym.upper()
            if s not in self._tier1 and s not in self._tier2:
                self._tier3.add(s)

    def get_tier(self, symbol: str) -> int:
        sym = symbol.upper()
        if sym in self._tier1:
            return 1
        if sym in self._tier2:
            return 2
        return 3

    def get_tier1(self) -> List[str]:
        return sorted(self._tier1)

    def get_tier2(self) -> List[str]:
        return sorted(self._tier2 - self._tier1)

    def get_tier3(self) -> List[str]:
        return sorted(self._tier3 - self._tier1 - self._tier2)

    def get_all_ordered(self) -> List[str]:
        """Return all assets ordered tier1 first."""
        return self.get_tier1() + self.get_tier2() + self.get_tier3()

    def summary(self) -> dict:
        return {
            "tier1": len(self._tier1),
            "tier2": len(self._tier2 - self._tier1),
            "tier3": len(self._tier3 - self._tier1 - self._tier2),
            "watchlist_in_tier1": len(self._watchlist & self._tier1),
        }


# Global singleton
priority_manager = PriorityManager()
