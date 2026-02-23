"""
Market Brain — Data Fetchers
─────────────────────────────
Fetchers pull raw asset data from external APIs.
They return normalised dicts. They do NOT write to the database.
The ingestion engine calls fetchers, classifies results, then writes.

Sources:
  - Yahoo Finance  (equities, ETFs, indices, forex)
  - CoinGecko      (crypto — free tier, no key needed)
  - Static seeds   (fallback / manual additions)

Adding a new source: implement a class with a fetch() method
that returns List[dict] with the raw fields listed in FIELD_SPEC.
"""

import asyncio
import logging
import time
from typing import List, Optional, Dict

import httpx

log = logging.getLogger("mb-ingestion.fetchers")

REQUEST_TIMEOUT = 12
RETRY_ATTEMPTS  = 3
RETRY_DELAY     = 2.0   # seconds between retries

YAHOO_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
}

# ── FIELD SPEC ─────────────────────────────────────────────────
# All fetchers should attempt to return these fields (all optional except ticker)
FIELD_SPEC = [
    "ticker", "name", "quote_type", "exchange", "currency", "country",
    "sector", "industry", "market_cap", "price", "change_pct",
    "avg_volume_30d", "fifty_two_week_high", "fifty_two_week_low",
    "beta", "pe_ratio", "atr_pct", "source", "source_id",
]


# ══════════════════════════════════════════════════════════════
# HTTP CLIENT
# ══════════════════════════════════════════════════════════════
async def _get(client: httpx.AsyncClient, url: str, params: dict = None,
               headers: dict = None) -> Optional[dict]:
    for attempt in range(RETRY_ATTEMPTS):
        try:
            r = await client.get(url, params=params,
                                 headers=headers or YAHOO_HEADERS,
                                 timeout=REQUEST_TIMEOUT)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429:
                wait = RETRY_DELAY * (attempt + 1) * 2
                log.warning(f"Rate limited by {url[:40]}... waiting {wait}s")
                await asyncio.sleep(wait)
                continue
            log.warning(f"HTTP {r.status_code} from {url[:60]}")
        except httpx.TimeoutException:
            log.warning(f"Timeout (attempt {attempt+1}): {url[:60]}")
        except Exception as e:
            log.warning(f"Error (attempt {attempt+1}): {e}")
        if attempt < RETRY_ATTEMPTS - 1:
            await asyncio.sleep(RETRY_DELAY)
    return None


# ══════════════════════════════════════════════════════════════
# YAHOO FINANCE FETCHER
# ══════════════════════════════════════════════════════════════
class YahooFetcher:
    """
    Fetches asset metadata from Yahoo Finance.
    No API key required.
    Rate limit: ~2000 requests/hour on the v1 endpoint.
    """

    BASE_SCREENER  = "https://query1.finance.yahoo.com/v1/finance/screener"
    BASE_QUOTE     = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    BASE_QUOTETYPE = "https://query1.finance.yahoo.com/v1/finance/quoteType/{symbol}"
    BASE_SUMMARY   = "https://query2.finance.yahoo.com/v10/finance/quoteSummary/{symbol}"

    # Yahoo screener presets
    SCREENER_QUERIES = {
        "us_large_cap": {
            "offset": 0, "size": 250,
            "sortField": "marketcap", "sortType": "DESC",
            "quoteType": "EQUITY", "region": "us",
            "filters": [{"field":"marketcap","operator":"GT","value":10_000_000_000}]
        },
        "us_mid_cap": {
            "offset": 0, "size": 250,
            "sortField": "marketcap", "sortType": "DESC",
            "quoteType": "EQUITY", "region": "us",
            "filters": [
                {"field":"marketcap","operator":"GT","value":2_000_000_000},
                {"field":"marketcap","operator":"LT","value":10_000_000_000},
            ]
        },
        "us_small_cap": {
            "offset": 0, "size": 250,
            "sortField": "marketcap", "sortType": "DESC",
            "quoteType": "EQUITY", "region": "us",
            "filters": [
                {"field":"marketcap","operator":"GT","value":300_000_000},
                {"field":"marketcap","operator":"LT","value":2_000_000_000},
            ]
        },
    }

    # Static lists for asset classes Yahoo doesn't screener well
    CRYPTO_TICKERS = [
        "BTC-USD","ETH-USD","BNB-USD","SOL-USD","XRP-USD","ADA-USD",
        "AVAX-USD","DOGE-USD","DOT-USD","MATIC-USD","LTC-USD","LINK-USD",
        "ATOM-USD","UNI-USD","ICP-USD","FIL-USD","APT-USD","ARB-USD",
        "OP-USD","SUI-USD","INJ-USD","NEAR-USD","TIA-USD","SEI-USD",
        "RUNE-USD","ALGO-USD","EGLD-USD","XMR-USD","ETC-USD","HBAR-USD",
    ]

    FOREX_TICKERS = [
        "EURUSD=X","GBPUSD=X","USDJPY=X","USDCHF=X","AUDUSD=X",
        "USDCAD=X","NZDUSD=X","EURGBP=X","EURJPY=X","GBPJPY=X",
        "USDCNH=X","USDINR=X","USDMXN=X","USDBRL=X","USDSGD=X",
        "USDKRW=X","USDHKD=X","USDTRY=X","USDZAR=X","USDNOK=X",
    ]

    COMMODITY_TICKERS = [
        "GC=F",   # Gold futures
        "SI=F",   # Silver futures
        "CL=F",   # Crude oil WTI
        "BZ=F",   # Brent crude
        "NG=F",   # Natural gas
        "HG=F",   # Copper
        "ZW=F",   # Wheat
        "ZC=F",   # Corn
        "ZS=F",   # Soybeans
        "GLD",    # Gold ETF
        "SLV",    # Silver ETF
        "USO",    # Oil ETF
        "UNG",    # Natural gas ETF
    ]

    ETF_TICKERS = [
        "SPY","QQQ","IWM","DIA","VTI","VOO","VEA","VWO","EFA","EEM",
        "XLK","XLF","XLV","XLE","XLI","XLB","XLY","XLP","XLRE","XLU",
        "GLD","SLV","TLT","IEF","LQD","HYG","VNQ","ARKK","ARKG","ARKW",
        "SQQQ","TQQQ","SPXL","SPXS","UVXY","VXX","VIXY",
        "SOXX","SMH","IBB","XBI","IHI","IYT","ITB","XHB",
        "EWJ","EWZ","EWC","EWA","EWG","EWU","FXI","INDA",
    ]

    async def fetch_quote(self, client: httpx.AsyncClient, ticker: str) -> Optional[dict]:
        """Fetch full quote for a single ticker."""
        url = self.BASE_QUOTE.format(symbol=ticker)
        data = await _get(client, url)
        if not data:
            return None
        try:
            result = data.get("chart", {}).get("result", [])
            if not result:
                return None
            meta = result[0].get("meta", {})
            price = meta.get("regularMarketPrice") or meta.get("previousClose")
            if not price:
                return None
            return {
                "ticker":              ticker,
                "name":                meta.get("shortName") or meta.get("longName") or ticker,
                "quote_type":          meta.get("instrumentType") or meta.get("quoteType"),
                "exchange":            meta.get("exchangeName"),
                "currency":            meta.get("currency", "USD"),
                "price":               round(float(price), 4),
                "change_pct":          None,
                "fifty_two_week_high": meta.get("fiftyTwoWeekHigh"),
                "fifty_two_week_low":  meta.get("fiftyTwoWeekLow"),
                "avg_volume_30d":      meta.get("regularMarketVolume"),
                "market_cap":          meta.get("marketCap"),
                "source":              "yahoo_finance",
            }
        except Exception as e:
            log.warning(f"Parse error for {ticker}: {e}")
            return None

    async def fetch_summary(self, client: httpx.AsyncClient, ticker: str) -> Optional[dict]:
        """Fetch detailed fundamentals (sector, beta, PE etc)."""
        url = self.BASE_SUMMARY.format(symbol=ticker)
        data = await _get(client, url, params={
            "modules": "summaryProfile,defaultKeyStatistics,summaryDetail,price"
        })
        if not data:
            return None
        try:
            result = data.get("quoteSummary", {}).get("result", [{}])[0]
            profile  = result.get("summaryProfile", {})
            stats    = result.get("defaultKeyStatistics", {})
            summary  = result.get("summaryDetail", {})
            price_m  = result.get("price", {})

            def val(d, k): return d.get(k, {}).get("raw") if isinstance(d.get(k), dict) else d.get(k)

            return {
                "sector":           profile.get("sector"),
                "industry":         profile.get("industry"),
                "country":          profile.get("country", "US"),
                "market_cap":       val(price_m, "marketCap") or val(summary, "marketCap"),
                "avg_volume_30d":   val(summary, "averageVolume"),
                "beta":             val(summary, "beta"),
                "pe_ratio":         val(summary, "trailingPE"),
                "fifty_two_week_high": val(summary, "fiftyTwoWeekHigh"),
                "fifty_two_week_low":  val(summary, "fiftyTwoWeekLow"),
                "short_ratio":      val(stats, "shortRatio"),
            }
        except Exception as e:
            log.warning(f"Summary parse error for {ticker}: {e}")
            return None

    async def fetch_tickers_batch(
        self,
        tickers: List[str],
        include_summary: bool = False,
        concurrency: int = 5,
    ) -> List[dict]:
        """Fetch a list of tickers with controlled concurrency."""
        results = []
        sem = asyncio.Semaphore(concurrency)

        async def fetch_one(ticker: str):
            async with sem:
                async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
                    quote = await self.fetch_quote(client, ticker)
                    if not quote:
                        return None
                    if include_summary:
                        summary = await self.fetch_summary(client, ticker)
                        if summary:
                            quote.update({k: v for k, v in summary.items() if v is not None})
                    await asyncio.sleep(0.2)  # gentle rate limiting
                    return quote

        tasks = [fetch_one(t) for t in tickers]
        raw = await asyncio.gather(*tasks, return_exceptions=True)
        for r in raw:
            if isinstance(r, dict):
                results.append(r)
            elif isinstance(r, Exception):
                log.warning(f"Fetch error: {r}")
        return results

    async def fetch_crypto(self) -> List[dict]:
        log.info(f"Fetching {len(self.CRYPTO_TICKERS)} crypto tickers from Yahoo")
        return await self.fetch_tickers_batch(self.CRYPTO_TICKERS, include_summary=False)

    async def fetch_forex(self) -> List[dict]:
        log.info(f"Fetching {len(self.FOREX_TICKERS)} forex pairs from Yahoo")
        return await self.fetch_tickers_batch(self.FOREX_TICKERS, include_summary=False)

    async def fetch_commodities(self) -> List[dict]:
        log.info(f"Fetching {len(self.COMMODITY_TICKERS)} commodity tickers from Yahoo")
        return await self.fetch_tickers_batch(self.COMMODITY_TICKERS, include_summary=False)

    async def fetch_etfs(self) -> List[dict]:
        log.info(f"Fetching {len(self.ETF_TICKERS)} ETF tickers from Yahoo")
        return await self.fetch_tickers_batch(self.ETF_TICKERS, include_summary=True, concurrency=3)

    async def fetch_equities_screener(self, query_name: str = "us_large_cap") -> List[dict]:
        """
        Use Yahoo's screener to pull equities by cap tier.
        Returns up to 250 results per call.
        """
        query = self.SCREENER_QUERIES.get(query_name, self.SCREENER_QUERIES["us_large_cap"])
        log.info(f"Screener: {query_name}")
        async with httpx.AsyncClient(timeout=15) as client:
            data = await _get(client, self.BASE_SCREENER,
                              params={"formatted": "false", "lang": "en-US", "region": "US"},
                              headers=YAHOO_HEADERS)
        if not data:
            # Screener endpoint is less reliable — fall back to known list
            log.warning("Yahoo screener unavailable — using fallback ticker list")
            return []

        try:
            quotes = data.get("finance", {}).get("result", [{}])[0].get("quotes", [])
            results = []
            for q in quotes:
                results.append({
                    "ticker":          q.get("symbol", ""),
                    "name":            q.get("shortName") or q.get("longName", ""),
                    "quote_type":      q.get("quoteType", "EQUITY"),
                    "exchange":        q.get("exchange"),
                    "currency":        q.get("currency", "USD"),
                    "market_cap":      q.get("marketCap"),
                    "price":           q.get("regularMarketPrice"),
                    "avg_volume_30d":  q.get("averageDailyVolume3Month"),
                    "sector":          q.get("sector"),
                    "industry":        q.get("industry"),
                    "fifty_two_week_high": q.get("fiftyTwoWeekHigh"),
                    "fifty_two_week_low":  q.get("fiftyTwoWeekLow"),
                    "source":          "yahoo_screener",
                })
            return results
        except Exception as e:
            log.warning(f"Screener parse error: {e}")
            return []

    async def validate_ticker(self, ticker: str) -> bool:
        """Check if a ticker is valid and active on Yahoo."""
        async with httpx.AsyncClient(timeout=8) as client:
            quote = await self.fetch_quote(client, ticker)
        return quote is not None and quote.get("price") is not None


# ══════════════════════════════════════════════════════════════
# COINGECKO FETCHER (crypto — no API key)
# ══════════════════════════════════════════════════════════════
class CoinGeckoFetcher:
    """
    Free CoinGecko API — no key needed.
    Rate limit: 10-30 calls/minute on free tier.
    Returns top N coins by market cap.
    """
    BASE = "https://api.coingecko.com/api/v3"

    async def fetch_top_coins(self, limit: int = 100) -> List[dict]:
        log.info(f"Fetching top {limit} coins from CoinGecko")
        results = []
        pages = (limit // 100) + 1

        async with httpx.AsyncClient(timeout=15) as client:
            for page in range(1, pages + 1):
                data = await _get(client, f"{self.BASE}/coins/markets", params={
                    "vs_currency": "usd",
                    "order":       "market_cap_desc",
                    "per_page":    100,
                    "page":        page,
                    "sparkline":   False,
                }, headers={"Accept": "application/json"})

                if not data:
                    break

                for coin in data:
                    symbol = coin.get("symbol", "").upper()
                    ticker = f"{symbol}-USD"
                    market_cap = coin.get("market_cap")
                    results.append({
                        "ticker":          ticker,
                        "name":            coin.get("name", symbol),
                        "quote_type":      "CRYPTOCURRENCY",
                        "sector":          "Crypto",
                        "currency":        "USD",
                        "market_cap":      market_cap,
                        "price":           coin.get("current_price"),
                        "change_pct":      coin.get("price_change_percentage_24h"),
                        "avg_volume_30d":  coin.get("total_volume"),
                        "fifty_two_week_high": coin.get("ath"),
                        "fifty_two_week_low":  coin.get("atl"),
                        "source":          "coingecko",
                        "source_id":       coin.get("id"),
                    })

                if len(data) < 100:
                    break
                await asyncio.sleep(2)  # CoinGecko rate limit

        return results[:limit]


# ══════════════════════════════════════════════════════════════
# STATIC SEED FETCHER
# ══════════════════════════════════════════════════════════════
class StaticSeedFetcher:
    """
    Manually curated asset list.
    Used for assets that screeners miss or for initial bootstrapping.
    Edit SEEDS to add/remove manually curated assets.
    """

    SEEDS = [
        # ── Space ──────────────────────────────────────────────
        {"ticker":"RKLB","name":"Rocket Lab USA","sector":"Space","industry":"Launch","country":"US"},
        {"ticker":"ASTS","name":"AST SpaceMobile","sector":"Space","industry":"Satellite","country":"US"},
        {"ticker":"LMT","name":"Lockheed Martin","sector":"Space","industry":"Defence","country":"US"},
        {"ticker":"NOC","name":"Northrop Grumman","sector":"Space","industry":"Defence","country":"US"},
        {"ticker":"SPCE","name":"Virgin Galactic","sector":"Space","industry":"Launch","country":"US"},
        {"ticker":"MAXR","name":"Maxar Technologies","sector":"Space","industry":"Satellite","country":"US"},
        # ── Quantum ───────────────────────────────────────────
        {"ticker":"IONQ","name":"IonQ","sector":"Technology","industry":"Quantum Computing","country":"US"},
        {"ticker":"QUBT","name":"Quantum Computing Inc","sector":"Technology","industry":"Quantum Computing","country":"US"},
        {"ticker":"RGTI","name":"Rigetti Computing","sector":"Technology","industry":"Quantum Computing","country":"US"},
        {"ticker":"QBTS","name":"D-Wave Quantum","sector":"Technology","industry":"Quantum Computing","country":"US"},
        # ── Nuclear / Uranium ─────────────────────────────────
        {"ticker":"UEC","name":"Uranium Energy","sector":"Energy","industry":"Uranium","country":"US"},
        {"ticker":"NNE","name":"Nano Nuclear Energy","sector":"Energy","industry":"Nuclear","country":"US"},
        {"ticker":"CCJ","name":"Cameco","sector":"Energy","industry":"Uranium","country":"CA"},
        {"ticker":"DNN","name":"Denison Mines","sector":"Energy","industry":"Uranium","country":"CA"},
        {"ticker":"UUUU","name":"Energy Fuels","sector":"Energy","industry":"Uranium","country":"US"},
        # ── Lithium / EV Materials ────────────────────────────
        {"ticker":"LAC","name":"Lithium Americas","sector":"Materials","industry":"Lithium","country":"CA"},
        {"ticker":"ALB","name":"Albemarle","sector":"Materials","industry":"Lithium","country":"US"},
        {"ticker":"SQM","name":"Sociedad Quimica","sector":"Materials","industry":"Lithium","country":"CL"},
        {"ticker":"PLL","name":"Piedmont Lithium","sector":"Materials","industry":"Lithium","country":"US"},
        # ── AI / ML ──────────────────────────────────────────
        {"ticker":"SOUN","name":"SoundHound AI","sector":"Technology","industry":"AI / ML","country":"US"},
        {"ticker":"BBAI","name":"BigBear.ai","sector":"Technology","industry":"AI / ML","country":"US"},
        {"ticker":"PLTR","name":"Palantir","sector":"Technology","industry":"AI / ML","country":"US"},
        {"ticker":"AI","name":"C3.ai","sector":"Technology","industry":"AI / ML","country":"US"},
        {"ticker":"UPST","name":"Upstart Holdings","sector":"Finance","industry":"AI Lending","country":"US"},
        # ── UK Stocks ─────────────────────────────────────────
        {"ticker":"BP.L","name":"BP plc","sector":"Energy","industry":"Oil & Gas","country":"GB","exchange":"LSE","currency":"GBP"},
        {"ticker":"SHEL.L","name":"Shell plc","sector":"Energy","industry":"Oil & Gas","country":"GB","exchange":"LSE","currency":"GBP"},
        {"ticker":"HSBA.L","name":"HSBC Holdings","sector":"Finance","industry":"Banking","country":"GB","exchange":"LSE","currency":"GBP"},
        {"ticker":"LLOY.L","name":"Lloyds Banking Group","sector":"Finance","industry":"Banking","country":"GB","exchange":"LSE","currency":"GBP"},
        {"ticker":"VOD.L","name":"Vodafone","sector":"Technology","industry":"Telecom","country":"GB","exchange":"LSE","currency":"GBP"},
        {"ticker":"RIO.L","name":"Rio Tinto","sector":"Materials","industry":"Mining","country":"GB","exchange":"LSE","currency":"GBP"},
        {"ticker":"BHP.L","name":"BHP Group","sector":"Materials","industry":"Mining","country":"AU","exchange":"LSE","currency":"GBP"},
        {"ticker":"AZN.L","name":"AstraZeneca","sector":"Healthcare","industry":"Pharma","country":"GB","exchange":"LSE","currency":"GBP"},
        {"ticker":"GSK.L","name":"GSK plc","sector":"Healthcare","industry":"Pharma","country":"GB","exchange":"LSE","currency":"GBP"},
        {"ticker":"ULVR.L","name":"Unilever","sector":"Consumer","industry":"FMCG","country":"GB","exchange":"LSE","currency":"GBP"},
        # ── Biotech / CRISPR ──────────────────────────────────
        {"ticker":"NTLA","name":"Intellia Therapeutics","sector":"Healthcare","industry":"CRISPR","country":"US"},
        {"ticker":"CRSP","name":"CRISPR Therapeutics","sector":"Healthcare","industry":"CRISPR","country":"US"},
        {"ticker":"BEAM","name":"Beam Therapeutics","sector":"Healthcare","industry":"Gene Editing","country":"US"},
        {"ticker":"RXRX","name":"Recursion Pharma","sector":"Healthcare","industry":"AI Drug Discovery","country":"US"},
        # ── Green Energy ──────────────────────────────────────
        {"ticker":"PLUG","name":"Plug Power","sector":"Energy","industry":"Hydrogen","country":"US"},
        {"ticker":"FCEL","name":"FuelCell Energy","sector":"Energy","industry":"Hydrogen","country":"US"},
        {"ticker":"ENPH","name":"Enphase Energy","sector":"Energy","industry":"Solar","country":"US"},
        {"ticker":"SEDG","name":"SolarEdge Technologies","sector":"Energy","industry":"Solar","country":"US"},
        {"ticker":"HASI","name":"Hannon Armstrong","sector":"Energy","industry":"Clean Energy Finance","country":"US"},
    ]

    async def fetch(self) -> List[dict]:
        log.info(f"Loading {len(self.SEEDS)} static seed assets")
        for seed in self.SEEDS:
            seed["source"] = "static_seed"
            if "quote_type" not in seed:
                seed["quote_type"] = "EQUITY"
        return self.SEEDS
