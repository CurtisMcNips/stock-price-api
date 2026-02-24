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

        # ── REMAINING FTSE 100 ────────────────────────────────
        {"ticker":"ABF.L","name":"Associated British Foods","sector":"Consumer","industry":"Food","country":"GB","exchange":"LSE","currency":"GBP"},
        {"ticker":"ADM.L","name":"Admiral Group","sector":"Finance","industry":"Insurance","country":"GB","exchange":"LSE","currency":"GBP"},
        {"ticker":"AHT.L","name":"Ashtead Group","sector":"Industrials","industry":"Equipment Rental","country":"GB","exchange":"LSE","currency":"GBP"},
        {"ticker":"ANTO.L","name":"Antofagasta","sector":"Materials","industry":"Copper Mining","country":"CL","exchange":"LSE","currency":"GBP"},
        {"ticker":"AV.L","name":"Aviva","sector":"Finance","industry":"Insurance","country":"GB","exchange":"LSE","currency":"GBP"},
        {"ticker":"BATS.L","name":"British American Tobacco","sector":"Consumer","industry":"Tobacco","country":"GB","exchange":"LSE","currency":"GBP"},
        {"ticker":"BLND.L","name":"British Land","sector":"Real Estate","industry":"REIT","country":"GB","exchange":"LSE","currency":"GBP"},
        {"ticker":"BVIC.L","name":"Britvic","sector":"Consumer","industry":"Beverages","country":"GB","exchange":"LSE","currency":"GBP"},
        {"ticker":"CPG.L","name":"Compass Group","sector":"Consumer","industry":"Catering","country":"GB","exchange":"LSE","currency":"GBP"},
        {"ticker":"CRH.L","name":"CRH plc","sector":"Industrials","industry":"Building Materials","country":"IE","exchange":"LSE","currency":"GBP"},
        {"ticker":"EZJ.L","name":"easyJet","sector":"Consumer","industry":"Airlines","country":"GB","exchange":"LSE","currency":"GBP"},
        {"ticker":"FERG.L","name":"Ferguson Enterprises","sector":"Industrials","industry":"Distribution","country":"GB","exchange":"LSE","currency":"GBP"},
        {"ticker":"FLTR.L","name":"Flutter Entertainment","sector":"Consumer","industry":"Gambling","country":"IE","exchange":"LSE","currency":"GBP"},
        {"ticker":"FRES.L","name":"Fresnillo","sector":"Materials","industry":"Silver Mining","country":"MX","exchange":"LSE","currency":"GBP"},
        {"ticker":"HIK.L","name":"Hikma Pharmaceuticals","sector":"Healthcare","industry":"Generics","country":"JO","exchange":"LSE","currency":"GBP"},
        {"ticker":"HL.L","name":"Hargreaves Lansdown","sector":"Finance","industry":"Wealth Management","country":"GB","exchange":"LSE","currency":"GBP"},
        {"ticker":"IMB.L","name":"Imperial Brands","sector":"Consumer","industry":"Tobacco","country":"GB","exchange":"LSE","currency":"GBP"},
        {"ticker":"INF.L","name":"Informa","sector":"Technology","industry":"Publishing","country":"GB","exchange":"LSE","currency":"GBP"},
        {"ticker":"ITRK.L","name":"Intertek Group","sector":"Industrials","industry":"Testing","country":"GB","exchange":"LSE","currency":"GBP"},
        {"ticker":"JD.L","name":"JD Sports","sector":"Consumer","industry":"Retail","country":"GB","exchange":"LSE","currency":"GBP"},
        {"ticker":"KGF.L","name":"Kingfisher","sector":"Consumer","industry":"DIY Retail","country":"GB","exchange":"LSE","currency":"GBP"},
        {"ticker":"LGEN.L","name":"Legal & General","sector":"Finance","industry":"Insurance","country":"GB","exchange":"LSE","currency":"GBP"},
        {"ticker":"MNDI.L","name":"Mondi","sector":"Materials","industry":"Packaging","country":"GB","exchange":"LSE","currency":"GBP"},
        {"ticker":"MNG.L","name":"M&G plc","sector":"Finance","industry":"Asset Management","country":"GB","exchange":"LSE","currency":"GBP"},
        {"ticker":"NXT.L","name":"Next plc","sector":"Consumer","industry":"Retail","country":"GB","exchange":"LSE","currency":"GBP"},
        {"ticker":"OCDO.L","name":"Ocado Group","sector":"Technology","industry":"E-Commerce","country":"GB","exchange":"LSE","currency":"GBP"},
        {"ticker":"PSN.L","name":"Persimmon","sector":"Real Estate","industry":"Housebuilding","country":"GB","exchange":"LSE","currency":"GBP"},
        {"ticker":"PSON.L","name":"Pearson","sector":"Technology","industry":"Education","country":"GB","exchange":"LSE","currency":"GBP"},
        {"ticker":"RKT.L","name":"Reckitt Benckiser","sector":"Consumer","industry":"FMCG","country":"GB","exchange":"LSE","currency":"GBP"},
        {"ticker":"RMV.L","name":"Rightmove","sector":"Technology","industry":"Marketplace","country":"GB","exchange":"LSE","currency":"GBP"},
        {"ticker":"SGE.L","name":"Sage Group","sector":"Technology","industry":"Software","country":"GB","exchange":"LSE","currency":"GBP"},
        {"ticker":"SKG.L","name":"Smurfit Kappa","sector":"Materials","industry":"Packaging","country":"IE","exchange":"LSE","currency":"GBP"},
        {"ticker":"SMT.L","name":"Scottish Mortgage IT","sector":"Finance","industry":"Investment Trust","country":"GB","exchange":"LSE","currency":"GBP"},
        {"ticker":"SN.L","name":"Smith & Nephew","sector":"Healthcare","industry":"MedTech","country":"GB","exchange":"LSE","currency":"GBP"},
        {"ticker":"SMIN.L","name":"Smiths Group","sector":"Industrials","industry":"Diversified","country":"GB","exchange":"LSE","currency":"GBP"},
        {"ticker":"SPX.L","name":"Spirax-Sarco","sector":"Industrials","industry":"Engineering","country":"GB","exchange":"LSE","currency":"GBP"},
        {"ticker":"STJ.L","name":"St. James Place","sector":"Finance","industry":"Wealth Management","country":"GB","exchange":"LSE","currency":"GBP"},
        {"ticker":"TW.L","name":"Taylor Wimpey","sector":"Real Estate","industry":"Housebuilding","country":"GB","exchange":"LSE","currency":"GBP"},
        {"ticker":"UU.L","name":"United Utilities","sector":"Industrials","industry":"Water","country":"GB","exchange":"LSE","currency":"GBP"},
        {"ticker":"WPP.L","name":"WPP plc","sector":"Technology","industry":"Advertising","country":"GB","exchange":"LSE","currency":"GBP"},
        {"ticker":"WTB.L","name":"Whitbread","sector":"Consumer","industry":"Hotels","country":"GB","exchange":"LSE","currency":"GBP"},
        {"ticker":"MNDI.L","name":"Mondi","sector":"Materials","industry":"Packaging","country":"GB","exchange":"LSE","currency":"GBP"},

        # ── EUROPEAN BLUE CHIPS ───────────────────────────────
        {"ticker":"SAP.DE","name":"SAP SE","sector":"Technology","industry":"Software","country":"DE","exchange":"XETRA","currency":"EUR"},
        {"ticker":"SIE.DE","name":"Siemens AG","sector":"Industrials","industry":"Engineering","country":"DE","exchange":"XETRA","currency":"EUR"},
        {"ticker":"ALV.DE","name":"Allianz SE","sector":"Finance","industry":"Insurance","country":"DE","exchange":"XETRA","currency":"EUR"},
        {"ticker":"MUV2.DE","name":"Munich Re","sector":"Finance","industry":"Reinsurance","country":"DE","exchange":"XETRA","currency":"EUR"},
        {"ticker":"DBK.DE","name":"Deutsche Bank","sector":"Finance","industry":"Banking","country":"DE","exchange":"XETRA","currency":"EUR"},
        {"ticker":"BMW.DE","name":"BMW AG","sector":"Consumer","industry":"Auto","country":"DE","exchange":"XETRA","currency":"EUR"},
        {"ticker":"MBG.DE","name":"Mercedes-Benz","sector":"Consumer","industry":"Auto","country":"DE","exchange":"XETRA","currency":"EUR"},
        {"ticker":"VOW3.DE","name":"Volkswagen AG","sector":"Consumer","industry":"Auto","country":"DE","exchange":"XETRA","currency":"EUR"},
        {"ticker":"BAYN.DE","name":"Bayer AG","sector":"Healthcare","industry":"Pharma","country":"DE","exchange":"XETRA","currency":"EUR"},
        {"ticker":"BASF.DE","name":"BASF SE","sector":"Materials","industry":"Chemicals","country":"DE","exchange":"XETRA","currency":"EUR"},
        {"ticker":"DTE.DE","name":"Deutsche Telekom","sector":"Technology","industry":"Telecom","country":"DE","exchange":"XETRA","currency":"EUR"},
        {"ticker":"RWE.DE","name":"RWE AG","sector":"Energy","industry":"Renewables","country":"DE","exchange":"XETRA","currency":"EUR"},
        {"ticker":"ADS.DE","name":"Adidas AG","sector":"Consumer","industry":"Apparel","country":"DE","exchange":"XETRA","currency":"EUR"},
        {"ticker":"MRK.DE","name":"Merck KGaA","sector":"Healthcare","industry":"Pharma","country":"DE","exchange":"XETRA","currency":"EUR"},
        {"ticker":"EOAN.DE","name":"E.ON SE","sector":"Energy","industry":"Utilities","country":"DE","exchange":"XETRA","currency":"EUR"},
        {"ticker":"OR.PA","name":"LOreal","sector":"Consumer","industry":"Beauty","country":"FR","exchange":"EURONEXT","currency":"EUR"},
        {"ticker":"MC.PA","name":"LVMH","sector":"Consumer","industry":"Luxury","country":"FR","exchange":"EURONEXT","currency":"EUR"},
        {"ticker":"RMS.PA","name":"Hermes","sector":"Consumer","industry":"Luxury","country":"FR","exchange":"EURONEXT","currency":"EUR"},
        {"ticker":"TTE.PA","name":"TotalEnergies","sector":"Energy","industry":"Oil & Gas","country":"FR","exchange":"EURONEXT","currency":"EUR"},
        {"ticker":"BNP.PA","name":"BNP Paribas","sector":"Finance","industry":"Banking","country":"FR","exchange":"EURONEXT","currency":"EUR"},
        {"ticker":"SAN.PA","name":"Sanofi","sector":"Healthcare","industry":"Pharma","country":"FR","exchange":"EURONEXT","currency":"EUR"},
        {"ticker":"AIR.PA","name":"Airbus SE","sector":"Industrials","industry":"Aerospace","country":"FR","exchange":"EURONEXT","currency":"EUR"},
        {"ticker":"AXA.PA","name":"AXA SA","sector":"Finance","industry":"Insurance","country":"FR","exchange":"EURONEXT","currency":"EUR"},
        {"ticker":"HEIA.AS","name":"Heineken","sector":"Consumer","industry":"Beverages","country":"NL","exchange":"EURONEXT","currency":"EUR"},
        {"ticker":"INGA.AS","name":"ING Group","sector":"Finance","industry":"Banking","country":"NL","exchange":"EURONEXT","currency":"EUR"},
        {"ticker":"ADYEN.AS","name":"Adyen","sector":"Finance","industry":"Fintech","country":"NL","exchange":"EURONEXT","currency":"EUR"},
        {"ticker":"NESN.SW","name":"Nestle SA","sector":"Consumer","industry":"Food","country":"CH","exchange":"SIX","currency":"CHF"},
        {"ticker":"ROG.SW","name":"Roche Holding","sector":"Healthcare","industry":"Pharma","country":"CH","exchange":"SIX","currency":"CHF"},
        {"ticker":"NOVN.SW","name":"Novartis AG","sector":"Healthcare","industry":"Pharma","country":"CH","exchange":"SIX","currency":"CHF"},
        {"ticker":"UBSG.SW","name":"UBS Group","sector":"Finance","industry":"Banking","country":"CH","exchange":"SIX","currency":"CHF"},
        {"ticker":"NOVOB.CO","name":"Novo Nordisk","sector":"Healthcare","industry":"Pharma","country":"DK","exchange":"OMXC","currency":"DKK"},
        {"ticker":"ERIC-B.ST","name":"Ericsson","sector":"Technology","industry":"Telecom","country":"SE","exchange":"OMXS","currency":"SEK"},
        {"ticker":"VOLV-B.ST","name":"Volvo AB","sector":"Industrials","industry":"Trucks","country":"SE","exchange":"OMXS","currency":"SEK"},

        # ── APAC ──────────────────────────────────────────────
        {"ticker":"7203.T","name":"Toyota Motor","sector":"Consumer","industry":"Auto","country":"JP","exchange":"TSE","currency":"JPY"},
        {"ticker":"6758.T","name":"Sony Group","sector":"Technology","industry":"Electronics","country":"JP","exchange":"TSE","currency":"JPY"},
        {"ticker":"9984.T","name":"SoftBank Group","sector":"Finance","industry":"Investment","country":"JP","exchange":"TSE","currency":"JPY"},
        {"ticker":"7974.T","name":"Nintendo","sector":"Consumer","industry":"Gaming","country":"JP","exchange":"TSE","currency":"JPY"},
        {"ticker":"6861.T","name":"Keyence","sector":"Technology","industry":"Automation","country":"JP","exchange":"TSE","currency":"JPY"},
        {"ticker":"8306.T","name":"Mitsubishi UFJ","sector":"Finance","industry":"Banking","country":"JP","exchange":"TSE","currency":"JPY"},
        {"ticker":"BHP.AX","name":"BHP Group ASX","sector":"Materials","industry":"Mining","country":"AU","exchange":"ASX","currency":"AUD"},
        {"ticker":"CBA.AX","name":"Commonwealth Bank","sector":"Finance","industry":"Banking","country":"AU","exchange":"ASX","currency":"AUD"},
        {"ticker":"CSL.AX","name":"CSL Limited","sector":"Healthcare","industry":"Biotech","country":"AU","exchange":"ASX","currency":"AUD"},
        {"ticker":"RIO.AX","name":"Rio Tinto ASX","sector":"Materials","industry":"Mining","country":"AU","exchange":"ASX","currency":"AUD"},
        {"ticker":"WBC.AX","name":"Westpac Banking","sector":"Finance","industry":"Banking","country":"AU","exchange":"ASX","currency":"AUD"},
        {"ticker":"005930.KS","name":"Samsung Electronics","sector":"Technology","industry":"Semiconductors","country":"KR","exchange":"KRX","currency":"KRW"},
        {"ticker":"BABA","name":"Alibaba Group","sector":"Technology","industry":"E-Commerce","country":"CN"},
        {"ticker":"JD","name":"JD.com","sector":"Technology","industry":"E-Commerce","country":"CN"},
        {"ticker":"PDD","name":"PDD Holdings","sector":"Technology","industry":"E-Commerce","country":"CN"},
        {"ticker":"BIDU","name":"Baidu","sector":"Technology","industry":"Search","country":"CN"},
        {"ticker":"TCEHY","name":"Tencent Holdings","sector":"Technology","industry":"Social Media","country":"CN"},
        {"ticker":"NTES","name":"NetEase","sector":"Technology","industry":"Gaming","country":"CN"},

        # ── S&P 500 GAP FILLS ─────────────────────────────────
        {"ticker":"GOOG","name":"Alphabet Class C","sector":"Technology","industry":"Software","country":"US"},
        {"ticker":"BRK-B","name":"Berkshire Hathaway B","sector":"Finance","industry":"Conglomerate","country":"US"},
        {"ticker":"LIN","name":"Linde plc","sector":"Materials","industry":"Chemicals","country":"US"},
        {"ticker":"PG","name":"Procter & Gamble","sector":"Consumer","industry":"FMCG","country":"US"},
        {"ticker":"KO","name":"Coca-Cola","sector":"Consumer","industry":"Beverages","country":"US"},
        {"ticker":"PEP","name":"PepsiCo","sector":"Consumer","industry":"Beverages","country":"US"},
        {"ticker":"PM","name":"Philip Morris","sector":"Consumer","industry":"Tobacco","country":"US"},
        {"ticker":"MO","name":"Altria Group","sector":"Consumer","industry":"Tobacco","country":"US"},
        {"ticker":"CL","name":"Colgate-Palmolive","sector":"Consumer","industry":"FMCG","country":"US"},
        {"ticker":"GIS","name":"General Mills","sector":"Consumer","industry":"Food","country":"US"},
        {"ticker":"MDLZ","name":"Mondelez International","sector":"Consumer","industry":"Food","country":"US"},
        {"ticker":"HSY","name":"Hershey Company","sector":"Consumer","industry":"Food","country":"US"},
        {"ticker":"STZ","name":"Constellation Brands","sector":"Consumer","industry":"Beverages","country":"US"},
        {"ticker":"HD","name":"Home Depot","sector":"Consumer","industry":"Home Improvement","country":"US"},
        {"ticker":"LOW","name":"Lowes Companies","sector":"Consumer","industry":"Home Improvement","country":"US"},
        {"ticker":"TJX","name":"TJX Companies","sector":"Consumer","industry":"Retail","country":"US"},
        {"ticker":"EBAY","name":"eBay","sector":"Consumer","industry":"E-Commerce","country":"US"},
        {"ticker":"ETSY","name":"Etsy","sector":"Consumer","industry":"E-Commerce","country":"US"},
        {"ticker":"DASH","name":"DoorDash","sector":"Consumer","industry":"Delivery","country":"US"},
        {"ticker":"YUM","name":"Yum Brands","sector":"Consumer","industry":"Restaurants","country":"US"},
        {"ticker":"DPZ","name":"Dominos Pizza","sector":"Consumer","industry":"Restaurants","country":"US"},
        {"ticker":"MAR","name":"Marriott International","sector":"Consumer","industry":"Hotels","country":"US"},
        {"ticker":"HLT","name":"Hilton Worldwide","sector":"Consumer","industry":"Hotels","country":"US"},
        {"ticker":"CCL","name":"Carnival Corporation","sector":"Consumer","industry":"Cruise","country":"US"},
        {"ticker":"RCL","name":"Royal Caribbean","sector":"Consumer","industry":"Cruise","country":"US"},
        {"ticker":"UAL","name":"United Airlines","sector":"Consumer","industry":"Airlines","country":"US"},
        {"ticker":"DAL","name":"Delta Air Lines","sector":"Consumer","industry":"Airlines","country":"US"},
        {"ticker":"AAL","name":"American Airlines","sector":"Consumer","industry":"Airlines","country":"US"},
        {"ticker":"LUV","name":"Southwest Airlines","sector":"Consumer","industry":"Airlines","country":"US"},
        {"ticker":"WBA","name":"Walgreens Boots","sector":"Healthcare","industry":"Pharmacy","country":"US"},
        {"ticker":"CI","name":"Cigna Group","sector":"Healthcare","industry":"Health Insurance","country":"US"},
        {"ticker":"ELV","name":"Elevance Health","sector":"Healthcare","industry":"Health Insurance","country":"US"},
        {"ticker":"HUM","name":"Humana","sector":"Healthcare","industry":"Health Insurance","country":"US"},
        {"ticker":"TMO","name":"Thermo Fisher","sector":"Healthcare","industry":"Lab Equipment","country":"US"},
        {"ticker":"DHR","name":"Danaher","sector":"Healthcare","industry":"Lab Equipment","country":"US"},
        {"ticker":"BSX","name":"Boston Scientific","sector":"Healthcare","industry":"MedTech","country":"US"},
        {"ticker":"ILMN","name":"Illumina","sector":"Healthcare","industry":"Genomics","country":"US"},
        {"ticker":"ALNY","name":"Alnylam Pharmaceuticals","sector":"Healthcare","industry":"Biotech","country":"US"},
        {"ticker":"INCY","name":"Incyte Corporation","sector":"Healthcare","industry":"Biotech","country":"US"},
        {"ticker":"BMRN","name":"BioMarin Pharmaceutical","sector":"Healthcare","industry":"Rare Disease","country":"US"},
        {"ticker":"USB","name":"US Bancorp","sector":"Finance","industry":"Banking","country":"US"},
        {"ticker":"PNC","name":"PNC Financial","sector":"Finance","industry":"Banking","country":"US"},
        {"ticker":"TFC","name":"Truist Financial","sector":"Finance","industry":"Banking","country":"US"},
        {"ticker":"COF","name":"Capital One","sector":"Finance","industry":"Banking","country":"US"},
        {"ticker":"ICE","name":"Intercontinental Exchange","sector":"Finance","industry":"Exchange","country":"US"},
        {"ticker":"CME","name":"CME Group","sector":"Finance","industry":"Exchange","country":"US"},
        {"ticker":"MSCI","name":"MSCI Inc","sector":"Finance","industry":"Data & Analytics","country":"US"},
        {"ticker":"SPGI","name":"SP Global","sector":"Finance","industry":"Data & Analytics","country":"US"},
        {"ticker":"MCO","name":"Moodys Corporation","sector":"Finance","industry":"Data & Analytics","country":"US"},
        {"ticker":"MMC","name":"Marsh McLennan","sector":"Finance","industry":"Insurance Broker","country":"US"},
        {"ticker":"AON","name":"Aon plc","sector":"Finance","industry":"Insurance Broker","country":"GB"},
        {"ticker":"MET","name":"MetLife","sector":"Finance","industry":"Insurance","country":"US"},
        {"ticker":"AFL","name":"Aflac","sector":"Finance","industry":"Insurance","country":"US"},
        {"ticker":"PGR","name":"Progressive Corporation","sector":"Finance","industry":"Insurance","country":"US"},
        {"ticker":"CB","name":"Chubb Limited","sector":"Finance","industry":"Insurance","country":"CH"},
        {"ticker":"TROW","name":"T Rowe Price","sector":"Finance","industry":"Asset Management","country":"US"},
        {"ticker":"BLK","name":"BlackRock","sector":"Finance","industry":"Asset Management","country":"US"},
        {"ticker":"LHX","name":"L3Harris Technologies","sector":"Space","industry":"Defence","country":"US"},
        {"ticker":"TDG","name":"TransDigm Group","sector":"Industrials","industry":"Aerospace","country":"US"},
        {"ticker":"ETN","name":"Eaton Corporation","sector":"Industrials","industry":"Power Mgmt","country":"IE"},
        {"ticker":"EMR","name":"Emerson Electric","sector":"Industrials","industry":"Automation","country":"US"},
        {"ticker":"ROK","name":"Rockwell Automation","sector":"Industrials","industry":"Automation","country":"US"},
        {"ticker":"ITW","name":"Illinois Tool Works","sector":"Industrials","industry":"Diversified","country":"US"},
        {"ticker":"PH","name":"Parker Hannifin","sector":"Industrials","industry":"Motion Control","country":"US"},
        {"ticker":"GWW","name":"WW Grainger","sector":"Industrials","industry":"Distribution","country":"US"},
        {"ticker":"FAST","name":"Fastenal","sector":"Industrials","industry":"Distribution","country":"US"},
        {"ticker":"CAT","name":"Caterpillar","sector":"Industrials","industry":"Machinery","country":"US"},
        {"ticker":"HON","name":"Honeywell","sector":"Industrials","industry":"Diversified","country":"US"},
        {"ticker":"GE","name":"GE Aerospace","sector":"Industrials","industry":"Aerospace","country":"US"},
        {"ticker":"UPS","name":"UPS","sector":"Industrials","industry":"Logistics","country":"US"},
        {"ticker":"FDX","name":"FedEx","sector":"Industrials","industry":"Logistics","country":"US"},
        {"ticker":"CSCO","name":"Cisco Systems","sector":"Technology","industry":"Networking","country":"US"},
        {"ticker":"ANET","name":"Arista Networks","sector":"Technology","industry":"Networking","country":"US"},
        {"ticker":"HPE","name":"Hewlett Packard Enterprise","sector":"Technology","industry":"Servers","country":"US"},
        {"ticker":"DELL","name":"Dell Technologies","sector":"Technology","industry":"PCs","country":"US"},
        {"ticker":"STX","name":"Seagate Technology","sector":"Technology","industry":"Storage","country":"IE"},
        {"ticker":"WDC","name":"Western Digital","sector":"Technology","industry":"Storage","country":"US"},
        {"ticker":"VRSK","name":"Verisk Analytics","sector":"Technology","industry":"Data & Analytics","country":"US"},

        # ── MORE CRYPTO (top 50) ──────────────────────────────
        {"ticker":"ADA-USD","name":"Cardano","sector":"Crypto","industry":"Layer 1","country":"US"},
        {"ticker":"AVAX-USD","name":"Avalanche","sector":"Crypto","industry":"Layer 1","country":"US"},
        {"ticker":"DOT-USD","name":"Polkadot","sector":"Crypto","industry":"Layer 0","country":"US"},
        {"ticker":"MATIC-USD","name":"Polygon","sector":"Crypto","industry":"Layer 2","country":"US"},
        {"ticker":"LINK-USD","name":"Chainlink","sector":"Crypto","industry":"Oracle","country":"US"},
        {"ticker":"UNI-USD","name":"Uniswap","sector":"Crypto","industry":"DEX","country":"US"},
        {"ticker":"LTC-USD","name":"Litecoin","sector":"Crypto","industry":"Payments","country":"US"},
        {"ticker":"BCH-USD","name":"Bitcoin Cash","sector":"Crypto","industry":"Payments","country":"US"},
        {"ticker":"ATOM-USD","name":"Cosmos","sector":"Crypto","industry":"Interoperability","country":"US"},
        {"ticker":"NEAR-USD","name":"NEAR Protocol","sector":"Crypto","industry":"Layer 1","country":"US"},
        {"ticker":"APT-USD","name":"Aptos","sector":"Crypto","industry":"Layer 1","country":"US"},
        {"ticker":"ARB-USD","name":"Arbitrum","sector":"Crypto","industry":"Layer 2","country":"US"},
        {"ticker":"OP-USD","name":"Optimism","sector":"Crypto","industry":"Layer 2","country":"US"},
        {"ticker":"SUI-USD","name":"Sui","sector":"Crypto","industry":"Layer 1","country":"US"},
        {"ticker":"INJ-USD","name":"Injective","sector":"Crypto","industry":"DeFi","country":"US"},
        {"ticker":"TON-USD","name":"Toncoin","sector":"Crypto","industry":"Layer 1","country":"US"},
        {"ticker":"PEPE-USD","name":"Pepe","sector":"Crypto","industry":"Meme","country":"US"},
        {"ticker":"WIF-USD","name":"dogwifhat","sector":"Crypto","industry":"Meme","country":"US"},
        {"ticker":"FTM-USD","name":"Fantom","sector":"Crypto","industry":"Layer 1","country":"US"},
        {"ticker":"AAVE-USD","name":"Aave","sector":"Crypto","industry":"DeFi","country":"US"},
        {"ticker":"MKR-USD","name":"Maker","sector":"Crypto","industry":"DeFi","country":"US"},

        # ── EXTENDED FOREX ────────────────────────────────────
        {"ticker":"AUDUSD=X","name":"AUD/USD","sector":"Forex","industry":"Major Pair","country":"US"},
        {"ticker":"NZDUSD=X","name":"NZD/USD","sector":"Forex","industry":"Major Pair","country":"US"},
        {"ticker":"USDCHF=X","name":"USD/CHF","sector":"Forex","industry":"Major Pair","country":"US"},
        {"ticker":"EURGBP=X","name":"EUR/GBP","sector":"Forex","industry":"Cross Pair","country":"US"},
        {"ticker":"EURJPY=X","name":"EUR/JPY","sector":"Forex","industry":"Cross Pair","country":"US"},
        {"ticker":"GBPJPY=X","name":"GBP/JPY","sector":"Forex","industry":"Cross Pair","country":"US"},
        {"ticker":"EURCHF=X","name":"EUR/CHF","sector":"Forex","industry":"Cross Pair","country":"US"},
        {"ticker":"USDINR=X","name":"USD/INR","sector":"Forex","industry":"EM Pair","country":"US"},
        {"ticker":"USDCNH=X","name":"USD/CNH","sector":"Forex","industry":"EM Pair","country":"US"},
        {"ticker":"USDBRL=X","name":"USD/BRL","sector":"Forex","industry":"EM Pair","country":"US"},
        {"ticker":"USDMXN=X","name":"USD/MXN","sector":"Forex","industry":"EM Pair","country":"US"},
        {"ticker":"USDZAR=X","name":"USD/ZAR","sector":"Forex","industry":"EM Pair","country":"US"},
        {"ticker":"USDTRY=X","name":"USD/TRY","sector":"Forex","industry":"EM Pair","country":"US"},
        {"ticker":"USDSGD=X","name":"USD/SGD","sector":"Forex","industry":"EM Pair","country":"US"},

        # ── COMMODITIES FUTURES ───────────────────────────────
        {"ticker":"GC=F","name":"Gold Futures","sector":"Commodities","industry":"Precious Metals","country":"US"},
        {"ticker":"SI=F","name":"Silver Futures","sector":"Commodities","industry":"Precious Metals","country":"US"},
        {"ticker":"CL=F","name":"Crude Oil WTI","sector":"Commodities","industry":"Energy","country":"US"},
        {"ticker":"BZ=F","name":"Brent Crude Oil","sector":"Commodities","industry":"Energy","country":"US"},
        {"ticker":"NG=F","name":"Natural Gas","sector":"Commodities","industry":"Energy","country":"US"},
        {"ticker":"HG=F","name":"Copper Futures","sector":"Commodities","industry":"Industrial Metals","country":"US"},
        {"ticker":"ZW=F","name":"Wheat Futures","sector":"Commodities","industry":"Grains","country":"US"},
        {"ticker":"ZC=F","name":"Corn Futures","sector":"Commodities","industry":"Grains","country":"US"},
        {"ticker":"ZS=F","name":"Soybean Futures","sector":"Commodities","industry":"Grains","country":"US"},

        # ── SECTOR ETFs & INDEX ETFs ───────────────────────────
        {"ticker":"GLD","name":"SPDR Gold ETF","sector":"ETF","industry":"Gold","country":"US"},
        {"ticker":"SLV","name":"iShares Silver ETF","sector":"ETF","industry":"Silver","country":"US"},
        {"ticker":"GDX","name":"VanEck Gold Miners ETF","sector":"ETF","industry":"Gold Miners","country":"US"},
        {"ticker":"GDXJ","name":"Junior Gold Miners ETF","sector":"ETF","industry":"Gold Miners","country":"US"},
        {"ticker":"USO","name":"US Oil Fund","sector":"ETF","industry":"Oil","country":"US"},
        {"ticker":"UNG","name":"US Natural Gas Fund","sector":"ETF","industry":"Gas","country":"US"},
        {"ticker":"COPX","name":"Copper Miners ETF","sector":"ETF","industry":"Copper","country":"US"},
        {"ticker":"LIT","name":"Lithium & Battery Tech ETF","sector":"ETF","industry":"Lithium","country":"US"},
        {"ticker":"URA","name":"Uranium ETF","sector":"ETF","industry":"Uranium","country":"US"},
        {"ticker":"TAN","name":"Solar ETF","sector":"ETF","industry":"Solar","country":"US"},
        {"ticker":"ICLN","name":"Clean Energy ETF","sector":"ETF","industry":"Clean Energy","country":"US"},
        {"ticker":"ARKK","name":"ARK Innovation ETF","sector":"ETF","industry":"Thematic","country":"US"},
        {"ticker":"ARKG","name":"ARK Genomics ETF","sector":"ETF","industry":"Genomics","country":"US"},
        {"ticker":"BOTZ","name":"Global Robotics & AI ETF","sector":"ETF","industry":"Robotics","country":"US"},
        {"ticker":"CIBR","name":"Cybersecurity ETF","sector":"ETF","industry":"Cybersecurity","country":"US"},
        {"ticker":"AIQ","name":"AI & Technology ETF","sector":"ETF","industry":"AI","country":"US"},
        {"ticker":"UFO","name":"Space ETF","sector":"ETF","industry":"Space","country":"US"},
        {"ticker":"XLK","name":"Technology SPDR","sector":"ETF","industry":"Technology","country":"US"},
        {"ticker":"XLF","name":"Financial SPDR","sector":"ETF","industry":"Finance","country":"US"},
        {"ticker":"XLV","name":"Health Care SPDR","sector":"ETF","industry":"Healthcare","country":"US"},
        {"ticker":"XLE","name":"Energy SPDR","sector":"ETF","industry":"Energy","country":"US"},
        {"ticker":"XLI","name":"Industrial SPDR","sector":"ETF","industry":"Industrials","country":"US"},
        {"ticker":"XLB","name":"Materials SPDR","sector":"ETF","industry":"Materials","country":"US"},
        {"ticker":"SPY","name":"SPDR S&P 500 ETF","sector":"ETF","industry":"Index","country":"US"},
        {"ticker":"QQQ","name":"Invesco NASDAQ 100 ETF","sector":"ETF","industry":"Index","country":"US"},
        {"ticker":"IWM","name":"iShares Russell 2000 ETF","sector":"ETF","industry":"Index","country":"US"},
        {"ticker":"DIA","name":"SPDR Dow Jones ETF","sector":"ETF","industry":"Index","country":"US"},
        {"ticker":"EWU","name":"iShares MSCI UK ETF","sector":"ETF","industry":"Index","country":"US"},
        {"ticker":"EWG","name":"iShares MSCI Germany ETF","sector":"ETF","industry":"Index","country":"US"},
        {"ticker":"EWJ","name":"iShares MSCI Japan ETF","sector":"ETF","industry":"Index","country":"US"},
        {"ticker":"EEM","name":"iShares MSCI EM ETF","sector":"ETF","industry":"Index","country":"US"},
        {"ticker":"TLT","name":"iShares 20yr Treasury ETF","sector":"ETF","industry":"Bonds","country":"US"},
        {"ticker":"HYG","name":"High Yield Bond ETF","sector":"ETF","industry":"Bonds","country":"US"},

        # ── METALS & MINING ADDITIONS ─────────────────────────
        {"ticker":"GFI","name":"Gold Fields","sector":"Materials","industry":"Gold Mining","country":"ZA"},
        {"ticker":"AEM","name":"Agnico Eagle Mines","sector":"Materials","industry":"Gold Mining","country":"CA"},
        {"ticker":"NEM","name":"Newmont Corporation","sector":"Materials","industry":"Gold Mining","country":"US"},
        {"ticker":"KGC","name":"Kinross Gold","sector":"Materials","industry":"Gold Mining","country":"CA"},
        {"ticker":"GOLD","name":"Barrick Gold","sector":"Materials","industry":"Gold Mining","country":"CA"},
        {"ticker":"WPM","name":"Wheaton Precious Metals","sector":"Materials","industry":"Royalties","country":"CA"},
        {"ticker":"FNV","name":"Franco-Nevada","sector":"Materials","industry":"Royalties","country":"CA"},
        {"ticker":"PAAS","name":"Pan American Silver","sector":"Materials","industry":"Silver Mining","country":"CA"},
        {"ticker":"SCCO","name":"Southern Copper","sector":"Materials","industry":"Copper","country":"US"},
        {"ticker":"TECK","name":"Teck Resources","sector":"Materials","industry":"Diversified Mining","country":"CA"},
        {"ticker":"STLD","name":"Steel Dynamics","sector":"Materials","industry":"Steel","country":"US"},
        {"ticker":"ATI","name":"ATI Inc","sector":"Materials","industry":"Specialty Metals","country":"US"},
        {"ticker":"MP","name":"MP Materials","sector":"Materials","industry":"Rare Earth","country":"US"},
        {"ticker":"SQM","name":"Sociedad Quimica","sector":"Materials","industry":"Lithium","country":"CL"},
        {"ticker":"PLL","name":"Piedmont Lithium","sector":"Materials","industry":"Lithium","country":"US"},
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

        # ── REMAINING FTSE 100 ────────────────────────────────
        {"ticker":"ABF.L","name":"Associated British Foods","sector":"Consumer","industry":"Food","country":"GB","exchange":"LSE","currency":"GBP"},
        {"ticker":"ADM.L","name":"Admiral Group","sector":"Finance","industry":"Insurance","country":"GB","exchange":"LSE","currency":"GBP"},
        {"ticker":"AHT.L","name":"Ashtead Group","sector":"Industrials","industry":"Equipment Rental","country":"GB","exchange":"LSE","currency":"GBP"},
        {"ticker":"ANTO.L","name":"Antofagasta","sector":"Materials","industry":"Copper Mining","country":"CL","exchange":"LSE","currency":"GBP"},
        {"ticker":"AV.L","name":"Aviva","sector":"Finance","industry":"Insurance","country":"GB","exchange":"LSE","currency":"GBP"},
        {"ticker":"BATS.L","name":"British American Tobacco","sector":"Consumer","industry":"Tobacco","country":"GB","exchange":"LSE","currency":"GBP"},
        {"ticker":"BLND.L","name":"British Land","sector":"Real Estate","industry":"REIT","country":"GB","exchange":"LSE","currency":"GBP"},
        {"ticker":"BVIC.L","name":"Britvic","sector":"Consumer","industry":"Beverages","country":"GB","exchange":"LSE","currency":"GBP"},
        {"ticker":"CPG.L","name":"Compass Group","sector":"Consumer","industry":"Catering","country":"GB","exchange":"LSE","currency":"GBP"},
        {"ticker":"CRH.L","name":"CRH plc","sector":"Industrials","industry":"Building Materials","country":"IE","exchange":"LSE","currency":"GBP"},
        {"ticker":"EZJ.L","name":"easyJet","sector":"Consumer","industry":"Airlines","country":"GB","exchange":"LSE","currency":"GBP"},
        {"ticker":"FERG.L","name":"Ferguson Enterprises","sector":"Industrials","industry":"Distribution","country":"GB","exchange":"LSE","currency":"GBP"},
        {"ticker":"FLTR.L","name":"Flutter Entertainment","sector":"Consumer","industry":"Gambling","country":"IE","exchange":"LSE","currency":"GBP"},
        {"ticker":"FRES.L","name":"Fresnillo","sector":"Materials","industry":"Silver Mining","country":"MX","exchange":"LSE","currency":"GBP"},
        {"ticker":"HIK.L","name":"Hikma Pharmaceuticals","sector":"Healthcare","industry":"Generics","country":"JO","exchange":"LSE","currency":"GBP"},
        {"ticker":"HL.L","name":"Hargreaves Lansdown","sector":"Finance","industry":"Wealth Management","country":"GB","exchange":"LSE","currency":"GBP"},
        {"ticker":"IMB.L","name":"Imperial Brands","sector":"Consumer","industry":"Tobacco","country":"GB","exchange":"LSE","currency":"GBP"},
        {"ticker":"INF.L","name":"Informa","sector":"Technology","industry":"Publishing","country":"GB","exchange":"LSE","currency":"GBP"},
        {"ticker":"ITRK.L","name":"Intertek Group","sector":"Industrials","industry":"Testing","country":"GB","exchange":"LSE","currency":"GBP"},
        {"ticker":"JD.L","name":"JD Sports","sector":"Consumer","industry":"Retail","country":"GB","exchange":"LSE","currency":"GBP"},
        {"ticker":"KGF.L","name":"Kingfisher","sector":"Consumer","industry":"DIY Retail","country":"GB","exchange":"LSE","currency":"GBP"},
        {"ticker":"LGEN.L","name":"Legal & General","sector":"Finance","industry":"Insurance","country":"GB","exchange":"LSE","currency":"GBP"},
        {"ticker":"MNDI.L","name":"Mondi","sector":"Materials","industry":"Packaging","country":"GB","exchange":"LSE","currency":"GBP"},
        {"ticker":"MNG.L","name":"M&G plc","sector":"Finance","industry":"Asset Management","country":"GB","exchange":"LSE","currency":"GBP"},
        {"ticker":"NXT.L","name":"Next plc","sector":"Consumer","industry":"Retail","country":"GB","exchange":"LSE","currency":"GBP"},
        {"ticker":"OCDO.L","name":"Ocado Group","sector":"Technology","industry":"E-Commerce","country":"GB","exchange":"LSE","currency":"GBP"},
        {"ticker":"PSN.L","name":"Persimmon","sector":"Real Estate","industry":"Housebuilding","country":"GB","exchange":"LSE","currency":"GBP"},
        {"ticker":"PSON.L","name":"Pearson","sector":"Technology","industry":"Education","country":"GB","exchange":"LSE","currency":"GBP"},
        {"ticker":"RKT.L","name":"Reckitt Benckiser","sector":"Consumer","industry":"FMCG","country":"GB","exchange":"LSE","currency":"GBP"},
        {"ticker":"RMV.L","name":"Rightmove","sector":"Technology","industry":"Marketplace","country":"GB","exchange":"LSE","currency":"GBP"},
        {"ticker":"SGE.L","name":"Sage Group","sector":"Technology","industry":"Software","country":"GB","exchange":"LSE","currency":"GBP"},
        {"ticker":"SKG.L","name":"Smurfit Kappa","sector":"Materials","industry":"Packaging","country":"IE","exchange":"LSE","currency":"GBP"},
        {"ticker":"SMT.L","name":"Scottish Mortgage IT","sector":"Finance","industry":"Investment Trust","country":"GB","exchange":"LSE","currency":"GBP"},
        {"ticker":"SN.L","name":"Smith & Nephew","sector":"Healthcare","industry":"MedTech","country":"GB","exchange":"LSE","currency":"GBP"},
        {"ticker":"SMIN.L","name":"Smiths Group","sector":"Industrials","industry":"Diversified","country":"GB","exchange":"LSE","currency":"GBP"},
        {"ticker":"SPX.L","name":"Spirax-Sarco","sector":"Industrials","industry":"Engineering","country":"GB","exchange":"LSE","currency":"GBP"},
        {"ticker":"STJ.L","name":"St. James Place","sector":"Finance","industry":"Wealth Management","country":"GB","exchange":"LSE","currency":"GBP"},
        {"ticker":"TW.L","name":"Taylor Wimpey","sector":"Real Estate","industry":"Housebuilding","country":"GB","exchange":"LSE","currency":"GBP"},
        {"ticker":"UU.L","name":"United Utilities","sector":"Industrials","industry":"Water","country":"GB","exchange":"LSE","currency":"GBP"},
        {"ticker":"WPP.L","name":"WPP plc","sector":"Technology","industry":"Advertising","country":"GB","exchange":"LSE","currency":"GBP"},
        {"ticker":"WTB.L","name":"Whitbread","sector":"Consumer","industry":"Hotels","country":"GB","exchange":"LSE","currency":"GBP"},
        {"ticker":"MNDI.L","name":"Mondi","sector":"Materials","industry":"Packaging","country":"GB","exchange":"LSE","currency":"GBP"},

        # ── EUROPEAN BLUE CHIPS ───────────────────────────────
        {"ticker":"SAP.DE","name":"SAP SE","sector":"Technology","industry":"Software","country":"DE","exchange":"XETRA","currency":"EUR"},
        {"ticker":"SIE.DE","name":"Siemens AG","sector":"Industrials","industry":"Engineering","country":"DE","exchange":"XETRA","currency":"EUR"},
        {"ticker":"ALV.DE","name":"Allianz SE","sector":"Finance","industry":"Insurance","country":"DE","exchange":"XETRA","currency":"EUR"},
        {"ticker":"MUV2.DE","name":"Munich Re","sector":"Finance","industry":"Reinsurance","country":"DE","exchange":"XETRA","currency":"EUR"},
        {"ticker":"DBK.DE","name":"Deutsche Bank","sector":"Finance","industry":"Banking","country":"DE","exchange":"XETRA","currency":"EUR"},
        {"ticker":"BMW.DE","name":"BMW AG","sector":"Consumer","industry":"Auto","country":"DE","exchange":"XETRA","currency":"EUR"},
        {"ticker":"MBG.DE","name":"Mercedes-Benz","sector":"Consumer","industry":"Auto","country":"DE","exchange":"XETRA","currency":"EUR"},
        {"ticker":"VOW3.DE","name":"Volkswagen AG","sector":"Consumer","industry":"Auto","country":"DE","exchange":"XETRA","currency":"EUR"},
        {"ticker":"BAYN.DE","name":"Bayer AG","sector":"Healthcare","industry":"Pharma","country":"DE","exchange":"XETRA","currency":"EUR"},
        {"ticker":"BASF.DE","name":"BASF SE","sector":"Materials","industry":"Chemicals","country":"DE","exchange":"XETRA","currency":"EUR"},
        {"ticker":"DTE.DE","name":"Deutsche Telekom","sector":"Technology","industry":"Telecom","country":"DE","exchange":"XETRA","currency":"EUR"},
        {"ticker":"RWE.DE","name":"RWE AG","sector":"Energy","industry":"Renewables","country":"DE","exchange":"XETRA","currency":"EUR"},
        {"ticker":"ADS.DE","name":"Adidas AG","sector":"Consumer","industry":"Apparel","country":"DE","exchange":"XETRA","currency":"EUR"},
        {"ticker":"MRK.DE","name":"Merck KGaA","sector":"Healthcare","industry":"Pharma","country":"DE","exchange":"XETRA","currency":"EUR"},
        {"ticker":"EOAN.DE","name":"E.ON SE","sector":"Energy","industry":"Utilities","country":"DE","exchange":"XETRA","currency":"EUR"},
        {"ticker":"OR.PA","name":"LOreal","sector":"Consumer","industry":"Beauty","country":"FR","exchange":"EURONEXT","currency":"EUR"},
        {"ticker":"MC.PA","name":"LVMH","sector":"Consumer","industry":"Luxury","country":"FR","exchange":"EURONEXT","currency":"EUR"},
        {"ticker":"RMS.PA","name":"Hermes","sector":"Consumer","industry":"Luxury","country":"FR","exchange":"EURONEXT","currency":"EUR"},
        {"ticker":"TTE.PA","name":"TotalEnergies","sector":"Energy","industry":"Oil & Gas","country":"FR","exchange":"EURONEXT","currency":"EUR"},
        {"ticker":"BNP.PA","name":"BNP Paribas","sector":"Finance","industry":"Banking","country":"FR","exchange":"EURONEXT","currency":"EUR"},
        {"ticker":"SAN.PA","name":"Sanofi","sector":"Healthcare","industry":"Pharma","country":"FR","exchange":"EURONEXT","currency":"EUR"},
        {"ticker":"AIR.PA","name":"Airbus SE","sector":"Industrials","industry":"Aerospace","country":"FR","exchange":"EURONEXT","currency":"EUR"},
        {"ticker":"AXA.PA","name":"AXA SA","sector":"Finance","industry":"Insurance","country":"FR","exchange":"EURONEXT","currency":"EUR"},
        {"ticker":"HEIA.AS","name":"Heineken","sector":"Consumer","industry":"Beverages","country":"NL","exchange":"EURONEXT","currency":"EUR"},
        {"ticker":"INGA.AS","name":"ING Group","sector":"Finance","industry":"Banking","country":"NL","exchange":"EURONEXT","currency":"EUR"},
        {"ticker":"ADYEN.AS","name":"Adyen","sector":"Finance","industry":"Fintech","country":"NL","exchange":"EURONEXT","currency":"EUR"},
        {"ticker":"NESN.SW","name":"Nestle SA","sector":"Consumer","industry":"Food","country":"CH","exchange":"SIX","currency":"CHF"},
        {"ticker":"ROG.SW","name":"Roche Holding","sector":"Healthcare","industry":"Pharma","country":"CH","exchange":"SIX","currency":"CHF"},
        {"ticker":"NOVN.SW","name":"Novartis AG","sector":"Healthcare","industry":"Pharma","country":"CH","exchange":"SIX","currency":"CHF"},
        {"ticker":"UBSG.SW","name":"UBS Group","sector":"Finance","industry":"Banking","country":"CH","exchange":"SIX","currency":"CHF"},
        {"ticker":"NOVOB.CO","name":"Novo Nordisk","sector":"Healthcare","industry":"Pharma","country":"DK","exchange":"OMXC","currency":"DKK"},
        {"ticker":"ERIC-B.ST","name":"Ericsson","sector":"Technology","industry":"Telecom","country":"SE","exchange":"OMXS","currency":"SEK"},
        {"ticker":"VOLV-B.ST","name":"Volvo AB","sector":"Industrials","industry":"Trucks","country":"SE","exchange":"OMXS","currency":"SEK"},

        # ── APAC ──────────────────────────────────────────────
        {"ticker":"7203.T","name":"Toyota Motor","sector":"Consumer","industry":"Auto","country":"JP","exchange":"TSE","currency":"JPY"},
        {"ticker":"6758.T","name":"Sony Group","sector":"Technology","industry":"Electronics","country":"JP","exchange":"TSE","currency":"JPY"},
        {"ticker":"9984.T","name":"SoftBank Group","sector":"Finance","industry":"Investment","country":"JP","exchange":"TSE","currency":"JPY"},
        {"ticker":"7974.T","name":"Nintendo","sector":"Consumer","industry":"Gaming","country":"JP","exchange":"TSE","currency":"JPY"},
        {"ticker":"6861.T","name":"Keyence","sector":"Technology","industry":"Automation","country":"JP","exchange":"TSE","currency":"JPY"},
        {"ticker":"8306.T","name":"Mitsubishi UFJ","sector":"Finance","industry":"Banking","country":"JP","exchange":"TSE","currency":"JPY"},
        {"ticker":"BHP.AX","name":"BHP Group ASX","sector":"Materials","industry":"Mining","country":"AU","exchange":"ASX","currency":"AUD"},
        {"ticker":"CBA.AX","name":"Commonwealth Bank","sector":"Finance","industry":"Banking","country":"AU","exchange":"ASX","currency":"AUD"},
        {"ticker":"CSL.AX","name":"CSL Limited","sector":"Healthcare","industry":"Biotech","country":"AU","exchange":"ASX","currency":"AUD"},
        {"ticker":"RIO.AX","name":"Rio Tinto ASX","sector":"Materials","industry":"Mining","country":"AU","exchange":"ASX","currency":"AUD"},
        {"ticker":"WBC.AX","name":"Westpac Banking","sector":"Finance","industry":"Banking","country":"AU","exchange":"ASX","currency":"AUD"},
        {"ticker":"005930.KS","name":"Samsung Electronics","sector":"Technology","industry":"Semiconductors","country":"KR","exchange":"KRX","currency":"KRW"},
        {"ticker":"BABA","name":"Alibaba Group","sector":"Technology","industry":"E-Commerce","country":"CN"},
        {"ticker":"JD","name":"JD.com","sector":"Technology","industry":"E-Commerce","country":"CN"},
        {"ticker":"PDD","name":"PDD Holdings","sector":"Technology","industry":"E-Commerce","country":"CN"},
        {"ticker":"BIDU","name":"Baidu","sector":"Technology","industry":"Search","country":"CN"},
        {"ticker":"TCEHY","name":"Tencent Holdings","sector":"Technology","industry":"Social Media","country":"CN"},
        {"ticker":"NTES","name":"NetEase","sector":"Technology","industry":"Gaming","country":"CN"},

        # ── S&P 500 GAP FILLS ─────────────────────────────────
        {"ticker":"GOOG","name":"Alphabet Class C","sector":"Technology","industry":"Software","country":"US"},
        {"ticker":"BRK-B","name":"Berkshire Hathaway B","sector":"Finance","industry":"Conglomerate","country":"US"},
        {"ticker":"LIN","name":"Linde plc","sector":"Materials","industry":"Chemicals","country":"US"},
        {"ticker":"PG","name":"Procter & Gamble","sector":"Consumer","industry":"FMCG","country":"US"},
        {"ticker":"KO","name":"Coca-Cola","sector":"Consumer","industry":"Beverages","country":"US"},
        {"ticker":"PEP","name":"PepsiCo","sector":"Consumer","industry":"Beverages","country":"US"},
        {"ticker":"PM","name":"Philip Morris","sector":"Consumer","industry":"Tobacco","country":"US"},
        {"ticker":"MO","name":"Altria Group","sector":"Consumer","industry":"Tobacco","country":"US"},
        {"ticker":"CL","name":"Colgate-Palmolive","sector":"Consumer","industry":"FMCG","country":"US"},
        {"ticker":"GIS","name":"General Mills","sector":"Consumer","industry":"Food","country":"US"},
        {"ticker":"MDLZ","name":"Mondelez International","sector":"Consumer","industry":"Food","country":"US"},
        {"ticker":"HSY","name":"Hershey Company","sector":"Consumer","industry":"Food","country":"US"},
        {"ticker":"STZ","name":"Constellation Brands","sector":"Consumer","industry":"Beverages","country":"US"},
        {"ticker":"HD","name":"Home Depot","sector":"Consumer","industry":"Home Improvement","country":"US"},
        {"ticker":"LOW","name":"Lowes Companies","sector":"Consumer","industry":"Home Improvement","country":"US"},
        {"ticker":"TJX","name":"TJX Companies","sector":"Consumer","industry":"Retail","country":"US"},
        {"ticker":"EBAY","name":"eBay","sector":"Consumer","industry":"E-Commerce","country":"US"},
        {"ticker":"ETSY","name":"Etsy","sector":"Consumer","industry":"E-Commerce","country":"US"},
        {"ticker":"DASH","name":"DoorDash","sector":"Consumer","industry":"Delivery","country":"US"},
        {"ticker":"YUM","name":"Yum Brands","sector":"Consumer","industry":"Restaurants","country":"US"},
        {"ticker":"DPZ","name":"Dominos Pizza","sector":"Consumer","industry":"Restaurants","country":"US"},
        {"ticker":"MAR","name":"Marriott International","sector":"Consumer","industry":"Hotels","country":"US"},
        {"ticker":"HLT","name":"Hilton Worldwide","sector":"Consumer","industry":"Hotels","country":"US"},
        {"ticker":"CCL","name":"Carnival Corporation","sector":"Consumer","industry":"Cruise","country":"US"},
        {"ticker":"RCL","name":"Royal Caribbean","sector":"Consumer","industry":"Cruise","country":"US"},
        {"ticker":"UAL","name":"United Airlines","sector":"Consumer","industry":"Airlines","country":"US"},
        {"ticker":"DAL","name":"Delta Air Lines","sector":"Consumer","industry":"Airlines","country":"US"},
        {"ticker":"AAL","name":"American Airlines","sector":"Consumer","industry":"Airlines","country":"US"},
        {"ticker":"LUV","name":"Southwest Airlines","sector":"Consumer","industry":"Airlines","country":"US"},
        {"ticker":"WBA","name":"Walgreens Boots","sector":"Healthcare","industry":"Pharmacy","country":"US"},
        {"ticker":"CI","name":"Cigna Group","sector":"Healthcare","industry":"Health Insurance","country":"US"},
        {"ticker":"ELV","name":"Elevance Health","sector":"Healthcare","industry":"Health Insurance","country":"US"},
        {"ticker":"HUM","name":"Humana","sector":"Healthcare","industry":"Health Insurance","country":"US"},
        {"ticker":"TMO","name":"Thermo Fisher","sector":"Healthcare","industry":"Lab Equipment","country":"US"},
        {"ticker":"DHR","name":"Danaher","sector":"Healthcare","industry":"Lab Equipment","country":"US"},
        {"ticker":"BSX","name":"Boston Scientific","sector":"Healthcare","industry":"MedTech","country":"US"},
        {"ticker":"ILMN","name":"Illumina","sector":"Healthcare","industry":"Genomics","country":"US"},
        {"ticker":"ALNY","name":"Alnylam Pharmaceuticals","sector":"Healthcare","industry":"Biotech","country":"US"},
        {"ticker":"INCY","name":"Incyte Corporation","sector":"Healthcare","industry":"Biotech","country":"US"},
        {"ticker":"BMRN","name":"BioMarin Pharmaceutical","sector":"Healthcare","industry":"Rare Disease","country":"US"},
        {"ticker":"USB","name":"US Bancorp","sector":"Finance","industry":"Banking","country":"US"},
        {"ticker":"PNC","name":"PNC Financial","sector":"Finance","industry":"Banking","country":"US"},
        {"ticker":"TFC","name":"Truist Financial","sector":"Finance","industry":"Banking","country":"US"},
        {"ticker":"COF","name":"Capital One","sector":"Finance","industry":"Banking","country":"US"},
        {"ticker":"ICE","name":"Intercontinental Exchange","sector":"Finance","industry":"Exchange","country":"US"},
        {"ticker":"CME","name":"CME Group","sector":"Finance","industry":"Exchange","country":"US"},
        {"ticker":"MSCI","name":"MSCI Inc","sector":"Finance","industry":"Data & Analytics","country":"US"},
        {"ticker":"SPGI","name":"SP Global","sector":"Finance","industry":"Data & Analytics","country":"US"},
        {"ticker":"MCO","name":"Moodys Corporation","sector":"Finance","industry":"Data & Analytics","country":"US"},
        {"ticker":"MMC","name":"Marsh McLennan","sector":"Finance","industry":"Insurance Broker","country":"US"},
        {"ticker":"AON","name":"Aon plc","sector":"Finance","industry":"Insurance Broker","country":"GB"},
        {"ticker":"MET","name":"MetLife","sector":"Finance","industry":"Insurance","country":"US"},
        {"ticker":"AFL","name":"Aflac","sector":"Finance","industry":"Insurance","country":"US"},
        {"ticker":"PGR","name":"Progressive Corporation","sector":"Finance","industry":"Insurance","country":"US"},
        {"ticker":"CB","name":"Chubb Limited","sector":"Finance","industry":"Insurance","country":"CH"},
        {"ticker":"TROW","name":"T Rowe Price","sector":"Finance","industry":"Asset Management","country":"US"},
        {"ticker":"BLK","name":"BlackRock","sector":"Finance","industry":"Asset Management","country":"US"},
        {"ticker":"LHX","name":"L3Harris Technologies","sector":"Space","industry":"Defence","country":"US"},
        {"ticker":"TDG","name":"TransDigm Group","sector":"Industrials","industry":"Aerospace","country":"US"},
        {"ticker":"ETN","name":"Eaton Corporation","sector":"Industrials","industry":"Power Mgmt","country":"IE"},
        {"ticker":"EMR","name":"Emerson Electric","sector":"Industrials","industry":"Automation","country":"US"},
        {"ticker":"ROK","name":"Rockwell Automation","sector":"Industrials","industry":"Automation","country":"US"},
        {"ticker":"ITW","name":"Illinois Tool Works","sector":"Industrials","industry":"Diversified","country":"US"},
        {"ticker":"PH","name":"Parker Hannifin","sector":"Industrials","industry":"Motion Control","country":"US"},
        {"ticker":"GWW","name":"WW Grainger","sector":"Industrials","industry":"Distribution","country":"US"},
        {"ticker":"FAST","name":"Fastenal","sector":"Industrials","industry":"Distribution","country":"US"},
        {"ticker":"CAT","name":"Caterpillar","sector":"Industrials","industry":"Machinery","country":"US"},
        {"ticker":"HON","name":"Honeywell","sector":"Industrials","industry":"Diversified","country":"US"},
        {"ticker":"GE","name":"GE Aerospace","sector":"Industrials","industry":"Aerospace","country":"US"},
        {"ticker":"UPS","name":"UPS","sector":"Industrials","industry":"Logistics","country":"US"},
        {"ticker":"FDX","name":"FedEx","sector":"Industrials","industry":"Logistics","country":"US"},
        {"ticker":"CSCO","name":"Cisco Systems","sector":"Technology","industry":"Networking","country":"US"},
        {"ticker":"ANET","name":"Arista Networks","sector":"Technology","industry":"Networking","country":"US"},
        {"ticker":"HPE","name":"Hewlett Packard Enterprise","sector":"Technology","industry":"Servers","country":"US"},
        {"ticker":"DELL","name":"Dell Technologies","sector":"Technology","industry":"PCs","country":"US"},
        {"ticker":"STX","name":"Seagate Technology","sector":"Technology","industry":"Storage","country":"IE"},
        {"ticker":"WDC","name":"Western Digital","sector":"Technology","industry":"Storage","country":"US"},
        {"ticker":"VRSK","name":"Verisk Analytics","sector":"Technology","industry":"Data & Analytics","country":"US"},

        # ── MORE CRYPTO (top 50) ──────────────────────────────
        {"ticker":"ADA-USD","name":"Cardano","sector":"Crypto","industry":"Layer 1","country":"US"},
        {"ticker":"AVAX-USD","name":"Avalanche","sector":"Crypto","industry":"Layer 1","country":"US"},
        {"ticker":"DOT-USD","name":"Polkadot","sector":"Crypto","industry":"Layer 0","country":"US"},
        {"ticker":"MATIC-USD","name":"Polygon","sector":"Crypto","industry":"Layer 2","country":"US"},
        {"ticker":"LINK-USD","name":"Chainlink","sector":"Crypto","industry":"Oracle","country":"US"},
        {"ticker":"UNI-USD","name":"Uniswap","sector":"Crypto","industry":"DEX","country":"US"},
        {"ticker":"LTC-USD","name":"Litecoin","sector":"Crypto","industry":"Payments","country":"US"},
        {"ticker":"BCH-USD","name":"Bitcoin Cash","sector":"Crypto","industry":"Payments","country":"US"},
        {"ticker":"ATOM-USD","name":"Cosmos","sector":"Crypto","industry":"Interoperability","country":"US"},
        {"ticker":"NEAR-USD","name":"NEAR Protocol","sector":"Crypto","industry":"Layer 1","country":"US"},
        {"ticker":"APT-USD","name":"Aptos","sector":"Crypto","industry":"Layer 1","country":"US"},
        {"ticker":"ARB-USD","name":"Arbitrum","sector":"Crypto","industry":"Layer 2","country":"US"},
        {"ticker":"OP-USD","name":"Optimism","sector":"Crypto","industry":"Layer 2","country":"US"},
        {"ticker":"SUI-USD","name":"Sui","sector":"Crypto","industry":"Layer 1","country":"US"},
        {"ticker":"INJ-USD","name":"Injective","sector":"Crypto","industry":"DeFi","country":"US"},
        {"ticker":"TON-USD","name":"Toncoin","sector":"Crypto","industry":"Layer 1","country":"US"},
        {"ticker":"PEPE-USD","name":"Pepe","sector":"Crypto","industry":"Meme","country":"US"},
        {"ticker":"WIF-USD","name":"dogwifhat","sector":"Crypto","industry":"Meme","country":"US"},
        {"ticker":"FTM-USD","name":"Fantom","sector":"Crypto","industry":"Layer 1","country":"US"},
        {"ticker":"AAVE-USD","name":"Aave","sector":"Crypto","industry":"DeFi","country":"US"},
        {"ticker":"MKR-USD","name":"Maker","sector":"Crypto","industry":"DeFi","country":"US"},

        # ── EXTENDED FOREX ────────────────────────────────────
        {"ticker":"AUDUSD=X","name":"AUD/USD","sector":"Forex","industry":"Major Pair","country":"US"},
        {"ticker":"NZDUSD=X","name":"NZD/USD","sector":"Forex","industry":"Major Pair","country":"US"},
        {"ticker":"USDCHF=X","name":"USD/CHF","sector":"Forex","industry":"Major Pair","country":"US"},
        {"ticker":"EURGBP=X","name":"EUR/GBP","sector":"Forex","industry":"Cross Pair","country":"US"},
        {"ticker":"EURJPY=X","name":"EUR/JPY","sector":"Forex","industry":"Cross Pair","country":"US"},
        {"ticker":"GBPJPY=X","name":"GBP/JPY","sector":"Forex","industry":"Cross Pair","country":"US"},
        {"ticker":"EURCHF=X","name":"EUR/CHF","sector":"Forex","industry":"Cross Pair","country":"US"},
        {"ticker":"USDINR=X","name":"USD/INR","sector":"Forex","industry":"EM Pair","country":"US"},
        {"ticker":"USDCNH=X","name":"USD/CNH","sector":"Forex","industry":"EM Pair","country":"US"},
        {"ticker":"USDBRL=X","name":"USD/BRL","sector":"Forex","industry":"EM Pair","country":"US"},
        {"ticker":"USDMXN=X","name":"USD/MXN","sector":"Forex","industry":"EM Pair","country":"US"},
        {"ticker":"USDZAR=X","name":"USD/ZAR","sector":"Forex","industry":"EM Pair","country":"US"},
        {"ticker":"USDTRY=X","name":"USD/TRY","sector":"Forex","industry":"EM Pair","country":"US"},
        {"ticker":"USDSGD=X","name":"USD/SGD","sector":"Forex","industry":"EM Pair","country":"US"},

        # ── COMMODITIES FUTURES ───────────────────────────────
        {"ticker":"GC=F","name":"Gold Futures","sector":"Commodities","industry":"Precious Metals","country":"US"},
        {"ticker":"SI=F","name":"Silver Futures","sector":"Commodities","industry":"Precious Metals","country":"US"},
        {"ticker":"CL=F","name":"Crude Oil WTI","sector":"Commodities","industry":"Energy","country":"US"},
        {"ticker":"BZ=F","name":"Brent Crude Oil","sector":"Commodities","industry":"Energy","country":"US"},
        {"ticker":"NG=F","name":"Natural Gas","sector":"Commodities","industry":"Energy","country":"US"},
        {"ticker":"HG=F","name":"Copper Futures","sector":"Commodities","industry":"Industrial Metals","country":"US"},
        {"ticker":"ZW=F","name":"Wheat Futures","sector":"Commodities","industry":"Grains","country":"US"},
        {"ticker":"ZC=F","name":"Corn Futures","sector":"Commodities","industry":"Grains","country":"US"},
        {"ticker":"ZS=F","name":"Soybean Futures","sector":"Commodities","industry":"Grains","country":"US"},

        # ── SECTOR ETFs & INDEX ETFs ───────────────────────────
        {"ticker":"GLD","name":"SPDR Gold ETF","sector":"ETF","industry":"Gold","country":"US"},
        {"ticker":"SLV","name":"iShares Silver ETF","sector":"ETF","industry":"Silver","country":"US"},
        {"ticker":"GDX","name":"VanEck Gold Miners ETF","sector":"ETF","industry":"Gold Miners","country":"US"},
        {"ticker":"GDXJ","name":"Junior Gold Miners ETF","sector":"ETF","industry":"Gold Miners","country":"US"},
        {"ticker":"USO","name":"US Oil Fund","sector":"ETF","industry":"Oil","country":"US"},
        {"ticker":"UNG","name":"US Natural Gas Fund","sector":"ETF","industry":"Gas","country":"US"},
        {"ticker":"COPX","name":"Copper Miners ETF","sector":"ETF","industry":"Copper","country":"US"},
        {"ticker":"LIT","name":"Lithium & Battery Tech ETF","sector":"ETF","industry":"Lithium","country":"US"},
        {"ticker":"URA","name":"Uranium ETF","sector":"ETF","industry":"Uranium","country":"US"},
        {"ticker":"TAN","name":"Solar ETF","sector":"ETF","industry":"Solar","country":"US"},
        {"ticker":"ICLN","name":"Clean Energy ETF","sector":"ETF","industry":"Clean Energy","country":"US"},
        {"ticker":"ARKK","name":"ARK Innovation ETF","sector":"ETF","industry":"Thematic","country":"US"},
        {"ticker":"ARKG","name":"ARK Genomics ETF","sector":"ETF","industry":"Genomics","country":"US"},
        {"ticker":"BOTZ","name":"Global Robotics & AI ETF","sector":"ETF","industry":"Robotics","country":"US"},
        {"ticker":"CIBR","name":"Cybersecurity ETF","sector":"ETF","industry":"Cybersecurity","country":"US"},
        {"ticker":"AIQ","name":"AI & Technology ETF","sector":"ETF","industry":"AI","country":"US"},
        {"ticker":"UFO","name":"Space ETF","sector":"ETF","industry":"Space","country":"US"},
        {"ticker":"XLK","name":"Technology SPDR","sector":"ETF","industry":"Technology","country":"US"},
        {"ticker":"XLF","name":"Financial SPDR","sector":"ETF","industry":"Finance","country":"US"},
        {"ticker":"XLV","name":"Health Care SPDR","sector":"ETF","industry":"Healthcare","country":"US"},
        {"ticker":"XLE","name":"Energy SPDR","sector":"ETF","industry":"Energy","country":"US"},
        {"ticker":"XLI","name":"Industrial SPDR","sector":"ETF","industry":"Industrials","country":"US"},
        {"ticker":"XLB","name":"Materials SPDR","sector":"ETF","industry":"Materials","country":"US"},
        {"ticker":"SPY","name":"SPDR S&P 500 ETF","sector":"ETF","industry":"Index","country":"US"},
        {"ticker":"QQQ","name":"Invesco NASDAQ 100 ETF","sector":"ETF","industry":"Index","country":"US"},
        {"ticker":"IWM","name":"iShares Russell 2000 ETF","sector":"ETF","industry":"Index","country":"US"},
        {"ticker":"DIA","name":"SPDR Dow Jones ETF","sector":"ETF","industry":"Index","country":"US"},
        {"ticker":"EWU","name":"iShares MSCI UK ETF","sector":"ETF","industry":"Index","country":"US"},
        {"ticker":"EWG","name":"iShares MSCI Germany ETF","sector":"ETF","industry":"Index","country":"US"},
        {"ticker":"EWJ","name":"iShares MSCI Japan ETF","sector":"ETF","industry":"Index","country":"US"},
        {"ticker":"EEM","name":"iShares MSCI EM ETF","sector":"ETF","industry":"Index","country":"US"},
        {"ticker":"TLT","name":"iShares 20yr Treasury ETF","sector":"ETF","industry":"Bonds","country":"US"},
        {"ticker":"HYG","name":"High Yield Bond ETF","sector":"ETF","industry":"Bonds","country":"US"},

        # ── METALS & MINING ADDITIONS ─────────────────────────
        {"ticker":"GFI","name":"Gold Fields","sector":"Materials","industry":"Gold Mining","country":"ZA"},
        {"ticker":"AEM","name":"Agnico Eagle Mines","sector":"Materials","industry":"Gold Mining","country":"CA"},
        {"ticker":"NEM","name":"Newmont Corporation","sector":"Materials","industry":"Gold Mining","country":"US"},
        {"ticker":"KGC","name":"Kinross Gold","sector":"Materials","industry":"Gold Mining","country":"CA"},
        {"ticker":"GOLD","name":"Barrick Gold","sector":"Materials","industry":"Gold Mining","country":"CA"},
        {"ticker":"WPM","name":"Wheaton Precious Metals","sector":"Materials","industry":"Royalties","country":"CA"},
        {"ticker":"FNV","name":"Franco-Nevada","sector":"Materials","industry":"Royalties","country":"CA"},
        {"ticker":"PAAS","name":"Pan American Silver","sector":"Materials","industry":"Silver Mining","country":"CA"},
        {"ticker":"SCCO","name":"Southern Copper","sector":"Materials","industry":"Copper","country":"US"},
        {"ticker":"TECK","name":"Teck Resources","sector":"Materials","industry":"Diversified Mining","country":"CA"},
        {"ticker":"STLD","name":"Steel Dynamics","sector":"Materials","industry":"Steel","country":"US"},
        {"ticker":"ATI","name":"ATI Inc","sector":"Materials","industry":"Specialty Metals","country":"US"},
        {"ticker":"MP","name":"MP Materials","sector":"Materials","industry":"Rare Earth","country":"US"},
        {"ticker":"SQM","name":"Sociedad Quimica","sector":"Materials","industry":"Lithium","country":"CL"},
        {"ticker":"PLL","name":"Piedmont Lithium","sector":"Materials","industry":"Lithium","country":"US"},
    ]

    async def fetch(self) -> List[dict]:
        log.info(f"Loading {len(self.SEEDS)} static seed assets")
        for seed in self.SEEDS:
            seed["source"] = "static_seed"
            if "quote_type" not in seed:
                seed["quote_type"] = "EQUITY"
        return self.SEEDS
