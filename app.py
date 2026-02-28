"""
Stock Price API + User Auth + MarketBrain AI Chat
FastAPI backend with JWT authentication, user accounts, persistent portfolios, and AI chat proxy
"""

import asyncio
import json
import logging
import os
import time
import hashlib
import hmac
import base64
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Dict, List, Optional

import httpx
import redis.asyncio as aioredis
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Query, Depends, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────
REDIS_URL    = os.environ.get("REDIS_URL", "redis://localhost:6379")
SECRET_KEY   = os.environ.get("SECRET_KEY", "change-me-in-production-railway-env")
CACHE_TTL    = 5
TOKEN_TTL    = 60 * 60 * 24 * 30   # 30 days
REQUEST_TIMEOUT = 8

YAHOO_URL          = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
YAHOO_FALLBACK_URL = "https://query2.finance.yahoo.com/v8/finance/chart/{symbol}"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
}

# ── Anthropic AI Config ───────────────────────────────────────
# Set ANTHROPIC_API_KEY in Railway → Settings → Variables
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_MODEL   = "claude-sonnet-4-20250514"

MARKETBRAIN_SYSTEM_PROMPT = """You are MarketBrain AI - the intelligent co-pilot for Market Brain users. Your mission is "Knowledge is Power": empower users with deep insights from the Market Brain system, help them analyze their virtual portfolio, and visualize trades in real-time context.

## CORE IDENTITY & BEHAVIOR
- Voice: Professional yet approachable trading analyst. Clear, concise, structured. Use bullet points for data. Explain *why* something matters.
- Role: Research assistant + portfolio coach + signal interpreter. Never give financial advice. Always frame as "analysis" or "what the data shows".
- Proactive: Surface insights unprompted. Suggest next steps.
- Transparent: Cite data sources (e.g., "Yahoo technicals", "Research v2 cache").
- Educational: Every response teaches Market Brain concepts (signals, bots, scoring, timeframes).

## MARKET BRAIN SYSTEM CONTEXT
Market Brain tracks 1,144 assets (stocks, crypto, forex, ETFs, commodities).

Signal Scoring: -100 to +100 across 5 timeframes:
- Intraday (0-24h), Short Swing (2-5d), Medium Swing (1-4wk), Position (1-6mo), Long Term (6mo+)
- Risk labels: CRITICAL / HIGH / MODERATE / OPPORTUNITY / STRONG

6 Research Bots:
- MacroBot (FRED): sector rotation, rates, inflation
- FundamentalsBot (FMP): revenue growth, debt ratio, P/E
- AnalystBot (FMP): buy/hold/sell consensus
- EarningsBot (FMP+AV): earnings calendar, surprises
- NewsBot (GNews): sentiment, catalysts
- TechnicalLevelsBot (Polygon): support/resistance, golden cross

Key signal inputs: RSI, MACD, volume ratio, price vs MA50/MA200, sentiment, short interest, insider buying, catalyst news, sector flow, revenue growth, debt ratio, days to earnings.

Cap Tiers: Nano / Micro / Small / Mid / Large - affects volatility and stop-loss sizing.
Virtual Portfolio: Users have virtual cash balance, open trades, closed trades, watchlist. 2% risk rule for position sizing.

## RESPONSE FORMAT
Keep responses concise but structured. Use when relevant:
- SIGNAL OVERVIEW: score, bestTF, risk label
- KEY DRIVERS: top bull/bear factors
- TECHNICALS: RSI, MACD, volume, MA levels
- INSIGHT: one clear, actionable observation

For portfolio queries:
- PORTFOLIO SNAPSHOT: balance, P&L, positions
- RISK CHECK: concentration, drawdown, correlation

## FORMATTING
- Use **bold** for key numbers and labels
- Use emoji headers for sections
- Max ~200 words unless deep analysis is requested
- Always end with a question or next step

## CONSTRAINTS
- NO FINANCIAL ADVICE - always frame as analysis only
- If no real data: "Using simulated data - Tier 1 assets have real data prioritized"
- Currency: GBP (UK-focused)
- Confirm trades before logging
"""

# ── In-memory fallbacks ───────────────────────────────────────
_memory_cache: Dict[str, dict] = {}
_memory_users: Dict[str, dict] = {}
_memory_tokens: Dict[str, str] = {}
_memory_portfolios: Dict[str, dict] = {}
redis_client: Optional[aioredis.Redis] = None


# ── Redis ─────────────────────────────────────────────────────
async def get_redis() -> Optional[aioredis.Redis]:
    global redis_client
    if redis_client:
        try:
            await redis_client.ping()
            return redis_client
        except Exception:
            redis_client = None
    try:
        redis_client = await aioredis.from_url(REDIS_URL, decode_responses=True, socket_timeout=2)
        await redis_client.ping()
        log.info("Redis connected")
        return redis_client
    except Exception as e:
        log.warning(f"Redis unavailable ({e}) - using memory")
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
            else: await r.set(key, value)
            return
        except: pass

async def rdel(key: str):
    r = await get_redis()
    if r:
        try: await r.delete(key)
        except: pass


# ── Auth helpers ──────────────────────────────────────────────
def hash_password(password: str) -> str:
    salt = os.urandom(16)
    key = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 100_000)
    return base64.b64encode(salt + key).decode()

def verify_password(password: str, stored: str) -> bool:
    try:
        data = base64.b64decode(stored.encode())
        salt, key = data[:16], data[16:]
        new_key = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 100_000)
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
app = FastAPI(title="Market Brain API", version="2.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])


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

class AIChatRequest(BaseModel):
    messages: list  # [{"role": "user"|"assistant", "content": "..."}]


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
        "name": req.name.strip(),
        "email": email,
        "password_hash": hash_password(req.password),
        "created_at": int(time.time()),
    }
    await save_user(email, user)
    token = make_token(email)
    await save_token(token, email)
    return {"token": token, "user": {"name": user["name"], "email": email}}

@app.post("/api/auth/login", tags=["Auth"])
async def login(req: LoginRequest):
    email = req.email.lower().strip()
    user = await load_user(email)
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
async def save_portfolio_endpoint(data: PortfolioSave, current_user: dict = Depends(get_current_user)):
    await save_portfolio(current_user["email"], data.dict())
    return {"ok": True}


# ── AI Chat endpoint ──────────────────────────────────────────
@app.post("/api/ai-chat", tags=["AI"])
async def ai_chat(req: AIChatRequest, current_user: dict = Depends(get_current_user)):
    """
    Proxy for MarketBrain AI chat. Keeps the Anthropic API key server-side.
    Requires a valid user session (Bearer token) — only logged-in users can call this.

    Body: { "messages": [{"role": "user", "content": "..."}, ...] }
    Returns: { "reply": "..." }
    """
    if not ANTHROPIC_API_KEY:
        return JSONResponse(
            status_code=503,
            content={"error": "AI chat unavailable — ANTHROPIC_API_KEY not set on server."}
        )

    messages = req.messages[-20:]  # cap at 20 messages to control costs

    # Validate
    for msg in messages:
        if msg.get("role") not in ("user", "assistant"):
            return JSONResponse(status_code=400, content={"error": f"Invalid role: {msg.get('role')}"})
        if not isinstance(msg.get("content"), str) or not msg["content"].strip():
            return JSONResponse(status_code=400, content={"error": "Empty message content."})

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                ANTHROPIC_API_URL,
                headers={
                    "x-api-key":         ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type":      "application/json",
                },
                json={
                    "model":    ANTHROPIC_MODEL,
                    "max_tokens": 1000,
                    "system":   MARKETBRAIN_SYSTEM_PROMPT,
                    "messages": messages,
                }
            )

        if resp.status_code != 200:
            log.error(f"Anthropic {resp.status_code}: {resp.text[:200]}")
            return JSONResponse(status_code=502, content={"error": "AI service error. Try again."})

        data = resp.json()
        reply = data.get("content", [{}])[0].get("text", "No response.")
        log.info(f"AI chat ok user={current_user['email']} usage={data.get('usage')}")
        return {"reply": reply}

    except httpx.TimeoutException:
        return JSONResponse(status_code=504, content={"error": "AI request timed out. Try again."})
    except Exception as e:
        log.error(f"AI chat exception: {e}")
        return JSONResponse(status_code=500, content={"error": f"Server error: {str(e)}"})


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
_last_known: Dict[str, dict] = {}

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
            data = r.json()
            result = data.get("chart", {}).get("result", [])
            if not result: continue
            meta = result[0].get("meta", {})
            price = meta.get("regularMarketPrice") or meta.get("previousClose")
            if not price: continue
            prev_close = meta.get("previousClose") or meta.get("chartPreviousClose") or price
            change = price - prev_close
            change_pct = (change / prev_close * 100) if prev_close else 0
            market_state = meta.get("marketState", "CLOSED")
            if market_state == "PRE": price = meta.get("preMarketPrice") or price
            elif market_state == "POST": price = meta.get("postMarketPrice") or price
            return {
                "symbol": symbol, "price": round(float(price), 4),
                "change": round(float(change), 4), "change_pct": round(float(change_pct), 4),
                "prev_close": round(float(prev_close), 4),
                "currency": meta.get("currency", "USD"), "market_state": market_state,
                "exchange": meta.get("exchangeName", ""),
                "name": meta.get("shortName") or meta.get("longName") or symbol,
                "volume": meta.get("regularMarketVolume"),
                "day_high": meta.get("regularMarketDayHigh"),
                "day_low": meta.get("regularMarketDayLow"),
                "fifty_two_week_high": meta.get("fiftyTwoWeekHigh"),
                "fifty_two_week_low": meta.get("fiftyTwoWeekLow"),
                "timestamp": int(time.time()), "source": "yahoo_finance",
            }
        except httpx.TimeoutException: log.warning(f"Timeout: {symbol}")
        except Exception as e: log.warning(f"Error {symbol}: {e}")
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
    for prefix, suffix in [("LON:","\.L"),("EPA:",".PA"),("ETR:",".DE"),("AMS:",".AS"),("TSX:",".TO"),("ASX:",".AX")]:
        if symbol.startswith(prefix): return symbol[len(prefix):] + suffix
    return symbol


# ── Price endpoints ───────────────────────────────────────────
@app.get("/health")
async def health():
    r = await get_redis()
    return {"status": "healthy", "redis": "connected" if r else "memory", "timestamp": int(time.time())}

@app.get("/api/price/{symbol}", tags=["Prices"])
async def get_single_price(symbol: str):
    return await get_price(normalise_symbol(symbol), await get_client())

@app.get("/api/prices", tags=["Prices"])
async def get_multiple_prices(symbols: str = Query(...), delay_ms: int = Query(0)):
    raw = [s.strip() for s in symbols.split(",") if s.strip()]
    if not raw: raise HTTPException(400, "No symbols")
    if len(raw) > 50: raise HTTPException(400, "Max 50 symbols")
    sym_list = [normalise_symbol(s) for s in raw]
    client = await get_client()
    if delay_ms == 0:
        results = await asyncio.gather(*[get_price(s, client) for s in sym_list])
    else:
        results = []
        for s in sym_list:
            results.append(await get_price(s, client))
            await asyncio.sleep(delay_ms / 1000)
    return {"symbols": sym_list, "count": len(results), "timestamp": int(time.time()), "data": {r["symbol"]: r for r in results}}

@app.get("/api/search", tags=["Prices"])
async def search_symbol(q: str = Query(...)):
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(f"https://query1.finance.yahoo.com/v1/finance/search?q={q}&quotesCount=8", headers=HEADERS, timeout=8)
            quotes = r.json().get("quotes", [])
            return {"query": q, "results": [{"symbol": q["symbol"], "name": q.get("shortname") or q.get("longname"), "exchange": q.get("exchDisp")} for q in quotes if q.get("symbol")]}
    except Exception as e:
        raise HTTPException(500, f"Search failed: {e}")


# ── WebSocket ─────────────────────────────────────────────────
class ConnectionManager:
    def __init__(self): self.active: Dict[str, List[WebSocket]] = {}
    async def connect(self, ws: WebSocket, symbol: str):
        await ws.accept(); self.active.setdefault(symbol, []).append(ws)
    def disconnect(self, ws: WebSocket, symbol: str):
        if symbol in self.active:
            self.active[symbol] = [w for w in self.active[symbol] if w != ws]
            if not self.active[symbol]: del self.active[symbol]

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
    except Exception as e: log.error(f"WS error {symbol}: {e}")
    finally: manager.disconnect(websocket, symbol)


# ── Static / frontend ─────────────────────────────────────────
static_dir = Path("static")
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/{full_path:path}", include_in_schema=False)
async def serve_frontend(full_path: str):
    index = Path("static/index.html")
    if index.exists(): return FileResponse(index)
    return {"status": "ok", "docs": "/docs"}


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=False, log_level="info")
