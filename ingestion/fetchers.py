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
from typing import List, Optional

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
                log.warning(f"HTTP 401 — skipping {url[:60]}")
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

    BASE_QUOTE          = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    BASE_QUOTE_FALLBACK = "https://query2.finance.yahoo.com/v8/finance/chart/{symbol}"

    FOREX_TICKERS = [
        "EURUSD=X","GBPUSD=X","USDJPY=X","USDCHF=X","AUDUSD=X",
        "USDCAD=X","NZDUSD=X","EURGBP=X","EURJPY=X","GBPJPY=X",
        "USDCNH=X","USDINR=X","USDMXN=X","USDBRL=X","USDSGD=X",
        "USDKRW=X","USDHKD=X","USDTRY=X","USDZAR=X","USDNOK=X",
    ]

    COMMODITY_TICKERS = [
        "GC=F","SI=F","CL=F","BZ=F","NG=F","HG=F",
        "ZW=F","ZC=F","ZS=F","GLD","SLV","USO","UNG",
    ]

    ETF_TICKERS = [
        "SPY","QQQ","IWM","DIA","VTI","VOO","VEA","VWO","EFA","EEM",
        "XLK","XLF","XLV","XLE","XLI","XLB","XLY","XLP","XLRE","XLU",
        "GLD","SLV","TLT","IEF","LQD","HYG","VNQ","ARKK","ARKG","ARKW",
        "SQQQ","TQQQ","SPXL","SPXS","UVXY","VXX",
        "SOXX","SMH","IBB","XBI","IHI","IYT","ITB","XHB",
        "EWJ","EWZ","EWC","EWA","EWG","EWU","FXI","INDA",
        "BOTZ","ROBO","AIQ","WCLD","BUG","HACK",
        "ICLN","QCLN","TAN","FAN","ACES","GRID",
        "CPER","REMX","LIT","PICK","SIL","GDX","GDXJ",
    ]

    async def fetch_quote(self, client: httpx.AsyncClient, ticker: str) -> Optional[dict]:
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

    async def fetch_tickers_batch(self, tickers: List[str], concurrency: int = 5) -> List[dict]:
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
        return results

    async def fetch_forex(self) -> List[dict]:
        log.info(f"Fetching {len(self.FOREX_TICKERS)} forex pairs")
        return await self.fetch_tickers_batch(self.FOREX_TICKERS)

    async def fetch_commodities(self) -> List[dict]:
        log.info(f"Fetching {len(self.COMMODITY_TICKERS)} commodities")
        return await self.fetch_tickers_batch(self.COMMODITY_TICKERS)

    async def fetch_etfs(self) -> List[dict]:
        log.info(f"Fetching {len(self.ETF_TICKERS)} ETFs")
        return await self.fetch_tickers_batch(self.ETF_TICKERS, concurrency=3)

    async def fetch_equities_screener(self, query_name: str = "us_large_cap") -> List[dict]:
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
            return [{
                "ticker":         q.get("symbol",""),
                "name":           q.get("shortName") or q.get("longName",""),
                "quote_type":     q.get("quoteType","EQUITY"),
                "exchange":       q.get("exchange"),
                "currency":       q.get("currency","USD"),
                "market_cap":     q.get("marketCap"),
                "price":          q.get("regularMarketPrice"),
                "avg_volume_30d": q.get("averageDailyVolume3Month"),
                "sector":         q.get("sector"),
                "industry":       q.get("industry"),
                "fifty_two_week_high": q.get("fiftyTwoWeekHigh"),
                "fifty_two_week_low":  q.get("fiftyTwoWeekLow"),
                "source":         "yahoo_screener",
            } for q in quotes]
        except Exception as e:
            log.warning(f"Screener parse error: {e}")
            return []

    async def validate_ticker(self, ticker: str) -> bool:
        async with httpx.AsyncClient(timeout=8) as client:
            quote = await self.fetch_quote(client, ticker)
        return quote is not None and quote.get("price") is not None


# ══════════════════════════════════════════════════════════════
# COINGECKO FETCHER
# ══════════════════════════════════════════════════════════════
class CoinGeckoFetcher:

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
                    symbol = coin.get("symbol","").upper()
                    results.append({
                        "ticker":          f"{symbol}-USD",
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
# STATIC SEED FETCHER — 300+ curated assets
# ══════════════════════════════════════════════════════════════
class StaticSeedFetcher:
    """
    Manually curated asset list — pre-vetted, bypass liquidity filter.
    Covers: US mega/large caps, UK FTSE stocks, thematic plays, crypto proxies.
    """

    SEEDS = [
        # ── US MEGA CAP TECH ─────────────────────────────────
        {"ticker":"NVDA","name":"NVIDIA","sector":"Technology","industry":"Semiconductors","country":"US"},
        {"ticker":"AAPL","name":"Apple","sector":"Technology","industry":"Hardware","country":"US"},
        {"ticker":"MSFT","name":"Microsoft","sector":"Technology","industry":"Software","country":"US"},
        {"ticker":"GOOGL","name":"Alphabet","sector":"Technology","industry":"Software","country":"US"},
        {"ticker":"META","name":"Meta Platforms","sector":"Technology","industry":"Social Media","country":"US"},
        {"ticker":"AMD","name":"AMD","sector":"Technology","industry":"Semiconductors","country":"US"},
        {"ticker":"INTC","name":"Intel","sector":"Technology","industry":"Semiconductors","country":"US"},
        {"ticker":"AVGO","name":"Broadcom","sector":"Technology","industry":"Semiconductors","country":"US"},
        {"ticker":"QCOM","name":"Qualcomm","sector":"Technology","industry":"Semiconductors","country":"US"},
        {"ticker":"TXN","name":"Texas Instruments","sector":"Technology","industry":"Semiconductors","country":"US"},
        {"ticker":"MU","name":"Micron Technology","sector":"Technology","industry":"Semiconductors","country":"US"},
        {"ticker":"AMAT","name":"Applied Materials","sector":"Technology","industry":"Semiconductors","country":"US"},
        {"ticker":"LRCX","name":"Lam Research","sector":"Technology","industry":"Semiconductors","country":"US"},
        {"ticker":"ASML","name":"ASML Holding","sector":"Technology","industry":"Semiconductors","country":"NL"},
        {"ticker":"TSM","name":"TSMC","sector":"Technology","industry":"Semiconductors","country":"TW"},
        {"ticker":"ARM","name":"Arm Holdings","sector":"Technology","industry":"Semiconductors","country":"GB"},
        {"ticker":"MRVL","name":"Marvell Technology","sector":"Technology","industry":"Semiconductors","country":"US"},
        {"ticker":"KLAC","name":"KLA Corporation","sector":"Technology","industry":"Semiconductors","country":"US"},

        # ── SOFTWARE / CLOUD ─────────────────────────────────
        {"ticker":"CRM","name":"Salesforce","sector":"Technology","industry":"Software","country":"US"},
        {"ticker":"ORCL","name":"Oracle","sector":"Technology","industry":"Software","country":"US"},
        {"ticker":"ADBE","name":"Adobe","sector":"Technology","industry":"Software","country":"US"},
        {"ticker":"NOW","name":"ServiceNow","sector":"Technology","industry":"Software","country":"US"},
        {"ticker":"SNOW","name":"Snowflake","sector":"Technology","industry":"Cloud","country":"US"},
        {"ticker":"DDOG","name":"Datadog","sector":"Technology","industry":"Cloud","country":"US"},
        {"ticker":"NET","name":"Cloudflare","sector":"Technology","industry":"Cloud","country":"US"},
        {"ticker":"MDB","name":"MongoDB","sector":"Technology","industry":"Cloud","country":"US"},
        {"ticker":"HUBS","name":"HubSpot","sector":"Technology","industry":"Software","country":"US"},
        {"ticker":"TEAM","name":"Atlassian","sector":"Technology","industry":"Software","country":"AU"},
        {"ticker":"WDAY","name":"Workday","sector":"Technology","industry":"Software","country":"US"},
        {"ticker":"ZM","name":"Zoom Video","sector":"Technology","industry":"Software","country":"US"},
        {"ticker":"TWLO","name":"Twilio","sector":"Technology","industry":"Cloud","country":"US"},

        # ── CYBERSECURITY ────────────────────────────────────
        {"ticker":"CRWD","name":"CrowdStrike","sector":"Technology","industry":"Cybersecurity","country":"US"},
        {"ticker":"PANW","name":"Palo Alto Networks","sector":"Technology","industry":"Cybersecurity","country":"US"},
        {"ticker":"ZS","name":"Zscaler","sector":"Technology","industry":"Cybersecurity","country":"US"},
        {"ticker":"S","name":"SentinelOne","sector":"Technology","industry":"Cybersecurity","country":"US"},
        {"ticker":"OKTA","name":"Okta","sector":"Technology","industry":"Cybersecurity","country":"US"},
        {"ticker":"FTNT","name":"Fortinet","sector":"Technology","industry":"Cybersecurity","country":"US"},

        # ── AI / ML ──────────────────────────────────────────
        {"ticker":"PLTR","name":"Palantir","sector":"Technology","industry":"AI / ML","country":"US"},
        {"ticker":"AI","name":"C3.ai","sector":"Technology","industry":"AI / ML","country":"US"},
        {"ticker":"SOUN","name":"SoundHound AI","sector":"Technology","industry":"AI / ML","country":"US"},
        {"ticker":"BBAI","name":"BigBear.ai","sector":"Technology","industry":"AI / ML","country":"US"},
        {"ticker":"UPST","name":"Upstart Holdings","sector":"Finance","industry":"AI Lending","country":"US"},

        # ── QUANTUM COMPUTING ────────────────────────────────
        {"ticker":"IONQ","name":"IonQ","sector":"Technology","industry":"Quantum Computing","country":"US"},
        {"ticker":"QUBT","name":"Quantum Computing Inc","sector":"Technology","industry":"Quantum Computing","country":"US"},
        {"ticker":"RGTI","name":"Rigetti Computing","sector":"Technology","industry":"Quantum Computing","country":"US"},
        {"ticker":"QBTS","name":"D-Wave Quantum","sector":"Technology","industry":"Quantum Computing","country":"US"},

        # ── STREAMING / MEDIA / GAMING ───────────────────────
        {"ticker":"NFLX","name":"Netflix","sector":"Consumer","industry":"Streaming","country":"US"},
        {"ticker":"DIS","name":"Walt Disney","sector":"Consumer","industry":"Entertainment","country":"US"},
        {"ticker":"SPOT","name":"Spotify","sector":"Consumer","industry":"Streaming","country":"SE"},
        {"ticker":"RBLX","name":"Roblox","sector":"Consumer","industry":"Gaming","country":"US"},
        {"ticker":"TTWO","name":"Take-Two Interactive","sector":"Consumer","industry":"Gaming","country":"US"},
        {"ticker":"EA","name":"Electronic Arts","sector":"Consumer","industry":"Gaming","country":"US"},
        {"ticker":"ATVI","name":"Activision Blizzard","sector":"Consumer","industry":"Gaming","country":"US"},

        # ── US CONSUMER ───────────────────────────────────────
        {"ticker":"AMZN","name":"Amazon","sector":"Consumer","industry":"E-Commerce","country":"US"},
        {"ticker":"TSLA","name":"Tesla","sector":"Consumer","industry":"EV","country":"US"},
        {"ticker":"NKE","name":"Nike","sector":"Consumer","industry":"Apparel","country":"US"},
        {"ticker":"SBUX","name":"Starbucks","sector":"Consumer","industry":"Restaurants","country":"US"},
        {"ticker":"MCD","name":"McDonald's","sector":"Consumer","industry":"Restaurants","country":"US"},
        {"ticker":"CMG","name":"Chipotle","sector":"Consumer","industry":"Restaurants","country":"US"},
        {"ticker":"LULU","name":"Lululemon","sector":"Consumer","industry":"Apparel","country":"US"},
        {"ticker":"GME","name":"GameStop","sector":"Consumer","industry":"Gaming Retail","country":"US"},
        {"ticker":"AMC","name":"AMC Entertainment","sector":"Consumer","industry":"Entertainment","country":"US"},
        {"ticker":"WMT","name":"Walmart","sector":"Consumer","industry":"Retail","country":"US"},
        {"ticker":"TGT","name":"Target","sector":"Consumer","industry":"Retail","country":"US"},
        {"ticker":"COST","name":"Costco","sector":"Consumer","industry":"Retail","country":"US"},
        {"ticker":"RIVN","name":"Rivian","sector":"Consumer","industry":"EV","country":"US"},
        {"ticker":"LCID","name":"Lucid Group","sector":"Consumer","industry":"EV","country":"US"},
        {"ticker":"NIO","name":"NIO Inc","sector":"Consumer","industry":"EV","country":"CN"},
        {"ticker":"LI","name":"Li Auto","sector":"Consumer","industry":"EV","country":"CN"},
        {"ticker":"XPEV","name":"XPeng","sector":"Consumer","industry":"EV","country":"CN"},
        {"ticker":"F","name":"Ford Motor","sector":"Consumer","industry":"Auto","country":"US"},
        {"ticker":"GM","name":"General Motors","sector":"Consumer","industry":"Auto","country":"US"},
        {"ticker":"UBER","name":"Uber Technologies","sector":"Consumer","industry":"Rideshare","country":"US"},
        {"ticker":"LYFT","name":"Lyft","sector":"Consumer","industry":"Rideshare","country":"US"},
        {"ticker":"ABNB","name":"Airbnb","sector":"Consumer","industry":"Travel","country":"US"},
        {"ticker":"BKNG","name":"Booking Holdings","sector":"Consumer","industry":"Travel","country":"US"},

        # ── US FINANCE ────────────────────────────────────────
        {"ticker":"JPM","name":"JPMorgan Chase","sector":"Finance","industry":"Banking","country":"US"},
        {"ticker":"GS","name":"Goldman Sachs","sector":"Finance","industry":"Investment Banking","country":"US"},
        {"ticker":"MS","name":"Morgan Stanley","sector":"Finance","industry":"Investment Banking","country":"US"},
        {"ticker":"BAC","name":"Bank of America","sector":"Finance","industry":"Banking","country":"US"},
        {"ticker":"WFC","name":"Wells Fargo","sector":"Finance","industry":"Banking","country":"US"},
        {"ticker":"C","name":"Citigroup","sector":"Finance","industry":"Banking","country":"US"},
        {"ticker":"V","name":"Visa","sector":"Finance","industry":"Payments","country":"US"},
        {"ticker":"MA","name":"Mastercard","sector":"Finance","industry":"Payments","country":"US"},
        {"ticker":"PYPL","name":"PayPal","sector":"Finance","industry":"Fintech","country":"US"},
        {"ticker":"SQ","name":"Block Inc","sector":"Finance","industry":"Fintech","country":"US"},
        {"ticker":"SOFI","name":"SoFi Technologies","sector":"Finance","industry":"Fintech","country":"US"},
        {"ticker":"AFRM","name":"Affirm Holdings","sector":"Finance","industry":"Fintech","country":"US"},
        {"ticker":"COIN","name":"Coinbase","sector":"Finance","industry":"Crypto Exchange","country":"US"},
        {"ticker":"MSTR","name":"MicroStrategy","sector":"Finance","industry":"BTC Treasury","country":"US"},
        {"ticker":"HOOD","name":"Robinhood","sector":"Finance","industry":"Fintech","country":"US"},
        {"ticker":"SCHW","name":"Charles Schwab","sector":"Finance","industry":"Brokerage","country":"US"},
        {"ticker":"BLK","name":"BlackRock","sector":"Finance","industry":"Asset Management","country":"US"},
        {"ticker":"AXP","name":"American Express","sector":"Finance","industry":"Payments","country":"US"},

        # ── US HEALTHCARE ─────────────────────────────────────
        {"ticker":"JNJ","name":"Johnson & Johnson","sector":"Healthcare","industry":"Pharma","country":"US"},
        {"ticker":"PFE","name":"Pfizer","sector":"Healthcare","industry":"Pharma","country":"US"},
        {"ticker":"MRK","name":"Merck","sector":"Healthcare","industry":"Pharma","country":"US"},
        {"ticker":"ABBV","name":"AbbVie","sector":"Healthcare","industry":"Pharma","country":"US"},
        {"ticker":"LLY","name":"Eli Lilly","sector":"Healthcare","industry":"Pharma","country":"US"},
        {"ticker":"BMY","name":"Bristol-Myers Squibb","sector":"Healthcare","industry":"Pharma","country":"US"},
        {"ticker":"MRNA","name":"Moderna","sector":"Healthcare","industry":"Biotech","country":"US"},
        {"ticker":"BIIB","name":"Biogen","sector":"Healthcare","industry":"Biotech","country":"US"},
        {"ticker":"GILD","name":"Gilead Sciences","sector":"Healthcare","industry":"Biotech","country":"US"},
        {"ticker":"REGN","name":"Regeneron","sector":"Healthcare","industry":"Biotech","country":"US"},
        {"ticker":"VRTX","name":"Vertex Pharma","sector":"Healthcare","industry":"Biotech","country":"US"},
        {"ticker":"ISRG","name":"Intuitive Surgical","sector":"Healthcare","industry":"MedTech","country":"US"},
        {"ticker":"SYK","name":"Stryker","sector":"Healthcare","industry":"MedTech","country":"US"},
        {"ticker":"MDT","name":"Medtronic","sector":"Healthcare","industry":"MedTech","country":"US"},
        {"ticker":"DXCM","name":"Dexcom","sector":"Healthcare","industry":"MedTech","country":"US"},
        {"ticker":"NTLA","name":"Intellia Therapeutics","sector":"Healthcare","industry":"CRISPR","country":"US"},
        {"ticker":"CRSP","name":"CRISPR Therapeutics","sector":"Healthcare","industry":"CRISPR","country":"US"},
        {"ticker":"BEAM","name":"Beam Therapeutics","sector":"Healthcare","industry":"Gene Editing","country":"US"},
        {"ticker":"EDIT","name":"Editas Medicine","sector":"Healthcare","industry":"CRISPR","country":"US"},
        {"ticker":"RXRX","name":"Recursion Pharma","sector":"Healthcare","industry":"AI Drug Discovery","country":"US"},
        {"ticker":"UNH","name":"UnitedHealth Group","sector":"Healthcare","industry":"Health Insurance","country":"US"},
        {"ticker":"CVS","name":"CVS Health","sector":"Healthcare","industry":"Pharmacy","country":"US"},
        {"ticker":"HCA","name":"HCA Healthcare","sector":"Healthcare","industry":"Hospitals","country":"US"},

        # ── US ENERGY ─────────────────────────────────────────
        {"ticker":"XOM","name":"ExxonMobil","sector":"Energy","industry":"Oil & Gas","country":"US"},
        {"ticker":"CVX","name":"Chevron","sector":"Energy","industry":"Oil & Gas","country":"US"},
        {"ticker":"COP","name":"ConocoPhillips","sector":"Energy","industry":"Oil & Gas","country":"US"},
        {"ticker":"OXY","name":"Occidental Petroleum","sector":"Energy","industry":"Oil & Gas","country":"US"},
        {"ticker":"SLB","name":"SLB (Schlumberger)","sector":"Energy","industry":"Oilfield Services","country":"US"},
        {"ticker":"FSLR","name":"First Solar","sector":"Energy","industry":"Solar","country":"US"},
        {"ticker":"ENPH","name":"Enphase Energy","sector":"Energy","industry":"Solar","country":"US"},
        {"ticker":"SEDG","name":"SolarEdge Technologies","sector":"Energy","industry":"Solar","country":"US"},
        {"ticker":"NEE","name":"NextEra Energy","sector":"Energy","industry":"Renewables","country":"US"},
        {"ticker":"PLUG","name":"Plug Power","sector":"Energy","industry":"Hydrogen","country":"US"},
        {"ticker":"FCEL","name":"FuelCell Energy","sector":"Energy","industry":"Hydrogen","country":"US"},
        {"ticker":"BE","name":"Bloom Energy","sector":"Energy","industry":"Fuel Cells","country":"US"},
        {"ticker":"HASI","name":"Hannon Armstrong","sector":"Energy","industry":"Clean Energy Finance","country":"US"},
        {"ticker":"CHPT","name":"ChargePoint","sector":"Energy","industry":"EV Charging","country":"US"},
        {"ticker":"BLNK","name":"Blink Charging","sector":"Energy","industry":"EV Charging","country":"US"},
        {"ticker":"EVGO","name":"EVgo","sector":"Energy","industry":"EV Charging","country":"US"},

        # ── NUCLEAR / URANIUM ─────────────────────────────────
        {"ticker":"NNE","name":"Nano Nuclear Energy","sector":"Energy","industry":"Nuclear","country":"US"},
        {"ticker":"CCJ","name":"Cameco","sector":"Energy","industry":"Uranium","country":"CA"},
        {"ticker":"UEC","name":"Uranium Energy","sector":"Energy","industry":"Uranium","country":"US"},
        {"ticker":"DNN","name":"Denison Mines","sector":"Energy","industry":"Uranium","country":"CA"},
        {"ticker":"UUUU","name":"Energy Fuels","sector":"Energy","industry":"Uranium","country":"US"},
        {"ticker":"URG","name":"Ur-Energy","sector":"Energy","industry":"Uranium","country":"CA"},
        {"ticker":"SMR","name":"NuScale Power","sector":"Energy","industry":"Nuclear","country":"US"},
        {"ticker":"OKLO","name":"Oklo Inc","sector":"Energy","industry":"Nuclear","country":"US"},

        # ── SPACE & DEFENCE ───────────────────────────────────
        {"ticker":"RKLB","name":"Rocket Lab USA","sector":"Space","industry":"Launch","country":"US"},
        {"ticker":"ASTS","name":"AST SpaceMobile","sector":"Space","industry":"Satellite","country":"US"},
        {"ticker":"LMT","name":"Lockheed Martin","sector":"Space","industry":"Defence","country":"US"},
        {"ticker":"NOC","name":"Northrop Grumman","sector":"Space","industry":"Defence","country":"US"},
        {"ticker":"RTX","name":"RTX Corporation","sector":"Space","industry":"Defence","country":"US"},
        {"ticker":"BA","name":"Boeing","sector":"Space","industry":"Aerospace","country":"US"},
        {"ticker":"GD","name":"General Dynamics","sector":"Space","industry":"Defence","country":"US"},
        {"ticker":"SPCE","name":"Virgin Galactic","sector":"Space","industry":"Launch","country":"US"},
        {"ticker":"MAXR","name":"Maxar Technologies","sector":"Space","industry":"Satellite","country":"US"},
        {"ticker":"BWXT","name":"BWX Technologies","sector":"Space","industry":"Nuclear Defence","country":"US"},
        {"ticker":"HII","name":"Huntington Ingalls","sector":"Space","industry":"Defence","country":"US"},
        {"ticker":"KTOS","name":"Kratos Defence","sector":"Space","industry":"Defence","country":"US"},
        {"ticker":"AVAV","name":"AeroVironment","sector":"Space","industry":"Drones","country":"US"},
        {"ticker":"ACHR","name":"Archer Aviation","sector":"Space","industry":"eVTOL","country":"US"},
        {"ticker":"JOBY","name":"Joby Aviation","sector":"Space","industry":"eVTOL","country":"US"},

        # ── LITHIUM / CRITICAL MINERALS ───────────────────────
        {"ticker":"ALB","name":"Albemarle","sector":"Materials","industry":"Lithium","country":"US"},
        {"ticker":"LAC","name":"Lithium Americas","sector":"Materials","industry":"Lithium","country":"CA"},
        {"ticker":"SQM","name":"Sociedad Quimica","sector":"Materials","industry":"Lithium","country":"CL"},
        {"ticker":"PLL","name":"Piedmont Lithium","sector":"Materials","industry":"Lithium","country":"US"},
        {"ticker":"LTHM","name":"Livent","sector":"Materials","industry":"Lithium","country":"US"},
        {"ticker":"FCX","name":"Freeport-McMoRan","sector":"Materials","industry":"Copper","country":"US"},
        {"ticker":"NUE","name":"Nucor Steel","sector":"Materials","industry":"Steel","country":"US"},
        {"ticker":"VALE","name":"Vale SA","sector":"Materials","industry":"Iron Ore","country":"BR"},
        {"ticker":"CLF","name":"Cleveland-Cliffs","sector":"Materials","industry":"Steel","country":"US"},
        {"ticker":"MP","name":"MP Materials","sector":"Materials","industry":"Rare Earth","country":"US"},

        # ── INDUSTRIALS ───────────────────────────────────────
        {"ticker":"DE","name":"John Deere","sector":"Industrials","industry":"Machinery","country":"US"},
        {"ticker":"CAT","name":"Caterpillar","sector":"Industrials","industry":"Machinery","country":"US"},
        {"ticker":"HON","name":"Honeywell","sector":"Industrials","industry":"Diversified","country":"US"},
        {"ticker":"GE","name":"GE Aerospace","sector":"Industrials","industry":"Aerospace","country":"US"},
        {"ticker":"MMM","name":"3M","sector":"Industrials","industry":"Diversified","country":"US"},
        {"ticker":"UPS","name":"UPS","sector":"Industrials","industry":"Logistics","country":"US"},
        {"ticker":"FDX","name":"FedEx","sector":"Industrials","industry":"Logistics","country":"US"},

        # ── UK STOCKS (FTSE) ──────────────────────────────────
        {"ticker":"BP.L","name":"BP plc","sector":"Energy","industry":"Oil & Gas","country":"GB","exchange":"LSE","currency":"GBP"},
        {"ticker":"SHEL.L","name":"Shell plc","sector":"Energy","industry":"Oil & Gas","country":"GB","exchange":"LSE","currency":"GBP"},
        {"ticker":"HSBA.L","name":"HSBC Holdings","sector":"Finance","industry":"Banking","country":"GB","exchange":"LSE","currency":"GBP"},
        {"ticker":"LLOY.L","name":"Lloyds Banking Group","sector":"Finance","industry":"Banking","country":"GB","exchange":"LSE","currency":"GBP"},
        {"ticker":"BARC.L","name":"Barclays","sector":"Finance","industry":"Banking","country":"GB","exchange":"LSE","currency":"GBP"},
        {"ticker":"NWG.L","name":"NatWest Group","sector":"Finance","industry":"Banking","country":"GB","exchange":"LSE","currency":"GBP"},
        {"ticker":"STAN.L","name":"Standard Chartered","sector":"Finance","industry":"Banking","country":"GB","exchange":"LSE","currency":"GBP"},
        {"ticker":"VOD.L","name":"Vodafone","sector":"Technology","industry":"Telecom","country":"GB","exchange":"LSE","currency":"GBP"},
        {"ticker":"BT-A.L","name":"BT Group","sector":"Technology","industry":"Telecom","country":"GB","exchange":"LSE","currency":"GBP"},
        {"ticker":"AZN.L","name":"AstraZeneca","sector":"Healthcare","industry":"Pharma","country":"GB","exchange":"LSE","currency":"GBP"},
        {"ticker":"GSK.L","name":"GSK plc","sector":"Healthcare","industry":"Pharma","country":"GB","exchange":"LSE","currency":"GBP"},
        {"ticker":"ULVR.L","name":"Unilever","sector":"Consumer","industry":"FMCG","country":"GB","exchange":"LSE","currency":"GBP"},
        {"ticker":"DGE.L","name":"Diageo","sector":"Consumer","industry":"Beverages","country":"GB","exchange":"LSE","currency":"GBP"},
        {"ticker":"REL.L","name":"RELX","sector":"Technology","industry":"Data & Analytics","country":"GB","exchange":"LSE","currency":"GBP"},
        {"ticker":"RIO.L","name":"Rio Tinto","sector":"Materials","industry":"Mining","country":"GB","exchange":"LSE","currency":"GBP"},
        {"ticker":"BHP.L","name":"BHP Group","sector":"Materials","industry":"Mining","country":"AU","exchange":"LSE","currency":"GBP"},
        {"ticker":"AAL.L","name":"Anglo American","sector":"Materials","industry":"Mining","country":"GB","exchange":"LSE","currency":"GBP"},
        {"ticker":"GLEN.L","name":"Glencore","sector":"Materials","industry":"Mining","country":"GB","exchange":"LSE","currency":"GBP"},
        {"ticker":"RR.L","name":"Rolls-Royce","sector":"Industrials","industry":"Aerospace","country":"GB","exchange":"LSE","currency":"GBP"},
        {"ticker":"BA.L","name":"BAE Systems","sector":"Space","industry":"Defence","country":"GB","exchange":"LSE","currency":"GBP"},
        {"ticker":"IAG.L","name":"IAG","sector":"Consumer","industry":"Airlines","country":"GB","exchange":"LSE","currency":"GBP"},
        {"ticker":"EXPN.L","name":"Experian","sector":"Finance","industry":"Data & Analytics","country":"GB","exchange":"LSE","currency":"GBP"},
        {"ticker":"LSEG.L","name":"London Stock Exchange Group","sector":"Finance","industry":"Exchange","country":"GB","exchange":"LSE","currency":"GBP"},
        {"ticker":"PRU.L","name":"Prudential","sector":"Finance","industry":"Insurance","country":"GB","exchange":"LSE","currency":"GBP"},
        {"ticker":"TSCO.L","name":"Tesco","sector":"Consumer","industry":"Retail","country":"GB","exchange":"LSE","currency":"GBP"},
        {"ticker":"MKS.L","name":"Marks & Spencer","sector":"Consumer","industry":"Retail","country":"GB","exchange":"LSE","currency":"GBP"},
        {"ticker":"SBRY.L","name":"Sainsbury's","sector":"Consumer","industry":"Retail","country":"GB","exchange":"LSE","currency":"GBP"},
        {"ticker":"AUTO.L","name":"Auto Trader","sector":"Technology","industry":"Marketplace","country":"GB","exchange":"LSE","currency":"GBP"},
        {"ticker":"WISE.L","name":"Wise","sector":"Finance","industry":"Fintech","country":"GB","exchange":"LSE","currency":"GBP"},
        {"ticker":"III.L","name":"3i Group","sector":"Finance","industry":"Private Equity","country":"GB","exchange":"LSE","currency":"GBP"},
        {"ticker":"CNA.L","name":"Centrica","sector":"Energy","industry":"Gas","country":"GB","exchange":"LSE","currency":"GBP"},
        {"ticker":"SSE.L","name":"SSE plc","sector":"Energy","industry":"Renewables","country":"GB","exchange":"LSE","currency":"GBP"},
        {"ticker":"NG.L","name":"National Grid","sector":"Energy","industry":"Utilities","country":"GB","exchange":"LSE","currency":"GBP"},
        {"ticker":"SGRO.L","name":"Segro","sector":"Real Estate","industry":"Logistics REIT","country":"GB","exchange":"LSE","currency":"GBP"},
        {"ticker":"LAND.L","name":"Land Securities","sector":"Real Estate","industry":"REIT","country":"GB","exchange":"LSE","currency":"GBP"},
        {"ticker":"SVT.L","name":"Severn Trent","sector":"Industrials","industry":"Water","country":"GB","exchange":"LSE","currency":"GBP"},

        # ── AGRICULTURE ───────────────────────────────────────
        {"ticker":"ADM","name":"Archer-Daniels-Midland","sector":"Agriculture","industry":"Grain","country":"US"},
        {"ticker":"BG","name":"Bunge Global","sector":"Agriculture","industry":"Grain","country":"US"},
        {"ticker":"MOS","name":"The Mosaic Company","sector":"Agriculture","industry":"Fertilisers","country":"US"},
        {"ticker":"NTR","name":"Nutrien","sector":"Agriculture","industry":"Fertilisers","country":"CA"},
        {"ticker":"CF","name":"CF Industries","sector":"Agriculture","industry":"Fertilisers","country":"US"},
        {"ticker":"CTVA","name":"Corteva","sector":"Agriculture","industry":"Seeds","country":"US"},
        {"ticker":"FMC","name":"FMC Corporation","sector":"Agriculture","industry":"Agrochemicals","country":"US"},

        # ── REAL ESTATE ───────────────────────────────────────
        {"ticker":"AMT","name":"American Tower","sector":"Real Estate","industry":"Data Centre REIT","country":"US"},
        {"ticker":"EQIX","name":"Equinix","sector":"Real Estate","industry":"Data Centre REIT","country":"US"},
        {"ticker":"PLD","name":"Prologis","sector":"Real Estate","industry":"Logistics REIT","country":"US"},
        {"ticker":"SPG","name":"Simon Property Group","sector":"Real Estate","industry":"Retail REIT","country":"US"},
        {"ticker":"O","name":"Realty Income","sector":"Real Estate","industry":"Net Lease REIT","country":"US"},
        {"ticker":"VICI","name":"VICI Properties","sector":"Real Estate","industry":"Gaming REIT","country":"US"},

        # ── CRYPTO PROXIES ────────────────────────────────────
        {"ticker":"IBIT","name":"iShares Bitcoin Trust","sector":"Crypto","industry":"Bitcoin ETF","country":"US"},
        {"ticker":"FBTC","name":"Fidelity Bitcoin Fund","sector":"Crypto","industry":"Bitcoin ETF","country":"US"},
        {"ticker":"GBTC","name":"Grayscale Bitcoin Trust","sector":"Crypto","industry":"Bitcoin ETF","country":"US"},
        {"ticker":"RIOT","name":"Riot Platforms","sector":"Crypto","industry":"Bitcoin Mining","country":"US"},
        {"ticker":"MARA","name":"Marathon Digital","sector":"Crypto","industry":"Bitcoin Mining","country":"US"},
        {"ticker":"CLSK","name":"CleanSpark","sector":"Crypto","industry":"Bitcoin Mining","country":"US"},
        {"ticker":"HUT","name":"Hut 8 Corp","sector":"Crypto","industry":"Bitcoin Mining","country":"CA"},
        {"ticker":"CIFR","name":"Cipher Mining","sector":"Crypto","industry":"Bitcoin Mining","country":"US"},
    ]

    async def fetch(self) -> List[dict]:
        log.info(f"Loading {len(self.SEEDS)} static seed assets")
        for seed in self.SEEDS:
            seed["source"] = "static_seed"
            if "quote_type" not in seed:
                seed["quote_type"] = "EQUITY"
        return self.SEEDS
