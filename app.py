"""
Market Brain API v3.0
FastAPI backend — Auth + Prices + Portfolio + Chief of Staff (Catalyst Intelligence)
"""

import asyncio
import json
import logging
import os
import time
import hashlib
import hmac
import base64
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Dict, List, Optional

import httpx
import redis.asyncio as aioredis
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Query, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────
REDIS_URL       = os.environ.get("REDIS_URL", "redis://localhost:6379")
SECRET_KEY      = os.environ.get("SECRET_KEY", "change-me-in-production-railway-env")
ANTHROPIC_KEY   = os.environ.get("ANTHROPIC_API_KEY", "")
CACHE_TTL       = 5
TOKEN_TTL       = 60 * 60 * 24 * 30   # 30 days
REQUEST_TIMEOUT = 8

YAHOO_URL          = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
YAHOO_FALLBACK_URL = "https://query2.finance.yahoo.com/v8/finance/chart/{symbol}"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
}

# ── In-memory fallbacks ───────────────────────────────────────
_memory_cache: Dict[str, dict]      = {}
_memory_users: Dict[str, dict]      = {}
_memory_tokens: Dict[str, str]      = {}
_memory_portfolios: Dict[str, dict] = {}
_memory_catalysts: Dict[str, dict]  = {}
redis_client: Optional[aioredis.Redis] = None


# ═══════════════════════════════════════════════════════════════
# REDIS HELPERS
# ═══════════════════════════════════════════════════════════════

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

async def rkeys(pattern: str) -> List[str]:
    r = await get_redis()
    if r:
        try: return await r.keys(pattern)
        except: pass
    return []


# ═══════════════════════════════════════════════════════════════
# AUTH HELPERS
# ═══════════════════════════════════════════════════════════════

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


# ═══════════════════════════════════════════════════════════════
# CHIEF OF STAFF — CATALYST LOGIC
# ═══════════════════════════════════════════════════════════════

EXPIRY_HOURS = {"geo": 60, "macro": 36, "sector": 18, "asset": 9}

DECAY_RATES = {
    "spark": 3.0, "confirmed": 1.5, "escalation": 1.0,
    "structural": 0.5, "regime": 0.2,
}

def determine_wave(renewal_count: int, confidence: float) -> str:
    if renewal_count == 0:                              return "spark"
    if renewal_count == 1 and confidence > 45:          return "confirmed"
    if renewal_count >= 2 and confidence > 60:          return "escalation"
    if renewal_count >= 4 and confidence > 75:          return "structural"
    if renewal_count >= 6 and confidence > 88:          return "regime"
    return "spark"

def compute_confidence(signals: list) -> float:
    if not signals: return 0.0
    weights = {
        "geo": 1.4, "macro": 1.2, "fundamental": 1.1, "sector": 1.0,
        "asset": 0.9, "technical": 0.8, "manual": 0.8, "news": 0.7,
    }
    total, weighted = 0.0, 0.0
    for s in signals:
        w = weights.get(s.get("source", "asset"), 0.8)
        weighted += s.get("strength", 50) * w
        total += w
    raw = (weighted / total) if total else 0.0
    sources = set(s.get("source") for s in signals)
    if len(sources) >= 3: raw = min(100, raw * 1.15)
    if len(sources) >= 5: raw = min(100, raw * 1.10)
    return round(raw, 1)

async def load_catalyst(cat_id: str) -> Optional[dict]:
    val = await rget(f"catalyst:{cat_id}")
    if val: return json.loads(val)
    return _memory_catalysts.get(cat_id)

async def save_catalyst(cat: dict):
    await rset(f"catalyst:{cat['id']}", json.dumps(cat))
    _memory_catalysts[cat["id"]] = cat

async def all_catalysts(status: str = "active") -> List[dict]:
    results = []
    seen = set()
    keys = await rkeys("catalyst:*")
    for key in keys:
        val = await rget(key)
        if not val: continue
        try:
            cat = json.loads(val)
            if cat["id"] not in seen:
                seen.add(cat["id"])
                if status == "all" or cat.get("status") == status:
                    results.append(cat)
        except: pass
    for cat_id, cat in _memory_catalysts.items():
        if cat_id not in seen:
            if status == "all" or cat.get("status") == status:
                results.append(cat)
    results.sort(key=lambda c: c.get("confidence", 0), reverse=True)
    return results

async def apply_decay():
    """Decay confidence on all active catalysts. Runs every 15 minutes."""
    now = time.time()
    keys = await rkeys("catalyst:*")
    decayed = 0
    for key in keys:
        val = await rget(key)
        if not val: continue
        try:
            cat = json.loads(val)
            if cat.get("status") != "active": continue
            hours = (now - cat["updated_at"]) / 3600
            rate  = DECAY_RATES.get(cat.get("wave", "spark"), 2.0)
            cat["confidence"] = max(0, round(cat["confidence"] - rate * hours, 1))
            cat["updated_at"] = now
            if cat["confidence"] < 15 and now > cat["expires_at"]:
                cat["status"] = "expired"
                log.info(f"Catalyst {cat['id']} expired (conf={cat['confidence']})")
            await save_catalyst(cat)
            decayed += 1
        except Exception as e:
            log.warning(f"Decay error on {key}: {e}")
    log.info(f"Decay applied to {decayed} catalysts")


# ═══════════════════════════════════════════════════════════════
# LIFESPAN
# ═══════════════════════════════════════════════════════════════

async def decay_loop():
    while True:
        await asyncio.sleep(900)  # 15 minutes
        try: await apply_decay()
        except Exception as e: log.warning(f"Decay loop error: {e}")
        try: await run_signal_spotter()
        except Exception as e: log.warning(f"Spotter loop error: {e}")

@asynccontextmanager
async def lifespan(app: FastAPI):
    await get_redis()
    Path("static").mkdir(exist_ok=True)
    asyncio.create_task(decay_loop())
    log.info("Market Brain API v3.0 started")
    yield
    if redis_client:
        await redis_client.aclose()


# ═══════════════════════════════════════════════════════════════
# APP
# ═══════════════════════════════════════════════════════════════

app = FastAPI(title="Market Brain API", version="3.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)


# ═══════════════════════════════════════════════════════════════
# PYDANTIC MODELS
# ═══════════════════════════════════════════════════════════════

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

class CatalystSignal(BaseModel):
    source: str                       # macro / news / technical / geo / manual / fundamental
    asset: Optional[str] = None
    sector: Optional[str] = None
    direction: str                    # bullish / bearish / neutral
    strength: float                   # 0–100
    summary: str
    tags: List[str] = []
    catalyst_type: str = "asset"      # geo / macro / sector / asset

class AttentionUpdate(BaseModel):
    catalyst_id: str
    state: str                        # watch / focus / actioned
    asset: Optional[str] = None
    direction: Optional[str] = None
    entry_price: Optional[float] = None
    goal_style: Optional[str] = None  # in_out / swing / long_term
    goal_timeline: Optional[str] = None
    notes: Optional[str] = None

class ChatRequest(BaseModel):
    message: str
    history: Optional[list] = []


# ═══════════════════════════════════════════════════════════════
# AUTH ENDPOINTS
# ═══════════════════════════════════════════════════════════════

@app.post("/api/auth/register", tags=["Auth"])
async def register(req: RegisterRequest):
    email = req.email.lower().strip()
    if len(req.password) < 6:
        raise HTTPException(400, "Password must be at least 6 characters")
    if await load_user(email):
        raise HTTPException(409, "Email already registered")
    user = {
        "name": req.name.strip(), "email": email,
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


# ═══════════════════════════════════════════════════════════════
# PORTFOLIO ENDPOINTS
# ═══════════════════════════════════════════════════════════════

@app.get("/api/portfolio", tags=["Portfolio"])
async def get_portfolio(current_user: dict = Depends(get_current_user)):
    return await load_portfolio(current_user["email"])

@app.post("/api/portfolio", tags=["Portfolio"])
async def save_portfolio_endpoint(data: PortfolioSave, current_user: dict = Depends(get_current_user)):
    await save_portfolio(current_user["email"], data.dict())
    return {"ok": True}


# ═══════════════════════════════════════════════════════════════
# PRICE CACHE + YAHOO FINANCE
# ═══════════════════════════════════════════════════════════════

async def cache_get(key: str) -> Optional[dict]:
    val = await rget(f"cache:{key}")
    if val: return json.loads(val)
    entry = _memory_cache.get(key)
    if entry and (time.time() - entry["_ts"]) < CACHE_TTL:
        return entry["data"]
    return None

async def cache_set(key: str, data: dict):
    await rset(f"cache:{key}", json.dumps(data), CACHE_TTL)
    _memory_cache[key] = {"data": data, "_ts": time.time()}

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
            if market_state == "PRE":   price = meta.get("preMarketPrice") or price
            elif market_state == "POST": price = meta.get("postMarketPrice") or price
            return {
                "symbol": symbol, "price": round(float(price), 4),
                "change": round(float(change), 4),
                "change_pct": round(float(change_pct), 4),
                "prev_close": round(float(prev_close), 4),
                "currency": meta.get("currency", "USD"),
                "market_state": market_state,
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


# ═══════════════════════════════════════════════════════════════
# RESEARCH BOTS
# ═══════════════════════════════════════════════════════════════

try:
    import sys as _sys
    _sys.path.insert(0, os.path.join(os.path.dirname(__file__), "research_bots"))
    from orchestrator import run_all_bots as _run_all_bots, run_single_bot as _run_single_bot
    BOTS_AVAILABLE = True
    log.info("Research bots loaded")
except ImportError as e:
    BOTS_AVAILABLE = False
    log.warning(f"Research bots not available: {e}")


@app.get("/api/research", tags=["Research"])
async def research(
    symbol: str = Query(...),
    bots:   str = Query(default="all"),
    current_user: dict = Depends(get_current_user),
):
    """
    Run research bots for a ticker. Returns merged signal inputs + bull/bear factors.
    Served from bot-level cache (2min–6hr per bot). Does NOT post to CoS
    (that is the sweep engine's job — this is user-triggered, on-demand only).
    """
    if not BOTS_AVAILABLE:
        raise HTTPException(503, "Research bots not available — check research_bots/ folder")

    sym        = normalise_symbol(symbol)
    asset_meta = {}

    # Enrich with sector/type from universe if available
    try:
        universe_raw = await rget("universe:assets")
        if universe_raw:
            for asset in json.loads(universe_raw):
                if asset.get("ticker") == sym:
                    asset_meta = asset
                    break
    except Exception:
        pass

    if bots == "all":
        result = await _run_all_bots(sym, asset_meta, post_to_cos=False)
        return result.to_dict()
    else:
        single = await _run_single_bot(bots.strip(), sym, asset_meta)
        if not single:
            raise HTTPException(404, f"Bot '{bots}' not found")
        return single.to_dict()


# ═══════════════════════════════════════════════════════════════
# PRICE ENDPOINTS
# ═══════════════════════════════════════════════════════════════

@app.get("/health")
async def health():
    r = await get_redis()
    cats = await all_catalysts("active")
    return {
        "status": "healthy",
        "version": "3.0.0",
        "redis": "connected" if r else "memory",
        "active_catalysts": len(cats),
        "research_bots":    BOTS_AVAILABLE,
        "bot_count":        7 if BOTS_AVAILABLE else 0,
        "timestamp": int(time.time()),
    }

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
    return {
        "symbols": sym_list, "count": len(results),
        "timestamp": int(time.time()),
        "data": {r["symbol"]: r for r in results},
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
                        "symbol": item["symbol"],
                        "name": item.get("shortname") or item.get("longname"),
                        "exchange": item.get("exchDisp"),
                    }
                    for item in quotes if item.get("symbol")
                ],
            }
    except Exception as e:
        raise HTTPException(500, f"Search failed: {e}")


# ═══════════════════════════════════════════════════════════════
# WEBSOCKET
# ═══════════════════════════════════════════════════════════════

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


# ═══════════════════════════════════════════════════════════════
# SEBASTIAN — INTERPRETATION ENGINE
# ═══════════════════════════════════════════════════════════════

@app.post("/api/sebastian/interpret", tags=["Sebastian"])
async def sebastian_interpret(req: ChatRequest, current_user: dict = Depends(get_current_user)):
    """
    Sebastian interprets catalyst clusters, wave transitions, and signal context.
    Not a chatbot — a structured interpretation engine.
    """
    if not ANTHROPIC_KEY:
        raise HTTPException(503, "Sebastian not configured — set ANTHROPIC_API_KEY")
    try:
        # Enrich context with live catalyst state
        active_cats = await all_catalysts("active")
        cat_summary = []
        for c in active_cats[:5]:
            cat_summary.append(
                f"[{c['wave'].upper()}] {c['title']} — conf={c['confidence']} dir={c['direction']} assets={c.get('assets',[])}"
            )
        catalyst_context = "\n".join(cat_summary) if cat_summary else "No active catalysts."

        messages = []
        for h in (req.history or [])[-6:]:
            role = h.get("role", "user")
            if role in ("user", "assistant"):
                messages.append({"role": role, "content": h.get("content", "")})
        messages.append({"role": "user", "content": req.message})

        async with httpx.AsyncClient() as client:
            r = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-sonnet-4-20250514",
                    "max_tokens": 800,
                    "system": (
                        "You are Sebastian, the interpretation engine for Market Brain. "
                        "You are not a chatbot. You interpret catalyst clusters, wave transitions, "
                        "confidence reasoning, and signal alignment. "
                        "You structure your output clearly — wave state, confidence rationale, "
                        "signal sources, and what this means for the user's attention. "
                        "Wave states: Spark → Confirmed → Escalation → Structural → Regime. "
                        "Confidence 0-100 decays without new signals. "
                        "Attention: Watch / Focus / Actioned. "
                        "Current live catalyst state:\n" + catalyst_context + "\n"
                        "Be precise. Never generic. Reference specific catalysts when relevant."
                    ),
                    "messages": messages,
                },
                timeout=30,
            )
            data = r.json()
            text = data.get("content", [{}])[0].get("text", "Sebastian unavailable")
            return {"response": text, "engine": "sebastian"}
    except Exception as e:
        log.error(f"Sebastian error: {e}")
        raise HTTPException(500, f"Sebastian error: {e}")


@app.post("/api/sebastian/summarise", tags=["Sebastian"])
async def sebastian_summarise(current_user: dict = Depends(get_current_user)):
    """Generate a structured intelligence brief from all active catalysts."""
    if not ANTHROPIC_KEY:
        return {"brief": "Sebastian not configured.", "engine": "sebastian"}
    active_cats = await all_catalysts("active")
    if not active_cats:
        return {"brief": "No active catalysts to summarise.", "engine": "sebastian"}

    cat_lines = []
    for c in active_cats[:10]:
        cat_lines.append(
            f"- [{c['wave'].upper()} | conf={c['confidence']} | {c['direction']}] "
            f"{c['title']} (assets: {', '.join(c.get('assets', []) or ['sector-wide'])})"
        )
    prompt = "Generate a structured intelligence brief from these active catalysts:\n" + "\n".join(cat_lines)
    prompt += "\n\nFormat: lead with highest conviction catalyst, group by theme, close with attention priorities."

    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                json={
                    "model": "claude-sonnet-4-20250514",
                    "max_tokens": 600,
                    "system": "You are Sebastian, Market Brain's interpretation engine. Generate concise, structured intelligence briefs.",
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=30,
            )
            data = r.json()
            return {"brief": data.get("content", [{}])[0].get("text", ""), "engine": "sebastian", "catalyst_count": len(active_cats)}
    except Exception as e:
        return {"brief": f"Brief unavailable: {e}", "engine": "sebastian"}


# Legacy endpoint — kept for backwards compat
@app.post("/api/ai-chat", tags=["Sebastian"])
async def ai_chat(req: ChatRequest, current_user: dict = Depends(get_current_user)):
    return await sebastian_interpret(req, current_user)


# ═══════════════════════════════════════════════════════════════
# CHIEF OF STAFF — CATALYST ENDPOINTS
# ═══════════════════════════════════════════════════════════════

@app.post("/api/cos/signal", tags=["ChiefOfStaff"])
async def ingest_signal(signal: CatalystSignal, current_user: dict = Depends(get_current_user)):
    """
    Ingest a signal from any source (research bots, MARI, manual, Perplexity).
    Auto-clusters into an existing catalyst if same asset/sector + direction within 6h.
    Otherwise creates a new catalyst at Wave 1 (Spark).
    """
    now = time.time()
    existing_id = None

    active = await all_catalysts("active")
    for cat in active:
        same_asset  = signal.asset  and signal.asset.upper()  in [a.upper() for a in cat.get("assets", [])]
        same_sector = signal.sector and signal.sector.upper() in [s.upper() for s in cat.get("sectors", [])]
        same_dir    = cat.get("direction") == signal.direction
        recent      = (now - cat["detected_at"]) < 21600  # 6h clustering window
        if (same_asset or same_sector) and same_dir and recent:
            existing_id = cat["id"]
            break

    if existing_id:
        cat = await load_catalyst(existing_id)
        cat["signals"].append({
            "source": signal.source, "strength": signal.strength,
            "summary": signal.summary, "tags": signal.tags, "ts": now,
        })
        cat["renewal_count"] += 1
        cat["confidence"]     = compute_confidence(cat["signals"])
        cat["wave"]           = determine_wave(cat["renewal_count"], cat["confidence"])
        cat["updated_at"]     = now
        cat["expires_at"]     = now + EXPIRY_HOURS.get(signal.catalyst_type, 9) * 3600
        cat["status"]         = "active"
        if signal.asset and signal.asset.upper() not in [a.upper() for a in cat["assets"]]:
            cat["assets"].append(signal.asset)
        if signal.sector and signal.sector.upper() not in [s.upper() for s in cat["sectors"]]:
            cat["sectors"].append(signal.sector)
        for tag in signal.tags:
            if tag not in cat["tags"]: cat["tags"].append(tag)
        await save_catalyst(cat)
        log.info(f"Signal merged → {existing_id} | wave={cat['wave']} conf={cat['confidence']}")
        return {
            "action": "merged", "catalyst_id": existing_id,
            "wave": cat["wave"], "confidence": cat["confidence"],
            "renewal_count": cat["renewal_count"],
        }

    # New catalyst
    cat_id = str(uuid.uuid4())[:8]
    cat = {
        "id":            cat_id,
        "title":         signal.summary[:120],
        "type":          signal.catalyst_type,
        "direction":     signal.direction,
        "wave":          "spark",
        "confidence":    round(signal.strength, 1),
        "renewal_count": 0,
        "detected_at":   now,
        "updated_at":    now,
        "expires_at":    now + EXPIRY_HOURS.get(signal.catalyst_type, 9) * 3600,
        "assets":        [signal.asset]  if signal.asset  else [],
        "sectors":       [signal.sector] if signal.sector else [],
        "signals":       [{
            "source": signal.source, "strength": signal.strength,
            "summary": signal.summary, "tags": signal.tags, "ts": now,
        }],
        "summary":       signal.summary,
        "verified":      False,
        "attention":     "watch",
        "status":        "active",
        "tags":          list(signal.tags),
        "position":      None,
        "created_by":    current_user["email"],
    }
    await save_catalyst(cat)
    log.info(f"New catalyst {cat_id} | source={signal.source} dir={signal.direction}")
    return {"action": "created", "catalyst_id": cat_id, "wave": "spark", "confidence": signal.strength}


@app.get("/api/cos/catalysts", tags=["ChiefOfStaff"])
async def get_catalysts(
    status: str = "active",
    current_user: dict = Depends(get_current_user),
):
    cats = await all_catalysts(status)
    return {"count": len(cats), "catalysts": cats, "timestamp": int(time.time())}


@app.get("/api/cos/catalyst/{catalyst_id}", tags=["ChiefOfStaff"])
async def get_catalyst(catalyst_id: str, current_user: dict = Depends(get_current_user)):
    cat = await load_catalyst(catalyst_id)
    if not cat: raise HTTPException(404, "Catalyst not found")
    return cat


@app.post("/api/cos/attention", tags=["ChiefOfStaff"])
async def set_attention(update: AttentionUpdate, current_user: dict = Depends(get_current_user)):
    cat = await load_catalyst(update.catalyst_id)
    if not cat: raise HTTPException(404, "Catalyst not found")
    cat["attention"]  = update.state
    cat["updated_at"] = time.time()
    if update.state == "actioned" and update.asset:
        cat["position"] = {
            "id":            str(uuid.uuid4())[:8],
            "asset":         update.asset,
            "direction":     update.direction or "long",
            "entry_price":   update.entry_price,
            "entry_time":    time.time(),
            "goal_style":    update.goal_style or "swing",
            "goal_timeline": update.goal_timeline or "",
            "notes":         update.notes or "",
            "status":        "open",
        }
    await save_catalyst(cat)
    return {"ok": True, "state": update.state}


@app.delete("/api/cos/catalyst/{catalyst_id}", tags=["ChiefOfStaff"])
async def dismiss_catalyst(catalyst_id: str, current_user: dict = Depends(get_current_user)):
    cat = await load_catalyst(catalyst_id)
    if not cat: raise HTTPException(404, "Catalyst not found")
    cat["status"]     = "dismissed"
    cat["updated_at"] = time.time()
    await save_catalyst(cat)
    return {"ok": True}


@app.get("/api/cos/brief/{asset}", tags=["ChiefOfStaff"])
async def get_brief(asset: str, current_user: dict = Depends(get_current_user)):
    """All active catalysts relevant to an asset — feeds Hyper Focus daily brief."""
    cats = await all_catalysts("active")
    relevant = [
        c for c in cats
        if asset.upper() in [a.upper() for a in c.get("assets", [])]
        or asset.upper() in [s.upper() for s in c.get("sectors", [])]
    ]
    relevant.sort(key=lambda c: c.get("confidence", 0), reverse=True)
    return {
        "asset":          asset.upper(),
        "catalyst_count": len(relevant),
        "top_catalyst":   relevant[0] if relevant else None,
        "all_catalysts":  relevant,
        "timestamp":      int(time.time()),
    }


@app.post("/api/cos/decay", tags=["ChiefOfStaff"])
async def trigger_decay(current_user: dict = Depends(get_current_user)):
    """Manually trigger confidence decay cycle."""
    await apply_decay()
    return {"ok": True, "triggered_at": int(time.time())}


# ═══════════════════════════════════════════════════════════════
# USER PROFILES — watchlist, focus, actioned, preferences
# ═══════════════════════════════════════════════════════════════

class ProfileUpdate(BaseModel):
    watchlist:    Optional[List[str]] = None
    focus_list:   Optional[List[str]] = None
    preferences:  Optional[dict] = None   # {"horizon": "short"|"long", "risk": "low"|"med"|"high"}

async def load_profile(email: str) -> dict:
    val = await rget(f"profile:{email}")
    if val: return json.loads(val)
    return {"watchlist": [], "focus_list": [], "actioned_assets": [], "preferences": {"horizon": "swing", "risk": "medium"}, "missions": []}

async def save_profile(email: str, profile: dict):
    await rset(f"profile:{email}", json.dumps(profile))

@app.get("/api/profile", tags=["Profile"])
async def get_profile(current_user: dict = Depends(get_current_user)):
    return await load_profile(current_user["email"])

@app.patch("/api/profile", tags=["Profile"])
async def update_profile(update: ProfileUpdate, current_user: dict = Depends(get_current_user)):
    profile = await load_profile(current_user["email"])
    if update.watchlist   is not None: profile["watchlist"]   = update.watchlist
    if update.focus_list  is not None: profile["focus_list"]  = update.focus_list
    if update.preferences is not None: profile["preferences"] = {**profile.get("preferences", {}), **update.preferences}
    await save_profile(current_user["email"], profile)
    return {"ok": True, "profile": profile}

@app.post("/api/profile/watchlist/{ticker}", tags=["Profile"])
async def add_to_watchlist(ticker: str, current_user: dict = Depends(get_current_user)):
    profile = await load_profile(current_user["email"])
    t = ticker.upper()
    if t not in profile["watchlist"]: profile["watchlist"].append(t)
    await save_profile(current_user["email"], profile)
    return {"ok": True, "watchlist": profile["watchlist"]}

@app.delete("/api/profile/watchlist/{ticker}", tags=["Profile"])
async def remove_from_watchlist(ticker: str, current_user: dict = Depends(get_current_user)):
    profile = await load_profile(current_user["email"])
    profile["watchlist"] = [t for t in profile["watchlist"] if t.upper() != ticker.upper()]
    await save_profile(current_user["email"], profile)
    return {"ok": True, "watchlist": profile["watchlist"]}


# ═══════════════════════════════════════════════════════════════
# MISSIONS — user-defined intelligence goals
# ═══════════════════════════════════════════════════════════════

class MissionCreate(BaseModel):
    mission_type: str          # asset / sector / theme / region / macro / industry
    target:       str          # "NVDA" / "Technology" / "AI" / "Middle East" / "Fed rates"
    description:  Optional[str] = ""
    priority:     str = "normal"   # normal / high / critical

@app.post("/api/missions", tags=["Missions"])
async def create_mission(req: MissionCreate, current_user: dict = Depends(get_current_user)):
    """User sends Sebastian on a mission — creates a tracked catalyst target."""
    mission_id = str(uuid.uuid4())[:8]
    now = time.time()
    mission = {
        "id":           mission_id,
        "type":         req.mission_type,
        "target":       req.target,
        "description":  req.description or f"Track {req.mission_type}: {req.target}",
        "priority":     req.priority,
        "status":       "active",
        "created_by":   current_user["email"],
        "created_at":   now,
        "updated_at":   now,
        "catalyst_ids": [],
    }

    # Store mission
    await rset(f"mission:{mission_id}", json.dumps(mission))

    # Create a seed catalyst for the mission so CoS tracks it immediately
    seed_asset  = req.target if req.mission_type == "asset"  else None
    seed_sector = req.target if req.mission_type == "sector" else None
    cat_id = str(uuid.uuid4())[:8]
    cat = {
        "id":            cat_id,
        "title":         f"Mission: Track {req.mission_type} — {req.target}",
        "type":          "asset" if req.mission_type == "asset" else "macro",
        "direction":     "neutral",
        "wave":          "spark",
        "confidence":    35.0,
        "renewal_count": 0,
        "detected_at":   now,
        "updated_at":    now,
        "expires_at":    now + 72 * 3600,   # missions live 72h by default
        "assets":        [seed_asset]  if seed_asset  else [],
        "sectors":       [seed_sector] if seed_sector else [],
        "signals":       [{"source": "mission", "strength": 35, "summary": mission["description"], "tags": [req.mission_type], "ts": now}],
        "summary":       mission["description"],
        "verified":      False,
        "attention":     "focus",    # missions start at Focus by default
        "status":        "active",
        "tags":          [req.mission_type, req.target.lower()],
        "position":      None,
        "created_by":    current_user["email"],
        "mission_id":    mission_id,
    }
    await save_catalyst(cat)
    mission["catalyst_ids"].append(cat_id)
    await rset(f"mission:{mission_id}", json.dumps(mission))

    # Add to user profile
    profile = await load_profile(current_user["email"])
    profile.setdefault("missions", []).append({"id": mission_id, "target": req.target, "type": req.mission_type})
    await save_profile(current_user["email"], profile)

    log.info(f"Mission {mission_id} created: {req.mission_type} → {req.target} by {current_user['email']}")
    return {"ok": True, "mission_id": mission_id, "catalyst_id": cat_id, "mission": mission}

@app.get("/api/missions", tags=["Missions"])
async def get_missions(current_user: dict = Depends(get_current_user)):
    keys = await rkeys("mission:*")
    missions = []
    for key in keys:
        val = await rget(key)
        if val:
            m = json.loads(val)
            if m.get("created_by") == current_user["email"] and m.get("status") == "active":
                missions.append(m)
    missions.sort(key=lambda m: m.get("created_at", 0), reverse=True)
    return {"missions": missions, "count": len(missions)}

@app.delete("/api/missions/{mission_id}", tags=["Missions"])
async def cancel_mission(mission_id: str, current_user: dict = Depends(get_current_user)):
    val = await rget(f"mission:{mission_id}")
    if not val: raise HTTPException(404, "Mission not found")
    m = json.loads(val)
    if m.get("created_by") != current_user["email"]: raise HTTPException(403, "Not your mission")
    m["status"] = "cancelled"
    await rset(f"mission:{mission_id}", json.dumps(m))
    return {"ok": True}


# ═══════════════════════════════════════════════════════════════
# PERPLEXITY — VERIFICATION ENGINE
# ═══════════════════════════════════════════════════════════════

PERPLEXITY_KEY = os.environ.get("PERPLEXITY_API_KEY", "")
PERPLEXITY_URL = "https://api.perplexity.ai/chat/completions"
_perplexity_last_call = 0.0
PERPLEXITY_RATE_LIMIT = 2.0   # minimum seconds between calls

async def perplexity_verify(query: str, context: str = "") -> dict:
    """Call Perplexity to verify a signal. Returns verification_score + reasoning."""
    global _perplexity_last_call
    if not PERPLEXITY_KEY:
        return {"verified": False, "score": 0.0, "reasoning": "Perplexity not configured", "sources": []}

    # Rate limiting
    elapsed = time.time() - _perplexity_last_call
    if elapsed < PERPLEXITY_RATE_LIMIT:
        await asyncio.sleep(PERPLEXITY_RATE_LIMIT - elapsed)

    prompt = (
        f"Verify this market signal and assess its credibility:\n\n"
        f"Signal: {query}\n"
        f"Context: {context}\n\n"
        f"Respond in JSON only:\n"
        f'{{"verified": true/false, "confidence_score": 0-100, '
        f'"sentiment": "bullish"|"bearish"|"neutral", '
        f'"geopolitical_context": "brief if relevant", '
        f'"reliability": "high"|"medium"|"low", '
        f'"reasoning": "2-3 sentences", '
        f'"sources_found": ["source1", "source2"]}}'
    )

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(
                PERPLEXITY_URL,
                headers={"Authorization": f"Bearer {PERPLEXITY_KEY}", "Content-Type": "application/json"},
                json={
                    "model": "llama-3.1-sonar-large-128k-online",
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 400,
                    "temperature": 0.1,
                },
            )
            _perplexity_last_call = time.time()
            if r.status_code != 200:
                return {"verified": False, "score": 0.0, "reasoning": f"Perplexity error {r.status_code}", "sources": []}

            text = r.json().get("choices", [{}])[0].get("message", {}).get("content", "{}")
            # Strip markdown fences if present
            text = text.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
            result = json.loads(text)
            score  = float(result.get("confidence_score", 50))
            return {
                "verified":            result.get("verified", False),
                "score":               score,
                "sentiment":           result.get("sentiment", "neutral"),
                "geopolitical_context":result.get("geopolitical_context", ""),
                "reliability":         result.get("reliability", "medium"),
                "reasoning":           result.get("reasoning", ""),
                "sources":             result.get("sources_found", []),
            }
    except Exception as e:
        log.warning(f"Perplexity error: {e}")
        return {"verified": False, "score": 0.0, "reasoning": str(e), "sources": []}


@app.post("/api/perplexity/verify", tags=["Verification"])
async def verify_signal(
    catalyst_id: str = Query(...),
    current_user: dict = Depends(get_current_user),
):
    """Verify a catalyst against Perplexity web search. Updates catalyst confidence."""
    cat = await load_catalyst(catalyst_id)
    if not cat: raise HTTPException(404, "Catalyst not found")

    query   = cat.get("title", cat.get("summary", "market signal"))
    context = f"Assets: {', '.join(cat.get('assets', []))}. Wave: {cat['wave']}. Direction: {cat['direction']}."
    result  = await perplexity_verify(query, context)

    # Apply verification to catalyst confidence
    if result.get("verified"):
        boost = min(15.0, result["score"] * 0.15)
        cat["confidence"] = min(100.0, cat["confidence"] + boost)
        cat["verified"]   = True
        cat["signals"].append({
            "source": "perplexity", "strength": result["score"],
            "summary": result["reasoning"][:120], "tags": ["verified"], "ts": time.time(),
        })
        cat["wave"] = determine_wave(cat["renewal_count"], cat["confidence"])
        await save_catalyst(cat)

    return {
        "catalyst_id": catalyst_id,
        "verification": result,
        "confidence_after": cat["confidence"],
        "wave_after": cat["wave"],
    }


# ═══════════════════════════════════════════════════════════════
# SIGNAL SPOTTER — anomaly + alignment detection
# ═══════════════════════════════════════════════════════════════

_spotter_last_run = 0.0

async def run_signal_spotter() -> dict:
    """
    Scans all active catalysts for:
    - multi-source alignment (3+ sources → boost confidence)
    - cross-asset alignment (same sector/direction)
    - stale signals that haven't renewed (flag for decay)
    - escalation candidates (renewal_count threshold crossing)
    Returns list of detected anomalies / candidates.
    """
    global _spotter_last_run
    now = time.time()
    cats = await all_catalysts("active")
    findings = []

    # Group by direction + sector
    sector_groups: Dict[str, list] = {}
    for cat in cats:
        for sector in cat.get("sectors", []):
            key = f"{sector}:{cat['direction']}"
            sector_groups.setdefault(key, []).append(cat)

    # Cross-asset alignment: 3+ catalysts in same sector+direction
    for key, group in sector_groups.items():
        if len(group) >= 3:
            sector, direction = key.split(":", 1)
            findings.append({
                "type":      "cross_asset_alignment",
                "sector":    sector,
                "direction": direction,
                "count":     len(group),
                "catalyst_ids": [c["id"] for c in group],
                "message":   f"{len(group)} catalysts aligned {direction} in {sector} — sector rotation signal",
            })
            # Boost all catalysts in this group
            for cat in group:
                cat["confidence"] = min(100, cat["confidence"] + 5)
                await save_catalyst(cat)

    # Multi-source alignment within single catalyst
    for cat in cats:
        sources = set(s.get("source") for s in cat.get("signals", []))
        if len(sources) >= 4 and cat["wave"] not in ("structural", "regime"):
            findings.append({
                "type":        "multi_source_alignment",
                "catalyst_id": cat["id"],
                "sources":     list(sources),
                "message":     f"{cat['title'][:60]} — {len(sources)} sources aligned. Escalation candidate.",
            })

    # Escalation candidates
    for cat in cats:
        if cat["renewal_count"] >= 2 and cat["confidence"] > 58 and cat["wave"] == "confirmed":
            cat["wave"] = determine_wave(cat["renewal_count"], cat["confidence"])
            await save_catalyst(cat)
            findings.append({
                "type":        "wave_transition",
                "catalyst_id": cat["id"],
                "new_wave":    cat["wave"],
                "message":     f"{cat['title'][:60]} → wave transition to {cat['wave']}",
            })

    _spotter_last_run = now
    log.info(f"Signal Spotter: {len(findings)} findings from {len(cats)} catalysts")
    return {"findings": findings, "catalysts_scanned": len(cats), "timestamp": int(now)}


@app.post("/api/spotter/run", tags=["SignalSpotter"])
async def trigger_spotter(current_user: dict = Depends(get_current_user)):
    result = await run_signal_spotter()
    return result

@app.get("/api/spotter/status", tags=["SignalSpotter"])
async def spotter_status(current_user: dict = Depends(get_current_user)):
    return {"last_run": int(_spotter_last_run), "status": "active"}


# ═══════════════════════════════════════════════════════════════
# PERSONA — summary generation
# ═══════════════════════════════════════════════════════════════

async def generate_catalyst_summary(cat: dict) -> str:
    """Generate a 2-sentence Persona summary for a catalyst."""
    if not ANTHROPIC_KEY:
        return f"{cat['wave'].capitalize()} signal: {cat['title']}. Confidence {cat['confidence']}."
    try:
        signals_text = ". ".join(s.get("summary", "") for s in cat.get("signals", [])[-3:] if s.get("summary"))
        prompt = (
            f"Write a 2-sentence intelligence summary for this market catalyst.\n"
            f"Title: {cat['title']}\n"
            f"Wave: {cat['wave']} | Confidence: {cat['confidence']} | Direction: {cat['direction']}\n"
            f"Signal sources: {signals_text}\n"
            f"Assets: {', '.join(cat.get('assets', []))}\n"
            f"Be specific and direct. No disclaimers."
        )
        async with httpx.AsyncClient() as client:
            r = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                json={
                    "model": "claude-sonnet-4-20250514",
                    "max_tokens": 150,
                    "system": "You are Persona, Market Brain's summary generation engine. Write precise 2-sentence catalyst summaries.",
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=15,
            )
            return r.json().get("content", [{}])[0].get("text", cat["title"])
    except Exception as e:
        return f"{cat['wave'].capitalize()}: {cat['title']} — conf {cat['confidence']}"

@app.post("/api/persona/summarise/{catalyst_id}", tags=["Persona"])
async def persona_summarise(catalyst_id: str, current_user: dict = Depends(get_current_user)):
    cat = await load_catalyst(catalyst_id)
    if not cat: raise HTTPException(404, "Catalyst not found")
    summary = await generate_catalyst_summary(cat)
    cat["persona_summary"] = summary
    cat["updated_at"] = time.time()
    await save_catalyst(cat)
    return {"catalyst_id": catalyst_id, "summary": summary}

@app.post("/api/persona/brief", tags=["Persona"])
async def persona_brief(current_user: dict = Depends(get_current_user)):
    """Generate the full daily intelligence brief."""
    return await sebastian_summarise(current_user)


# ═══════════════════════════════════════════════════════════════
# HYPER-FOCUS — attention management + daily briefs
# ═══════════════════════════════════════════════════════════════

@app.get("/api/hyperfocus/brief", tags=["HyperFocus"])
async def hyperfocus_brief(current_user: dict = Depends(get_current_user)):
    """
    Hyper-Focus daily brief: catalysts relevant to user's watchlist + focus list.
    Sorted by confidence descending. Includes attention state.
    """
    profile = await load_profile(current_user["email"])
    watchlist  = [t.upper() for t in profile.get("watchlist", [])]
    focus_list = [t.upper() for t in profile.get("focus_list", [])]
    tracked    = set(watchlist + focus_list)

    all_cats = await all_catalysts("active")

    # Personal catalysts — on user's watchlist/focus
    personal = []
    general  = []
    for cat in all_cats:
        cat_assets  = [a.upper() for a in cat.get("assets",  [])]
        cat_sectors = [s.upper() for s in cat.get("sectors", [])]
        is_personal = any(t in tracked for t in cat_assets + cat_sectors)
        if is_personal:
            personal.append(cat)
        else:
            general.append(cat)

    personal.sort(key=lambda c: c.get("confidence", 0), reverse=True)
    general.sort( key=lambda c: c.get("confidence", 0), reverse=True)

    # Promote focus-list catalysts
    focus_cats = [c for c in personal if any(a.upper() in focus_list for a in c.get("assets", []))]

    return {
        "user":         current_user["email"],
        "watchlist":    watchlist,
        "focus_list":   focus_list,
        "focus_catalysts":    focus_cats[:5],
        "personal_catalysts": personal[:10],
        "general_catalysts":  general[:5],
        "total_active":       len(all_cats),
        "timestamp":          int(time.time()),
    }

@app.get("/api/hyperfocus/attention", tags=["HyperFocus"])
async def attention_summary(current_user: dict = Depends(get_current_user)):
    """All catalysts grouped by attention state."""
    cats = await all_catalysts("active")
    by_state: Dict[str, list] = {"watch": [], "focus": [], "actioned": []}
    for cat in cats:
        state = cat.get("attention", "watch")
        if state in by_state:
            by_state[state].append(cat)
    return {"attention": by_state, "timestamp": int(time.time())}


# ═══════════════════════════════════════════════════════════════
# ASSETS PAGE — intelligence index
# ═══════════════════════════════════════════════════════════════

@app.get("/api/assets", tags=["Assets"])
async def get_assets_intelligence(current_user: dict = Depends(get_current_user)):
    """
    Intelligence index for the Assets page.
    Shows all tracked assets with linked catalyst state.
    Not a price grid — an intelligence index.
    """
    profile  = await load_profile(current_user["email"])
    watchlist  = [t.upper() for t in profile.get("watchlist", [])]
    focus_list = [t.upper() for t in profile.get("focus_list", [])]

    # Build asset index from active catalysts
    all_cats   = await all_catalysts("active")
    asset_index: Dict[str, dict] = {}

    for cat in all_cats:
        for asset in cat.get("assets", []):
            t = asset.upper()
            if t not in asset_index:
                asset_index[t] = {
                    "ticker":     t,
                    "catalysts":  [],
                    "top_wave":   "spark",
                    "top_conf":   0.0,
                    "direction":  "neutral",
                    "attention":  "none",
                    "on_watchlist": t in watchlist,
                    "on_focus":    t in focus_list,
                    "sector":     (cat.get("sectors") or [""])[0],
                }
            asset_index[t]["catalysts"].append({
                "id":         cat["id"],
                "wave":       cat["wave"],
                "confidence": cat["confidence"],
                "direction":  cat["direction"],
                "attention":  cat.get("attention", "watch"),
                "title":      cat["title"][:80],
            })
            if cat["confidence"] > asset_index[t]["top_conf"]:
                asset_index[t]["top_conf"]  = cat["confidence"]
                asset_index[t]["top_wave"]  = cat["wave"]
                asset_index[t]["direction"] = cat["direction"]
                asset_index[t]["attention"] = cat.get("attention", "watch")

    # Add watchlist assets not yet in any catalyst
    for ticker in watchlist:
        if ticker not in asset_index:
            asset_index[ticker] = {
                "ticker": ticker, "catalysts": [], "top_wave": None,
                "top_conf": 0.0, "direction": "neutral", "attention": "watch",
                "on_watchlist": True, "on_focus": ticker in focus_list, "sector": "",
            }

    assets = sorted(asset_index.values(), key=lambda a: a["top_conf"], reverse=True)
    return {"assets": assets, "count": len(assets), "timestamp": int(time.time())}


# ═══════════════════════════════════════════════════════════════
# RELAY BOT — signal clustering endpoint
# ═══════════════════════════════════════════════════════════════

class RelaySignalBatch(BaseModel):
    signals: List[CatalystSignal]

@app.post("/api/relay/ingest", tags=["Relay"])
async def relay_ingest(batch: RelaySignalBatch, current_user: dict = Depends(get_current_user)):
    """
    Relay Bot endpoint. Accepts a batch of raw signals, clusters them, then
    forwards distinct clusters to the CoS. Prevents duplicate signals flooding.
    """
    now = time.time()
    results = []

    # Group by (asset|sector, direction) — only forward one per group per 15 minutes
    clusters: Dict[str, CatalystSignal] = {}
    for sig in batch.signals:
        cluster_key = f"{(sig.asset or sig.sector or 'global').upper()}:{sig.direction}"
        existing = clusters.get(cluster_key)
        if not existing or sig.strength > existing.strength:
            clusters[cluster_key] = sig

    for key, sig in clusters.items():
        # Use ingest_signal logic — reuse the endpoint logic directly
        class _FakeUser: email = current_user["email"]
        result = await ingest_signal(sig, current_user)
        results.append({"key": key, "result": result})

    return {"clustered": len(clusters), "original": len(batch.signals), "results": results}


# ═══════════════════════════════════════════════════════════════
# CHAIN STATUS — Phase 1 verification
# ═══════════════════════════════════════════════════════════════

@app.get("/api/chain/status", tags=["Chain"])
async def chain_status(current_user: dict = Depends(get_current_user)):
    """
    Phase 1 verification endpoint. Reports status of every link in the signal chain.
    """
    r = await get_redis()
    active_cats = await all_catalysts("active")
    missions_keys = await rkeys("mission:*")

    return {
        "chain": {
            "redis":          "connected" if r else "memory_fallback",
            "research_bots":  BOTS_AVAILABLE,
            "sweep_engine":   "external_service",  # deployed as separate Railway service
            "relay_bot":      "integrated",
            "signal_spotter": "integrated",
            "sebastian":      bool(ANTHROPIC_KEY),
            "perplexity":     bool(PERPLEXITY_KEY),
            "chief_of_staff": True,
            "persona":        bool(ANTHROPIC_KEY),
            "hyper_focus":    True,
            "dashboard":      True,
        },
        "counts": {
            "active_catalysts": len(active_cats),
            "missions":         len(missions_keys),
        },
        "warnings": [
            *(["PERPLEXITY_API_KEY not set — verification disabled"] if not PERPLEXITY_KEY else []),
            *(["ANTHROPIC_API_KEY not set — Sebastian + Persona disabled"] if not ANTHROPIC_KEY else []),
            *(["Redis not connected — using memory fallback"] if not r else []),
        ],
        "timestamp": int(time.time()),
    }



static_dir = Path("static")
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/{full_path:path}", include_in_schema=False)
async def serve_frontend(full_path: str):
    index = Path("static/index.html")
    if index.exists(): return FileResponse(index)
    return {"status": "ok", "version": "3.0.0", "docs": "/docs"}


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=False, log_level="info")
