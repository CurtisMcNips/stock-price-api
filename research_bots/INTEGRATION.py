"""
Market Brain — Research Bots Integration Guide
═══════════════════════════════════════════════

RAILWAY ENVIRONMENT VARIABLES — ADD ALL OF THESE
─────────────────────────────────────────────────
Variable          Service   Value
──────────────────────────────────────────────────────────────
GNEWS_API_KEY     web       c7d8195679eab38431bbd674bb74fd96
FMP_API_KEY       web       REi5YWMduTkssRQFsNyEONemYwbbSjro
AV_API_KEY        web       KH9A652VHUDYN4SK
FRED_API_KEY      web       c18ec6200f048fa6f236646d5787ee5f
POLYGON_API_KEY   web       phpD3Q34FRcLIb1kphSqBAQjDBWKrb_y

All keys are also hardcoded as fallbacks in the bot files themselves,
so the bots work immediately even before Railway variables are set.
Adding the variables lets you rotate keys later without touching code.


BOT → DATA SOURCE MAP (after upgrade)
──────────────────────────────────────
Bot                  Primary          Fallback         Key needed
────────────────────────────────────────────────────────────────────
NewsBot              GNews            —                GNEWS_API_KEY
EarningsBot          FMP (UK) /       Alpha Vantage    FMP_API_KEY
                     Yahoo (US)                        AV_API_KEY
MacroBot             FRED + Yahoo     Yahoo ETFs only  FRED_API_KEY
InsiderBot           SEC EDGAR        —                none
FundamentalsBot      FMP              Yahoo            FMP_API_KEY
TechnicalLevelsBot   Polygon (US)     Yahoo            POLYGON_API_KEY
                     Yahoo (UK/global)
AnalystBot           FMP              Yahoo            FMP_API_KEY


DEPLOYMENT STEPS
────────────────
1. Upload research_bots/ folder to GitHub repo root
2. Add all 5 Railway environment variables above
3. Add /api/research endpoint to app.py (see below)
4. Update /health to report research_bots status
5. Update index.html to call /api/research and merge results
6. Deploy — Railway redeploys automatically


ADD THIS TO app.py (after /api/technicals endpoint)
────────────────────────────────────────────────────
"""

APP_PY_ENDPOINT = """
# ── Research bots ──────────────────────────────────────────────
try:
    import sys as _sys
    _sys.path.insert(0, os.path.join(os.path.dirname(__file__), "research_bots"))
    from orchestrator import run_all_bots, run_single_bot
    BOTS_AVAILABLE = True
    log.info("Research bots loaded")
except ImportError as e:
    BOTS_AVAILABLE = False
    log.warning(f"Research bots not available: {e}")


@app.get("/api/research", tags=["Research"])
async def research(
    symbol: str = Query(...),
    bots:   str = Query(default="all"),
):
    \"\"\"
    Run all research bots for a ticker.
    Returns merged signal inputs + bull/bear factors.
    Cached per-bot (15min-6hr depending on bot).
    \"\"\"
    if not BOTS_AVAILABLE:
        raise HTTPException(503, "Research bots not available")

    sym        = normalise_symbol(symbol)
    asset_meta = {}

    # Enrich with sector/type from universe cache
    try:
        universe_raw = await rget("universe:assets")
        if universe_raw:
            universe = json.loads(universe_raw)
            for asset in universe:
                if asset.get("ticker") == sym:
                    asset_meta = asset
                    break
    except Exception:
        pass

    if bots == "all":
        result = await run_all_bots(sym, asset_meta)
        return result.to_dict()
    else:
        single = await run_single_bot(bots.strip(), sym, asset_meta)
        if not single:
            raise HTTPException(404, f"Bot '{bots}' not found")
        return single.to_dict()
"""

HEALTH_UPDATE = """
# Add this line inside your /health endpoint return dict:
"research_bots": BOTS_AVAILABLE,
"bot_count": 7 if BOTS_AVAILABLE else 0,
"""

INDEX_HTML_UPDATE = """
// Add to index.html — find fetchTechnicals() and add this companion:

async function fetchResearch(ticker) {
    try {
        const r = await fetch(`/api/research?symbol=${encodeURIComponent(ticker)}`);
        if (!r.ok) return null;
        const data = await r.json();
        setResearch(data);
        return data;
    } catch(e) {
        console.warn('Research bots unavailable', e);
        return null;
    }
}

// Call both when asset is selected:
fetchTechnicals(ticker);
fetchResearch(ticker);

// In your bull/bear panel, merge real factors over simulated:
const bullFactors = (research?.bull_factors?.length >= 2)
    ? research.bull_factors
    : simulatedBullFactors;
const bearFactors = (research?.bear_factors?.length >= 2)
    ? research.bear_factors
    : simulatedBearFactors;

// Merge real signal inputs (real overwrites simulated):
if (research?.signal_inputs) {
    Object.assign(signals, research.signal_inputs);
}

// Add React state near top of component:
const [research, setResearch] = useState(null);
// Clear on new asset:
setResearch(null);
"""

REQUIREMENTS_UPDATE = """
# No new packages needed — all bots use httpx which is already in requirements.txt
"""

TEST_URLS = """
After deploy, test these in order:
  /health                          → should show "research_bots": true
  /api/research?symbol=AAPL        → US stock test
  /api/research?symbol=TSCO.L      → UK stock test (FMP path)
  /api/research?symbol=BTC-USD     → Crypto test (most bots skip, graceful)
  /api/research?symbol=AAPL&bots=NewsBot    → Single bot test
"""
