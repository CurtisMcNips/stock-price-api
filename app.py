"""
Market Brain — Main API v3
FastAPI backend with JWT auth, portfolios, live prices,
technicals, research bots, universe + ingest endpoints.
"""

import asyncio
import json
import logging
import os
import sys
import time
import hashlib
import hmac
import base64
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Dict, List, Optional

import httpx
import redis.asyncio as aioredis
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Query, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────
REDIS_URL       = os.environ.get("REDIS_URL",       "redis://localhost:6379")
SECRET_KEY      = os.environ.get("SECRET_KEY",      "change-me-in-production-railway-env")
INGEST_API_KEY  = os.environ.get("INGEST_API_KEY",  "mb-ingest-secret")
CACHE_TTL       = 5
TOKEN_TTL       = 60 * 60 * 24 * 30   # 30 days
REQUEST_TIMEOUT = 8

YAHOO_URL          = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
YAHOO_FALLBACK_URL = "https://query2.finance.yahoo.com/v8/finance/chart/{symbol}"
HEADERS = {
    "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept":          "application/json",
    "Accept-Language": "en-US,en;q=0.9",
}

# ── Research bots (optional — graceful fallback if missing) ───
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "research_bots"))
try:
    from orchestrator import run_all_bots, run_single_bot
    BOTS_AVAILABLE = True
    log.info("Research bots loaded successfully")
except ImportError as e:
    BOTS_AVAILABLE = False
    log.warning(f"Research bots not available: {e}")

# ── In-memory fallbacks ───────────────────────────────────────
_memory_cache: Dict[str, dict]      = {}
_memory_users: Dict[str, dict]      = {}
_memory_tokens: Dict[str, str]      = {}
_memory_portfolios: Dict[str, dict] = {}
redis_client: Optional[aioredis.Redis] = None


# ── Redis helpers ─────────────────────────────────────────────
async def get_redis() -> Optional[aioredis.Redis]:
    global redis_client
    if redis_client:
        try:
            await redis_client.ping()
            return redis_client
        except Exception:
            redis_client = None
    try:
        redis_client = await aioredis.from_url(
            REDIS_URL, decode_responses=True, socket_timeout=2
        )
        await redis_client.ping()
        log.info("Redis connected")
        return redis_client
    except Exception as e:
        log.warning(f"Redis unavailable ({e}) — using memory fallback")
        return None

async def rget(key: str) -> Optional[str]:
    r = await get_redis()
    if r:
        try: return await r.get(key)
        except: pass
    return None

async def rset(key: str, value: str, ttl: int = 0):
    r = await get_redis()
    if r:
        try:
            if ttl: await r.setex(key, ttl, value)
            else:   await r.set(key, value)
            return
        except: pass

async def rdel(key: str):
    r = await get_redis()
    if r:
        try: await r.delete(key)
        except: pass

async def rscan(pattern: str) -> List[str]:
    r = await get_redis()
    if r:
        try:
            keys = []
            async for key in r.scan_iter(pattern):
                keys.append(key)
            return keys
        except: pass
    return []


# ── Auth helpers ──────────────────────────────────────────────
def hash_password(password: str) -> str:
    salt = os.urandom(16)
    key  = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 100_000)
    return base64.b64encode(salt + key).decode()

def verify_password(password: str, stored: str) -> bool:
    try:
        data      = base64.b64decode(stored.encode())
        salt, key = data[:16], data[16:]
        new_key   = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 100_000)
        return hmac.compare_digest(key, new_key)
    except:
        return False

def make_token(email: str) -> str:
    raw = f"{email}:{time.time()}:{os.urandom(16).hex()}"
    return base64.b64encode(raw.encode()).decode().replace("=", "")

async def save_user(email: str, user: dict):
    await rset(f"user:{email}", json.dumps(user))
    _memory_users[email] = user

async def load_user(email: str) -> Optional[dict]:
    val = await rget(f"user:{email}")
    if val: return json.loads(val)
    return _memory_users.get(email)

async def save_token(token: str, email: str):
    await rset(f"token:{token}", email, TOKEN_TTL)
    _memory_tokens[token] = email

async def load_token(token: str) -> Optional[str]:
    val = await rget(f"token:{token}")
    if val: return val
    return _memory_tokens.get(token)

async def delete_token(token: str):
    await rdel(f"token:{token}")
    _memory_tokens.pop(token, None)

async def save_portfolio(email: str, portfolio: dict):
    await rset(f"portfolio:{email}", json.dumps(portfolio))
    _memory_portfolios[email] = portfolio

async def load_portfolio(email: str) -> dict:
    val = await rget(f"portfolio:{email}")
    if val: return json.loads(val)
    if email in _memory_portfolios: return _memory_portfolios[email]
    return {"trades": [], "watchItems": [], "balance": 1000, "startBalance": 1000}

async def get_current_user(authorization: Optional[str] = Header(None)) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Not authenticated")
    token = authorization[7:]
    email = await load_token(token)
    if not email:
        raise HTTPException(401, "Invalid or expired token")
    user = await load_user(email)
    if not user:
        raise HTTPException(401, "User not found")
    return user


# ── Lifespan ──────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    await get_redis()
    Path("static").mkdir(exist_ok=True)
    yield
    if redis_client:
        await redis_client.aclose()


# ── App ───────────────────────────────────────────────────────
app = FastAPI(title="Market Brain API", version="3.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)


# ── Pydantic models ───────────────────────────────────────────
class RegisterRequest(BaseModel):
    name: str
    email: str
    password: str

class LoginRequest(BaseModel):
    email: str
    password: str

class PortfolioSave(BaseModel):
    trades: list
    watchItems: list
    balance: float
    startBalance: float

class IngestRequest(BaseModel):
    api_key: str
    assets: List[dict]


# ── Auth endpoints ────────────────────────────────────────────
@app.post("/api/auth/register", tags=["Auth"])
async def register(req: RegisterRequest):
    email = req.email.lower().strip()
    if len(req.password) < 6:
        raise HTTPException(400, "Password must be at least 6 characters")
    existing = await load_user(email)
    if existing:
        raise HTTPException(409, "Email already registered")
    user = {
        "name":          req.name.strip(),
        "email":         email,
        "password_hash": hash_password(req.password),
        "created_at":    int(time.time()),
    }
    await save_user(email, user)
    token = make_token(email)
    await save_token(token, email)
    return {"token": token, "user": {"name": user["name"], "email": email}}

@app.post("/api/auth/login", tags=["Auth"])
async def login(req: LoginRequest):
    email = req.email.lower().strip()
    user  = await load_user(email)
    if not user or not verify_password(req.password, user["password_hash"]):
        raise HTTPException(401, "Invalid email or password")
    token = make_token(email)
    await save_token(token, email)
    return {"token": token, "user": {"name": user["name"], "email": email}}

@app.post("/api/auth/logout", tags=["Auth"])
async def logout(authorization: Optional[str] = Header(None)):
    if authorization and authorization.startswith("Bearer "):
        await delete_token(authorization[7:])
    return {"ok": True}

@app.get("/api/auth/me", tags=["Auth"])
async def me(current_user: dict = Depends(get_current_user)):
    return {"name": current_user["name"], "email": current_user["email"]}


# ── Portfolio endpoints ───────────────────────────────────────
@app.get("/api/portfolio", tags=["Portfolio"])
async def get_portfolio(current_user: dict = Depends(get_current_user)):
    return await load_portfolio(current_user["email"])

@app.post("/api/portfolio", tags=["Portfolio"])
async def save_portfolio_endpoint(
    data: PortfolioSave,
    current_user: dict = Depends(get_current_user),
):
    await save_portfolio(current_user["email"], data.dict())
    return {"ok": True}


# ── Price cache ───────────────────────────────────────────────
async def cache_get(key: str) -> Optional[dict]:
    val = await rget(key)
    if val: return json.loads(val)
    entry = _memory_cache.get(key)
    if entry and (time.time() - entry["_ts"]) < CACHE_TTL:
        return entry["data"]
    return None

async def cache_set(key: str, data: dict):
    await rset(f"cache:{key}", json.dumps(data), CACHE_TTL)
    _memory_cache[key] = {"data": data, "_ts": time.time()}


# ── Yahoo Finance ─────────────────────────────────────────────
_http_client: Optional[httpx.AsyncClient] = None
_last_known:  Dict[str, dict]             = {}

async def get_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(
            limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
            timeout=REQUEST_TIMEOUT,
        )
    return _http_client

async def fetch_yahoo(symbol: str, client: httpx.AsyncClient) -> Optional[dict]:
    for url in [YAHOO_URL.format(symbol=symbol), YAHOO_FALLBACK_URL.format(symbol=symbol)]:
        try:
            r = await client.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            if r.status_code != 200: continue
            data         = r.json()
            result       = data.get("chart", {}).get("result", [])
            if not result: continue
            meta         = result[0].get("meta", {})
            price        = meta.get("regularMarketPrice") or meta.get("previousClose")
            if not price: continue
            prev_close   = meta.get("previousClose") or meta.get("chartPreviousClose") or price
            change       = price - prev_close
            change_pct   = (change / prev_close * 100) if prev_close else 0
            market_state = meta.get("marketState", "CLOSED")
            if market_state == "PRE":
                price = meta.get("preMarketPrice")  or price
            elif market_state == "POST":
                price = meta.get("postMarketPrice") or price
            return {
                "symbol":             symbol,
                "price":              round(float(price), 4),
                "change":             round(float(change), 4),
                "change_pct":         round(float(change_pct), 4),
                "prev_close":         round(float(prev_close), 4),
                "currency":           meta.get("currency", "USD"),
                "market_state":       market_state,
                "exchange":           meta.get("exchangeName", ""),
                "name":               meta.get("shortName") or meta.get("longName") or symbol,
                "volume":             meta.get("regularMarketVolume"),
                "day_high":           meta.get("regularMarketDayHigh"),
                "day_low":            meta.get("regularMarketDayLow"),
                "fifty_two_week_high": meta.get("fiftyTwoWeekHigh"),
                "fifty_two_week_low":  meta.get("fiftyTwoWeekLow"),
                "timestamp":          int(time.time()),
                "source":             "yahoo_finance",
            }
        except httpx.TimeoutException: log.warning(f"Timeout: {symbol}")
        except Exception as e:         log.warning(f"Error {symbol}: {e}")
    return None

async def get_price(symbol: str, client: httpx.AsyncClient) -> dict:
    cached = await cache_get(f"price:{symbol}")
    if cached: return {**cached, "cached": True}
    data = await fetch_yahoo(symbol, client)
    if data:
        _last_known[symbol] = data
        await cache_set(f"price:{symbol}", data)
        return {**data, "cached": False}
    if symbol in _last_known:
        return {**_last_known[symbol], "stale": True, "cached": False}
    return {"symbol": symbol, "price": None, "error": "Unavailable", "timestamp": int(time.time())}

def normalise_symbol(symbol: str) -> str:
    symbol = symbol.upper().strip()
    for prefix, suffix in [
        ("LON:", ".L"), ("EPA:", ".PA"), ("ETR:", ".DE"),
        ("AMS:", ".AS"), ("TSX:", ".TO"), ("ASX:", ".AX"),
    ]:
        if symbol.startswith(prefix):
            return symbol[len(prefix):] + suffix
    return symbol


# ── Health endpoint ───────────────────────────────────────────
@app.get("/health")
async def health():
    r = await get_redis()
    return {
        "status":        "healthy",
        "redis":         "connected" if r else "memory",
        "timestamp":     int(time.time()),
        "research_bots": BOTS_AVAILABLE,
        "bot_count":     7 if BOTS_AVAILABLE else 0,
        "version":       "3.0.0",
    }


# ── Price endpoints ───────────────────────────────────────────
@app.get("/api/price/{symbol}", tags=["Prices"])
async def get_single_price(symbol: str):
    return await get_price(normalise_symbol(symbol), await get_client())

@app.get("/api/prices", tags=["Prices"])
async def get_multiple_prices(
    symbols:  str = Query(...),
    delay_ms: int = Query(0),
):
    raw = [s.strip() for s in symbols.split(",") if s.strip()]
    if not raw:       raise HTTPException(400, "No symbols")
    if len(raw) > 50: raise HTTPException(400, "Max 50 symbols")
    sym_list = [normalise_symbol(s) for s in raw]
    client   = await get_client()
    if delay_ms == 0:
        results = await asyncio.gather(*[get_price(s, client) for s in sym_list])
    else:
        results = []
        for s in sym_list:
            results.append(await get_price(s, client))
            await asyncio.sleep(delay_ms / 1000)
    return {
        "symbols":   sym_list,
        "count":     len(results),
        "timestamp": int(time.time()),
        "data":      {r["symbol"]: r for r in results},
    }

@app.get("/api/search", tags=["Prices"])
async def search_symbol(q: str = Query(...)):
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(
                f"https://query1.finance.yahoo.com/v1/finance/search?q={q}&quotesCount=8",
                headers=HEADERS, timeout=8,
            )
            quotes = r.json().get("quotes", [])
            return {
                "query": q,
                "results": [
                    {
                        "symbol":   qt["symbol"],
                        "name":     qt.get("shortname") or qt.get("longname"),
                        "exchange": qt.get("exchDisp"),
                    }
                    for qt in quotes if qt.get("symbol")
                ],
            }
    except Exception as e:
        raise HTTPException(500, f"Search failed: {e}")


# ── Universe endpoint ─────────────────────────────────────────
@app.get("/api/universe", tags=["Universe"])
async def get_universe():
    """Return all assets stored in Redis by the ingestion engine."""
    try:
        raw = await rget("universe:assets")
        if raw:
            assets = json.loads(raw)
            return {"count": len(assets), "assets": assets}

        # Fallback: scan individual asset keys
        keys = await rscan("asset:*")
        if keys:
            assets = []
            r = await get_redis()
            if r:
                vals = await r.mget(keys)
                for v in vals:
                    if v:
                        try: assets.append(json.loads(v))
                        except: pass
            if assets:
                return {"count": len(assets), "assets": assets}

        return {"count": 0, "assets": []}
    except Exception as e:
        log.error(f"Universe fetch error: {e}")
        return {"count": 0, "assets": [], "error": str(e)}


# ── Ingest endpoint ───────────────────────────────────────────
@app.post("/api/ingest", tags=["Ingest"])
async def ingest_assets(req: IngestRequest):
    """Receive asset data from the ingestion engine and store in Redis."""
    if req.api_key != INGEST_API_KEY:
        raise HTTPException(403, "Invalid API key")
    if not req.assets:
        raise HTTPException(400, "No assets provided")

    try:
        # Bulk list for fast /api/universe reads
        await rset("universe:assets", json.dumps(req.assets))

        # Individual keys for per-ticker lookups
        r = await get_redis()
        if r:
            pipe = r.pipeline()
            for asset in req.assets:
                ticker = asset.get("ticker")
                if ticker:
                    pipe.set(f"asset:{ticker}", json.dumps(asset))
            await pipe.execute()

        log.info(f"Ingested {len(req.assets)} assets")
        return {"ok": True, "stored": len(req.assets), "ts": int(time.time())}
    except Exception as e:
        log.error(f"Ingest error: {e}")
        raise HTTPException(500, f"Ingest failed: {e}")


# ── Technicals endpoint ───────────────────────────────────────
@app.get("/api/technicals", tags=["Data"])
async def get_technicals(symbols: str = Query(...)):
    """RSI, MACD, volume ratio, price vs MA50/MA200 for one or more symbols."""
    syms = [normalise_symbol(s.strip()) for s in symbols.split(",") if s.strip()]
    if not syms:       raise HTTPException(400, "No symbols provided")
    if len(syms) > 50: raise HTTPException(400, "Max 50 symbols per request")

    results = {}

    async def fetch_one(sym: str):
        cached = await cache_get(f"tech:{sym}")
        if cached:
            results[sym] = cached
            return

        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range=6mo&interval=1d"
        fb  = f"https://query2.finance.yahoo.com/v8/finance/chart/{sym}?range=6mo&interval=1d"
        client = await get_client()

        for endpoint in [url, fb]:
            try:
                r = await client.get(endpoint, headers=HEADERS, timeout=REQUEST_TIMEOUT)
                if r.status_code != 200: continue
                data   = r.json()
                result = data.get("chart", {}).get("result", [])
                if not result: continue

                quote   = result[0].get("indicators", {}).get("quote", [{}])[0]
                closes  = [c for c in (quote.get("close",  []) or []) if c is not None]
                volumes = [v for v in (quote.get("volume", []) or []) if v is not None]

                if len(closes) < 14:
                    break

                # RSI-14
                deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
                gains  = [d if d > 0 else 0   for d in deltas]
                losses = [abs(d) if d < 0 else 0 for d in deltas]
                avg_g  = sum(gains[-14:])  / 14
                avg_l  = sum(losses[-14:]) / 14
                rsi    = 100 - (100 / (1 + avg_g / avg_l)) if avg_l != 0 else 50

                # MACD (12/26 EMA)
                def ema(prices, period):
                    k = 2 / (period + 1); val = prices[0]
                    for p in prices[1:]: val = p * k + val * (1 - k)
                    return val

                if len(closes) >= 26:
                    macd_norm = (ema(closes[-26:], 12) - ema(closes[-26:], 26)) / closes[-1] * 100
                else:
                    macd_norm = 0.0

                # Volume ratio vs 20-day avg
                vol_ratio = (
                    volumes[-1] / (sum(volumes[-20:]) / 20)
                    if len(volumes) >= 20 and sum(volumes[-20:]) > 0 else 1.0
                )

                # Price vs moving averages
                ma50  = sum(closes[-50:])  / min(50,  len(closes))
                ma200 = sum(closes[-200:]) / min(200, len(closes))
                price_vs_ma50  = (closes[-1] - ma50)  / ma50  * 100 if ma50  else 0
                price_vs_ma200 = (closes[-1] - ma200) / ma200 * 100 if ma200 else 0

                tech = {
                    "rsi":           round(rsi,            2),
                    "macd":          round(macd_norm,       4),
                    "volume_ratio":  round(vol_ratio,       3),
                    "price_vs_ma50": round(price_vs_ma50,   2),
                    "price_vs_ma200":round(price_vs_ma200,  2),
                }
                await cache_set(f"tech:{sym}", tech)
                results[sym] = tech
                return
            except Exception as e:
                log.warning(f"Technicals error {sym}: {e}")
                continue
        results[sym] = None

    await asyncio.gather(*[fetch_one(s) for s in syms])
    return results


# ── Research endpoint ─────────────────────────────────────────
@app.get("/api/research", tags=["Research"])
async def research(
    symbol: str = Query(...),
    bots:   str = Query(default="all"),
):
    """
    Run research bots for a ticker.
    Returns signal_inputs, bull_factors, bear_factors, and per-bot results.
    Query ?bots=all or ?bots=NewsBot for a single bot.
    """
    if not BOTS_AVAILABLE:
        raise HTTPException(503, "Research bots not available — check Railway deployment logs")

    sym        = normalise_symbol(symbol)
    asset_meta = {}

    try:
        universe_raw = await rget("universe:assets")
        if universe_raw:
            for asset in json.loads(universe_raw):
                if asset.get("ticker") == sym:
                    asset_meta = asset
                    break
    except Exception:
        pass

    try:
        if bots.strip().lower() == "all":
            result = await run_all_bots(sym, asset_meta)
            return result.to_dict()
        else:
            single = await run_single_bot(bots.strip(), sym, asset_meta)
            if not single:
                raise HTTPException(404, f"Bot '{bots}' not found")
            return single.to_dict()
    except HTTPException:
        raise
    except Exception as e:
        log.error(f"Research bot error {sym}: {e}")
        raise HTTPException(500, f"Research failed: {e}")


# ── WebSocket ─────────────────────────────────────────────────
class ConnectionManager:
    def __init__(self): self.active: Dict[str, List[WebSocket]] = {}

    async def connect(self, ws: WebSocket, symbol: str):
        await ws.accept()
        self.active.setdefault(symbol, []).append(ws)

    def disconnect(self, ws: WebSocket, symbol: str):
        if symbol in self.active:
            self.active[symbol] = [w for w in self.active[symbol] if w != ws]
            if not self.active[symbol]:
                del self.active[symbol]

manager = ConnectionManager()

@app.websocket("/ws/{symbol}")
async def websocket_price(websocket: WebSocket, symbol: str):
    symbol = normalise_symbol(symbol)
    await manager.connect(websocket, symbol)
    client = await get_client()
    try:
        while True:
            data = await get_price(symbol, client)
            await websocket.send_json({**data, "ws": True})
            await asyncio.sleep(3)
    except WebSocketDisconnect: pass
    except Exception as e:      log.error(f"WS error {symbol}: {e}")
    finally: manager.disconnect(websocket, symbol)


# ── Static / frontend ─────────────────────────────────────────
static_dir = Path("static")
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/{full_path:path}", include_in_schema=False)
async def serve_frontend(full_path: str):
    index = Path("static/index.html")
    if index.exists():
        return FileResponse(index)
    return {"status": "ok", "docs": "/docs"}


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=False, log_level="info")
