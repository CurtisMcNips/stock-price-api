"""
Market Brain — Asset Classifiers
─────────────────────────────────
Pure functions. No side effects. No data fetching.
Take raw market data, return structured classification.
"""

from typing import Optional


# ── Market Cap Tiers ───────────────────────────────────────────
# Based on standard institutional definitions (USD)
CAP_TIERS = [
    ("Mega",  500_000_000_000),   # $500B+
    ("Large",  10_000_000_000),   # $10B–$500B
    ("Mid",     2_000_000_000),   # $2B–$10B
    ("Small",     300_000_000),   # $300M–$2B
    ("Micro",      50_000_000),   # $50M–$300M
    ("Nano",               0),   # <$50M
]

def classify_market_cap(market_cap_usd: Optional[float]) -> str:
    if not market_cap_usd or market_cap_usd <= 0:
        return "Unknown"
    for tier, threshold in CAP_TIERS:
        if market_cap_usd >= threshold:
            return tier
    return "Nano"


# ── Volatility Tiers ───────────────────────────────────────────
# Based on typical 30-day ATR% or beta
def classify_volatility(
    beta: Optional[float] = None,
    atr_pct: Optional[float] = None,
    asset_type: str = "equity",
) -> str:
    # Crypto always extreme
    if asset_type == "crypto":
        return "Extreme"
    if asset_type == "forex":
        return "Low"

    # Use ATR% if available (more accurate)
    if atr_pct is not None:
        if atr_pct < 1.0:   return "Low"
        if atr_pct < 2.0:   return "Med"
        if atr_pct < 4.0:   return "High"
        if atr_pct < 7.0:   return "VHigh"
        return "Extreme"

    # Fall back to beta
    if beta is not None:
        if beta < 0.6:   return "Low"
        if beta < 1.0:   return "Med"
        if beta < 1.5:   return "High"
        if beta < 2.5:   return "VHigh"
        return "Extreme"

    # Asset-type defaults
    defaults = {
        "equity": "Med", "etf": "Low", "index": "Low",
        "commodity": "Med", "crypto": "Extreme", "forex": "Low",
    }
    return defaults.get(asset_type, "Med")


# ── Asset Type Detection ───────────────────────────────────────
ETF_KEYWORDS = ["ETF", "FUND", "TRUST", "SHARES", "iSHARES", "SPDR",
                "VANGUARD", "INVESCO", "WISDOMTREE", "PROSHARES"]

CRYPTO_SUFFIXES = ["-USD", "-USDT", "-BTC", "-ETH"]

FOREX_PATTERN = ["=X", "=F"]

INDEX_KEYWORDS = ["INDEX", "^", "S&P", "NASDAQ", "DOW", "FTSE",
                  "RUSSELL", "NIKKEI", "DAX", "CAC"]

def classify_asset_type(
    ticker: str,
    name: str = "",
    quote_type: Optional[str] = None,   # from Yahoo: EQUITY, ETF, MUTUALFUND, etc.
) -> str:
    ticker_upper = ticker.upper()
    name_upper   = name.upper()

    # Yahoo gives us this directly
    if quote_type:
        qt = quote_type.upper()
        if qt == "ETF":        return "etf"
        if qt == "EQUITY":     return "equity"
        if qt == "MUTUALFUND": return "etf"
        if qt == "FUTURE":     return "commodity"
        if qt == "INDEX":      return "index"
        if qt == "CURRENCY":   return "forex"
        if qt == "CRYPTOCURRENCY": return "crypto"

    # Infer from ticker/name
    if any(ticker_upper.endswith(s) for s in CRYPTO_SUFFIXES):
        return "crypto"
    if any(ticker_upper.endswith(s) for s in FOREX_PATTERN):
        return "forex"
    if ticker_upper.startswith("^"):
        return "index"
    if any(kw in name_upper for kw in ETF_KEYWORDS):
        return "etf"
    if any(kw in name_upper for kw in INDEX_KEYWORDS):
        return "index"

    return "equity"


# ── Sector Mapping ─────────────────────────────────────────────
# Maps Yahoo Finance sector strings to Market Brain sectors
SECTOR_MAP = {
    "Technology":                   "Technology",
    "Consumer Cyclical":            "Consumer",
    "Consumer Defensive":           "Consumer",
    "Healthcare":                   "Healthcare",
    "Financial Services":           "Finance",
    "Industrials":                  "Industrials",
    "Basic Materials":              "Materials",
    "Energy":                       "Energy",
    "Communication Services":       "Technology",
    "Real Estate":                  "Real Estate",
    "Utilities":                    "Utilities",
    # Crypto
    "Cryptocurrency":               "Crypto",
    "Digital Assets":               "Crypto",
    # Forex
    "Currency":                     "Forex",
    # Commodities
    "Metals & Mining":              "Materials",
    "Gold":                         "Materials",
    "Oil & Gas":                    "Energy",
    "Agriculture":                  "Agriculture",
}

def normalise_sector(yahoo_sector: Optional[str], asset_type: str = "equity") -> Optional[str]:
    if not yahoo_sector:
        if asset_type == "crypto":   return "Crypto"
        if asset_type == "forex":    return "Forex"
        if asset_type == "commodity": return "Commodities"
        return None
    return SECTOR_MAP.get(yahoo_sector, yahoo_sector)


# ── Liquidity Filter ───────────────────────────────────────────
def is_liquid_enough(
    avg_volume_30d: Optional[float],
    price: Optional[float],
    asset_type: str = "equity",
    min_adv_usd: float = 1_000_000,    # $1M average daily value default
    min_price: float = 0.50,           # skip sub-50c stocks by default
) -> tuple[bool, str]:
    """
    Returns (passes, reason).
    Crypto and forex always pass (liquidity managed differently).
    """
    if asset_type in ("crypto", "forex", "index"):
        return True, "asset type exempt from liquidity filter"

    if price is not None and price < min_price:
        return False, f"price ${price:.4f} below minimum ${min_price}"

    if avg_volume_30d and price:
        adv = avg_volume_30d * price
        if adv < min_adv_usd:
            return False, f"ADV ${adv:,.0f} below minimum ${min_adv_usd:,.0f}"

    return True, "passes"


# ── Exchange Filter ────────────────────────────────────────────
# Allowed exchanges — exclude OTC, pink sheets, grey market
ALLOWED_EXCHANGES = {
    "NYSE", "NASDAQ", "AMEX", "NYSEArca",          # US
    "LSE", "LON",                                    # UK
    "XETRA", "FSX",                                 # Germany
    "EPA", "PAR",                                   # France
    "AMS",                                          # Netherlands
    "TSX", "TSX-V",                                 # Canada
    "ASX",                                          # Australia
    "NSE", "BSE",                                   # India
    "HKEX", "HKG",                                  # Hong Kong
    "SGX",                                          # Singapore
    "BINANCE", "COINBASE", "KRAKEN",                # Crypto
    "FX", "FOREX",                                  # Forex
    "CME", "NYMEX", "COMEX",                        # Commodities
}

OTC_MARKERS = {"OTC", "OTCMKTS", "OTCBB", "PINK", "GREY", "EXPERT"}

def is_allowed_exchange(exchange: Optional[str]) -> tuple[bool, str]:
    if not exchange:
        return True, "exchange unknown — allowing"
    ex = exchange.upper()
    if any(otc in ex for otc in OTC_MARKERS):
        return False, f"OTC/pink sheet exchange: {exchange}"
    return True, "allowed exchange"


# ── Symbol Normalisation ───────────────────────────────────────
def normalise_ticker(ticker: str, exchange: Optional[str] = None) -> str:
    """
    Normalise ticker to Yahoo Finance format.
    Handles London .L, Frankfurt .DE, Paris .PA etc.
    """
    ticker = ticker.upper().strip().replace(" ", "")
    if exchange:
        ex = exchange.upper()
        if ex in ("LSE", "LON") and not ticker.endswith(".L"):
            return ticker + ".L"
        if ex in ("XETRA", "FSX") and not ticker.endswith(".DE"):
            return ticker + ".DE"
        if ex in ("EPA", "PAR") and not ticker.endswith(".PA"):
            return ticker + ".PA"
        if ex in ("AMS",) and not ticker.endswith(".AS"):
            return ticker + ".AS"
        if ex in ("TSX",) and not ticker.endswith(".TO"):
            return ticker + ".TO"
        if ex in ("ASX",) and not ticker.endswith(".AX"):
            return ticker + ".AX"
    return ticker


# ── Full Classification Pipeline ──────────────────────────────
def classify_asset(raw: dict) -> dict:
    """
    Takes raw data from any source.
    Returns a fully classified asset dict ready for upsert_asset().
    """
    ticker     = normalise_ticker(raw.get("ticker", ""), raw.get("exchange"))
    name       = raw.get("name", ticker)
    quote_type = raw.get("quote_type")
    exchange   = raw.get("exchange")
    market_cap = raw.get("market_cap")
    price      = raw.get("price")
    beta       = raw.get("beta")
    atr_pct    = raw.get("atr_pct")
    volume     = raw.get("avg_volume_30d") or raw.get("volume")

    asset_type    = classify_asset_type(ticker, name, quote_type)
    sector        = normalise_sector(raw.get("sector"), asset_type)
    cap_tier      = classify_market_cap(market_cap)
    vol_tier      = classify_volatility(beta, atr_pct, asset_type)

    return {
        "ticker":           ticker,
        "name":             name,
        "asset_type":       asset_type,
        "sector":           sector,
        "industry":         raw.get("industry"),
        "exchange":         exchange,
        "country":          raw.get("country", "US"),
        "currency":         raw.get("currency", "USD"),
        "market_cap":       market_cap,
        "market_cap_tier":  cap_tier,
        "volatility_tier":  vol_tier,
        "price_last":       price,
        "price_52w_high":   raw.get("fifty_two_week_high"),
        "price_52w_low":    raw.get("fifty_two_week_low"),
        "avg_volume_30d":   volume,
        "source":           raw.get("source", "unknown"),
        "source_id":        raw.get("source_id"),
    }
