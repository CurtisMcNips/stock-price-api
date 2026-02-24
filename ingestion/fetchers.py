"""
Market Brain — Data Fetchers
─────────────────────────────
Fetchers pull raw asset data from external APIs.
They return normalised dicts. They do NOT write to the database.

Sources:
  - Yahoo Finance  (equities, ETFs, indices, forex) — uses v8/chart only
  - CoinGecko      (crypto — free tier, no key needed)
  - Static seeds   (fallback / manual additions)

NOTE: Yahoo's v10/quoteSummary endpoint now requires auth (401).
      We use only the v8/chart endpoint which still works freely.
"""

import asyncio
import logging
import time
from typing import List, Optional, Dict

import httpx

log = logging.getLogger("mb-ingestion.fetchers")

REQUEST_TIMEOUT = 12
RETRY_ATTEMPTS  = 3
RETRY_DELAY     = 2.0

YAHOO_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
}

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
                log.warning(f"Rate limited — waiting {wait}s")
                await asyncio.sleep(wait)
                continue
            if r.status_code == 401:
                log.warning(f"HTTP 401 (auth required) — skipping {url[:60]}")
                return None
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
    Fetches asset data from Yahoo Finance using only the v8/chart endpoint.
    The v10/quoteSummary endpoint now requires authentication and is not used.
    """

    # v8/chart works without auth — this is our only Yahoo endpoint
    BASE_QUOTE = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    BASE_QUOTE_FALLBACK = "https://query2.finance.yahoo.com/v8/finance/chart/{symbol}"

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
        """Fetch quote for a single ticker using v8/chart endpoint only."""
        for url_template in [self.BASE_QUOTE, self.BASE_QUOTE_FALLBACK]:
            url = url_template.format(symbol=ticker)
            data = await _get(client, url)
            if not data:
                continue
            try:
                result = data.get("chart", {}).get("result", [])
                if not result:
                    continue
                meta = result[0].get("meta", {})
                price = meta.get("regularMarketPrice") or meta.get("previousClose")
                if not price:
                    continue

                prev_close = meta.get("previousClose") or meta.get("chartPreviousClose") or price
                change_pct = ((price - prev_close) / prev_close * 100) if prev_close else 0

                return {
                    "ticker":              ticker,
                    "name":                meta.get("shortName") or meta.get("longName") or ticker,
                    "quote_type":          meta.get("instrumentType") or meta.get("quoteType"),
                    "exchange":            meta.get("exchangeName"),
                    "currency":            meta.get("currency", "USD"),
                    "price":               round(float(price), 4),
                    "change_pct":          round(float(change_pct), 4),
                    "fifty_two_week_high": meta.get("fiftyTwoWeekHigh"),
                    "fifty_two_week_low":  meta.get("fiftyTwoWeekLow"),
                    "avg_volume_30d":      meta.get("regularMarketVolume"),
                    "market_cap":          meta.get("marketCap"),
                    "source":              "yahoo_finance",
                }
            except Exception as e:
                log.warning(f"Parse error for {ticker}: {e}")
                continue
        return None

    async def fetch_tickers_batch(
        self,
        tickers: List[str],
        concurrency: int = 5,
    ) -> List[dict]:
        """Fetch a list of tickers with controlled concurrency."""
        results = []
        sem = asyncio.Semaphore(concurrency)

        async def fetch_one(ticker: str):
            async with sem:
                async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
                    quote = await self.fetch_quote(client, ticker)
                    await asyncio.sleep(0.2)
                    return quote

        tasks = [fetch_one(t) for t in tickers]
        raw = await asyncio.gather(*tasks, return_exceptions=True)
        for r in raw:
            if isinstance(r, dict):
                results.append(r)
            elif isinstance(r, Exception):
                log.warning(f"Fetch error: {r}")
        return results

    async def fetch_forex(self) -> List[dict]:
        log.info(f"Fetching {len(self.FOREX_TICKERS)} forex pairs from Yahoo")
        return await self.fetch_tickers_batch(self.FOREX_TICKERS)

    async def fetch_commodities(self) -> List[dict]:
        log.info(f"Fetching {len(self.COMMODITY_TICKERS)} commodity tickers from Yahoo")
        return await self.fetch_tickers_batch(self.COMMODITY_TICKERS)

    async def fetch_etfs(self) -> List[dict]:
        log.info(f"Fetching {len(self.ETF_TICKERS)} ETF tickers from Yahoo")
        return await self.fetch_tickers_batch(self.ETF_TICKERS, concurrency=3)

    async def fetch_equities_screener(self, query_name: str = "us_large_cap") -> List[dict]:
        """
        Yahoo screener for equities — may or may not work without auth.
        Falls back to empty list gracefully if blocked.
        """
        log.info(f"Attempting Yahoo screener: {query_name}")
        async with httpx.AsyncClient(timeout=15) as client:
            data = await _get(client,
                              "https://query1.finance.yahoo.com/v1/finance/screener",
                              params={"formatted": "false", "lang": "en-US", "region": "US"},
                              headers=YAHOO_HEADERS)
        if not data:
            log.warning("Yahoo screener unavailable — skipping")
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
# COINGECKO FETCHER
# ══════════════════════════════════════════════════════════════
class CoinGeckoFetcher:
    """
    Free CoinGecko API — no key needed.
    Rate limit: 10-30 calls/minute on free tier.
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
                    results.append({
                        "ticker":          ticker,
                        "name":            coin.get("name", symbol),
                        "quote_type":      "CRYPTOCURRENCY",
                        "sector":          "Crypto",
                        "currency":        "USD",
                        "market_cap":      coin.get("market_cap"),
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
                await asyncio.sleep(2)

        return results[:limit]


# ══════════════════════════════════════════════════════════════
# STATIC SEED FETCHER
# ══════════════════════════════════════════════════════════════
class StaticSeedFetcher:
    """
    Manually curated asset list.
    These bypass the liquidity filter as they are pre-vetted.
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
