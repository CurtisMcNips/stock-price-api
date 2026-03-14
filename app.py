"""
Market Brain — app.py
FastAPI backend with JWT auth, price API, portfolios, and Sebastian AI analyst.
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
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Query, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, EmailStr

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# Persona Bot — user-facing trading-style interpreter
# persona_bot.py lives in research_bots/ — add to path before importing
try:
    import sys as _sys
    _bots_dir = os.path.join(os.path.dirname(__file__), "research_bots")
    if _bots_dir not in _sys.path:
        _sys.path.insert(0, _bots_dir)
    from persona_bot import get_persona_chat_bot, PersonaChatBot
    _PERSONA_AVAILABLE = True
    log.info("PersonaChatBot loaded from research_bots/persona_bot.py")
except ImportError as _e:
    _PERSONA_AVAILABLE = False
    log.warning(f"persona_bot import failed — /api/persona unavailable: {_e}")

# ── Config ────────────────────────────────────────────────────
REDIS_URL       = os.environ.get("REDIS_URL", "redis://localhost:6379")
SECRET_KEY      = os.environ.get("SECRET_KEY", "change-me-in-production-railway-env")
CACHE_TTL       = 5
TOKEN_TTL       = 60 * 60 * 24 * 30   # 30 days
REQUEST_TIMEOUT = 8

# Anthropic / Sebastian
ANTHROPIC_KEY   = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_URL   = "https://api.anthropic.com/v1/messages"
ANTHROPIC_MODEL = "claude-sonnet-4-5"

YAHOO_URL          = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
YAHOO_FALLBACK_URL = "https://query2.finance.yahoo.com/v8/finance/chart/{symbol}"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
}

# Core tickers Sebastian always fetches prices for
SEBASTIAN_CORE_TICKERS = [
    "GC=F", "CL=F", "BZ=F", "NG=F", "HG=F",          # Commodities / futures
    "LMT", "RTX", "NOC", "BA", "GD",                   # Defence
    "ZIM", "FRO", "STNG", "DAC", "SBLK",               # Shipping
    "XOM", "CVX", "BP", "SHEL", "COP",                 # Energy
    "NVDA", "AMD", "MSFT", "GOOGL", "META",             # Tech
    "GLD", "SLV", "FCX", "NEM",                        # Metals
    "BTC-USD", "ETH-USD", "COIN",                      # Crypto
    "EURUSD=X", "GBPUSD=X", "USDJPY=X",               # Forex
    "SPY", "QQQ", "^VIX",                              # Macro
    "JPM", "GS", "BAC",                                # Finance
    "UPS", "FDX",                                      # Logistics
]

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
app = FastAPI(title="Market Brain API", version="2.1.0", lifespan=lifespan)
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

class SebastianRequest(BaseModel):
    question: str
    history:  list = []
    mode:     str  = "auto"    # auto | beginner | expert
    context:  dict = {}        # optional extra context from frontend (asset_context, tickers, etc.)


class PersonaRequest(BaseModel):
    question: str
    history:  list = []
    mode:     str  = "auto"    # auto | beginner | expert
    context:  dict = {}        # optional extra context (focused catalyst, asset focus, etc.)


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
    for prefix, suffix in [("LON:", ".L"), ("EPA:", ".PA"), ("ETR:", ".DE"), ("AMS:", ".AS"), ("TSX:", ".TO"), ("ASX:", ".AX")]:
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


# ── Catalyst endpoints ────────────────────────────────────────

@app.get("/api/catalysts", tags=["Catalysts"])
async def get_catalysts(
    wave:      Optional[str] = Query(None),
    direction: Optional[str] = Query(None),
    limit:     int           = Query(100),
    current_user: dict       = Depends(get_current_user),
):
    """
    Return all active catalysts from Redis.
    Tries all key patterns the CoS and sweep engine may use.
    """
    try:
        r = await get_redis()
        if not r:
            return {"catalysts": [], "total": 0, "source": "redis_unavailable"}

        # Try all key patterns in priority order
        keys = []
        for pattern in ("catalyst:*", "cos:catalyst:*", "mb:catalyst:*", "mb:cos:catalyst:*"):
            found = await r.keys(pattern)
            if found:
                keys.extend(found)

        # Deduplicate
        keys = list(dict.fromkeys(keys))

        if not keys:
            return {"catalysts": [], "total": 0, "source": "no_keys_found"}

        catalysts = []
        for key in keys:
            try:
                val = await r.get(key)
                if not val:
                    continue
                cat = json.loads(val)
                # Skip dismissed/expired
                if cat.get("status") in ("dismissed", "expired"):
                    continue
                # Apply filters
                if wave and cat.get("wave") != wave:
                    continue
                if direction and cat.get("direction") != direction:
                    continue
                catalysts.append(cat)
            except Exception:
                continue

        # Sort: wave priority → confidence → recency
        wave_order = {"regime": 5, "structural": 4, "escalation": 3, "confirmed": 2, "spark": 1}
        catalysts.sort(
            key=lambda c: (
                wave_order.get(c.get("wave", "spark"), 0),
                c.get("confidence", 0),
                c.get("updated_at", c.get("created_at", 0)),
            ),
            reverse=True,
        )

        return {
            "catalysts": catalysts[:limit],
            "total":     len(catalysts),
            "source":    "redis",
            "ts":        int(time.time()),
        }

    except Exception as e:
        log.error(f"Catalyst fetch error: {e}")
        raise HTTPException(500, f"Catalyst fetch failed: {e}")


@app.post("/api/catalysts/{catalyst_id}/dismiss", tags=["Catalysts"])
async def dismiss_catalyst(
    catalyst_id: str,
    current_user: dict = Depends(get_current_user),
):
    """Mark a catalyst as dismissed so it's filtered from the feed."""
    r = await get_redis()
    if not r:
        raise HTTPException(503, "Redis unavailable")

    # Try all key patterns
    for pattern_prefix in ("catalyst:", "cos:catalyst:", "mb:catalyst:", "mb:cos:catalyst:"):
        key = f"{pattern_prefix}{catalyst_id}"
        val = await r.get(key)
        if val:
            try:
                cat = json.loads(val)
                cat["status"] = "dismissed"
                cat["dismissed_at"] = int(time.time())
                await r.set(key, json.dumps(cat))
                return {"ok": True, "catalyst_id": catalyst_id}
            except Exception as e:
                raise HTTPException(500, f"Failed to dismiss: {e}")

    raise HTTPException(404, f"Catalyst {catalyst_id} not found")


# ── Sebastian helpers ─────────────────────────────────────────

async def _fetch_catalysts_for_sebastian() -> str:
    """
    Pull active catalysts from Redis and format them for Sebastian's context.
    Tries all known key patterns used by the CoS / sweep engine.
    """
    try:
        r = await get_redis()
        if not r:
            return "No catalyst data available (Redis offline)."

        # Try all key patterns the CoS might use
        keys = []
        for pattern in ("catalyst:*", "cos:catalyst:*", "mb:catalyst:*"):
            found = await r.keys(pattern)
            if found:
                keys.extend(found)
                break  # use first pattern that has results

        if not keys:
            return "No active catalysts in the intelligence chain yet."

        catalysts = []
        for key in keys[:30]:
            try:
                val = await r.get(key)
                if val:
                    cat = json.loads(val)
                    if cat.get("status") not in ("dismissed", "expired"):
                        catalysts.append(cat)
            except Exception:
                continue

        if not catalysts:
            return "No active catalysts found."

        # Sort: wave priority then confidence
        wave_order = {"regime": 5, "structural": 4, "escalation": 3, "confirmed": 2, "spark": 1}
        catalysts.sort(
            key=lambda c: (wave_order.get(c.get("wave", "spark"), 0), c.get("confidence", 0)),
            reverse=True,
        )

        lines = []
        for cat in catalysts[:8]:
            wave        = cat.get("wave", "spark").upper()
            direction   = cat.get("direction", "neutral")
            confidence  = cat.get("confidence", 0)
            title       = (cat.get("title") or cat.get("headline", ""))[:80]
            assets      = ", ".join((cat.get("assets") or [])[:4])
            sectors     = ", ".join((cat.get("sectors") or [])[:2])
            verified    = " ✓verified" if cat.get("verified") else ""
            renewals    = cat.get("renewals", 0)
            persona     = cat.get("persona_summary") or {}
            summary     = (persona.get("summary") or cat.get("summary", ""))[:120]

            # Include Perplexity verdict if available
            verif = cat.get("verification") or {}
            verif_str = ""
            if verif.get("verdict"):
                verif_str = f" | Perplexity: {verif['verdict']}"

            lines.append(
                f"[{wave} | {direction} | {confidence:.0f}%{verified}{verif_str} | renewals:{renewals}]\n"
                f"  {title}\n"
                f"  Assets: {assets or 'N/A'} | Sectors: {sectors or 'N/A'}\n"
                f"  {summary}"
            )

        return "\n\n".join(lines)

    except Exception as e:
        log.warning(f"Sebastian catalyst fetch error: {e}")
        return "Catalyst data temporarily unavailable."


def _build_sebastian_system(
    mode:          str,
    live_prices:   str,
    catalysts:     str,
    extra_context: str = "",
) -> str:
    """
    Build Sebastian's system prompt with live context injected server-side.
    Sebastian only — Persona Bot has its own prompt in persona_bot.py.
    The ANTHROPIC_API_KEY never leaves the server.
    """
    mode_instruction = {
        "beginner": (
            "The user is new to markets. Lead with a plain-English one-sentence summary "
            "before any technical detail. Define jargon the first time you use it. Be warm and clear."
        ),
        "expert": (
            "The user is experienced. Skip basics. Lead with technical depth — wave mechanics, "
            "confidence thresholds, bot alignment, signal strength, verification scores."
        ),
        "auto": (
            "Lead with a plain one-sentence summary anyone can understand, "
            "then provide full technical depth. Serves both beginners and experienced traders."
        ),
    }.get(mode, "")

    extra_block = f"\nADDITIONAL CONTEXT:\n{extra_context}\n" if extra_context else ""

    return f"""You are Sebastian — a senior hedge-fund market analyst with 30 years of experience across macro, equities, commodities, and geopolitical risk. You have worked on multi-strategy desks, advised portfolio managers, and built intelligence frameworks for institutional trading teams.

{mode_instruction}

YOUR ROLE:
You are the primary data and analysis layer. You surface what is actually moving, what the intelligence chain is detecting, what Perplexity is verifying, and what the risk picture looks like. The Persona Bot reads what you produce and interprets it through trading styles — so your job is precision and completeness of information, not style.

You never give financial advice. You never tell the user what to buy or sell. You never predict markets.

OPENING BRIEF — when first contacted, always lead with:
1. MOVERS: Flag any ticker showing >5% daily move. Group by cap size (small / mid / large). Note direction (up/down) and the magnitude. This is the most time-sensitive information.
2. TOP CATALYST: The highest-wave active catalyst — wave state, confidence, which bots are aligned, Perplexity verdict.
3. SECTOR HEAT: Which sectors have the most active signals right now.
4. INTELLIGENCE CHAIN STATUS: Overall market bias from the chain (bullish / bearish / mixed / neutral). Any major Perplexity contradictions that elevate uncertainty.
5. ONE THING TO WATCH: The single most important signal developing right now.

ANALYSIS FORMAT — for all follow-up questions:
1. Catalyst Summary: wave state, confidence %, intensity, domains, tags
2. Bot Intelligence Breakdown: GEO, FUNDAMENTALS, NEWS, TECHNICAL, HIRING, INSIDER, ANALYST, EARNINGS, MACRO — what each is saying
3. Per-Bot Verification: verified / mixed / contradicted / insufficient data — always shown
4. Verification Interpretation: agreements, disputes, contradictions — explain the risk implications
5. Cross-Asset Context: what the price action in the live prices above says relative to the signal
6. Final Summary: non-advisory, uncertainty acknowledged, one thing to watch next

CATALYST LIFECYCLE:
  SPARK:       Single early signal, unconfirmed. Note it but caveat heavily.
  CONFIRMED:   2+ bots aligned, 42%+ confidence. Worth tracking — name which bots agree.
  ESCALATION:  2+ bots + verification or cross-asset, 58%+. Building — explain what's driving it.
  STRUCTURAL:  3+ bots + verified + cross-asset, 72%+, 2h+. Significant — give full breakdown.
  REGIME:      4+ bots + verified + cross-asset + geo/macro, 85%+, 6h+. Major — treat seriously.
  DECAY:       Confidence falling. Explain what reversed and whether the thesis is broken.
  EXHAUSTION:  Signal spent. Explain what happened and what it means for related assets.

PERPLEXITY VERIFICATION — always surface:
  - overall score (-1.0 to +1.0), verdict (supported / mixed / weak / contradicted)
  - which bots were verified, which were contradicted
  - if contradicted: explain possible causes, highlight that certainty is reduced
  Never express certainty when Perplexity contradicts the internal chain.

LIVE MARKET PRICES (fetched {time.strftime("%H:%M UTC")} — flag any above ±5% as movers):
{live_prices}

ACTIVE INTELLIGENCE (top catalysts by wave priority):
{catalysts}{extra_block}"""




# ── Sebastian endpoint ────────────────────────────────────────

@app.post("/api/sebastian", tags=["Sebastian"])
async def sebastian_chat(
    req: SebastianRequest,
    current_user: dict = Depends(get_current_user),
):
    """
    Sebastian AI analyst — server-side proxy to Anthropic.
    Fetches live prices and catalyst context before every call.
    The ANTHROPIC_API_KEY never leaves the server.
    Trading-style questions (Persona Bot) are handled by POST /api/persona.
    """
    if not ANTHROPIC_KEY:
        raise HTTPException(503, "Sebastian is not configured — ANTHROPIC_API_KEY missing from environment")

    if not req.question or not req.question.strip():
        raise HTTPException(400, "Question cannot be empty")

    # ── 1. Fetch live prices ───────────────────────────────────
    live_prices_str = "Prices temporarily unavailable."
    try:
        extra_tickers = req.context.get("tickers", [])
        all_tickers   = list(dict.fromkeys(SEBASTIAN_CORE_TICKERS + extra_tickers))[:40]

        client  = await get_client()
        results = await asyncio.gather(
            *[get_price(t, client) for t in all_tickers],
            return_exceptions=True,
        )
        price_lines = []
        for r_data in results:
            if isinstance(r_data, dict) and r_data.get("price"):
                chg     = r_data.get("change_pct", 0) or 0
                chg_str = f"{chg:+.2f}%"
                state   = r_data.get("market_state", "")
                state_str = f" [{state}]" if state and state != "REGULAR" else ""
                price_lines.append(
                    f"  {r_data['symbol']}: {r_data.get('currency','USD')} "
                    f"{r_data['price']}{state_str} ({chg_str})"
                )
        if price_lines:
            live_prices_str = "\n".join(price_lines)
        log.info(f"Sebastian: fetched {len(price_lines)} prices for {current_user['email']}")
    except Exception as e:
        log.warning(f"Sebastian price fetch error: {e}")

    # ── 2. Fetch active catalysts ──────────────────────────────
    catalysts_str = await _fetch_catalysts_for_sebastian()

    # ── 3. Extra context from frontend ────────────────────────
    extra_context = ""
    if req.context.get("asset_context"):
        extra_context = str(req.context["asset_context"])[:500]
    if req.context.get("catalyst"):
        cat = req.context["catalyst"]
        extra_context += (
            f"\nFocused catalyst: {cat.get('title') or cat.get('headline', '')}\n"
            f"Wave: {cat.get('wave')} | Direction: {cat.get('direction')} | "
            f"Confidence: {cat.get('confidence', 0):.0f}%\n"
            f"Assets: {', '.join((cat.get('assets') or [])[:5])}\n"
            f"Verified: {cat.get('verified', False)} | "
            f"Perplexity verdict: {(cat.get('verification') or {}).get('verdict', 'none')}"
        )

    # ── 4. Build system prompt for the active agent ────────────
    system = _build_sebastian_system(
        mode=req.mode,
        live_prices=live_prices_str,
        catalysts=catalysts_str,
        extra_context=extra_context,
    )

    # ── 5. Build message history ───────────────────────────────
    messages = []
    for msg in req.history[-12:]:
        role    = msg.get("role", "")
        content = msg.get("content", "")
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": str(content)[:2000]})
    messages.append({"role": "user", "content": req.question.strip()})

    # ── 6. Call Anthropic ──────────────────────────────────────
    try:
        async with httpx.AsyncClient(timeout=45) as http:
            r = await http.post(
                ANTHROPIC_URL,
                headers={
                    "x-api-key":         ANTHROPIC_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type":      "application/json",
                },
                json={
                    "model":      ANTHROPIC_MODEL,
                    "max_tokens": 1200,
                    "system":     system,
                    "messages":   messages,
                },
            )

            if r.status_code != 200:
                log.warning(f"Anthropic API error {r.status_code}: {r.text[:300]}")
                raise HTTPException(502, f"AI service returned {r.status_code}")

            response_text = r.json().get("content", [{}])[0].get("text", "").strip()
            if not response_text:
                raise HTTPException(502, "Empty response from AI service")

            log.info(f"Sebastian responded to {current_user['email']}: {len(response_text)} chars")

            return {
                "response":       response_text,
                "agent":          "sebastian",
                "prices_fetched": len([l for l in live_prices_str.split("\n") if l.strip().startswith("  ")]),
                "model":          ANTHROPIC_MODEL,
                "ts":             int(time.time()),
            }

    except HTTPException:
        raise
    except httpx.TimeoutException:
        log.warning("Sebastian: Anthropic request timed out")
        raise HTTPException(504, "Request timed out — try again")
    except Exception as e:
        log.error(f"Sebastian endpoint error: {e}")
        raise HTTPException(500, f"Error: {str(e)}")


# ── Persona Bot endpoint ──────────────────────────────────────

@app.post("/api/persona", tags=["Persona"])
async def persona_chat(
    req: PersonaRequest,
    current_user: dict = Depends(get_current_user),
):
    """
    Persona Bot — trading-style interpreter.
    Separate endpoint from Sebastian. Calls PersonaChatBot.chat() directly.
    All prompt logic lives in persona_bot.py — none here.
    Fetches the same live prices and catalyst context as Sebastian.
    """
    if not ANTHROPIC_KEY:
        raise HTTPException(503, "Persona Bot is not configured — ANTHROPIC_API_KEY missing")

    if not _PERSONA_AVAILABLE:
        raise HTTPException(503, "Persona Bot module not available on this deployment")

    if not req.question or not req.question.strip():
        raise HTTPException(400, "Question cannot be empty")

    # ── 1. Fetch live prices (same core tickers as Sebastian) ──
    live_prices_str = "Prices temporarily unavailable."
    try:
        extra_tickers = req.context.get("tickers", [])
        all_tickers   = list(dict.fromkeys(SEBASTIAN_CORE_TICKERS + extra_tickers))[:40]
        client        = await get_client()
        results       = await asyncio.gather(
            *[get_price(t, client) for t in all_tickers],
            return_exceptions=True,
        )
        price_lines = []
        for r_data in results:
            if isinstance(r_data, dict) and r_data.get("price"):
                chg     = r_data.get("change_pct", 0) or 0
                state   = r_data.get("market_state", "")
                state_s = f" [{state}]" if state and state != "REGULAR" else ""
                price_lines.append(
                    f"  {r_data['symbol']}: {r_data.get('currency','USD')} "
                    f"{r_data['price']}{state_s} ({chg:+.2f}%)"
                )
        if price_lines:
            live_prices_str = "\n".join(price_lines)
        log.info(f"Persona Bot: fetched {len(price_lines)} prices for {current_user['email']}")
    except Exception as e:
        log.warning(f"Persona Bot price fetch error: {e}")

    # ── 2. Fetch active catalysts ──────────────────────────────
    catalysts_str = await _fetch_catalysts_for_sebastian()

    # ── 3. Extra context and verification from frontend ────────
    extra_context = ""
    verification  = ""

    # Sebastian's latest analysis — the core of the Persona Bot feed
    if req.context.get("sebastian_analysis"):
        seb_text = str(req.context["sebastian_analysis"])[:3000]
        extra_context += (
            f"SEBASTIAN'S LATEST ANALYSIS (interpret this through the requested trading style):\n"
            f"{seb_text}\n\n"
            f"NOTE: Sebastian has already surfaced the key intelligence. "
            f"Your job is to interpret what he found through the trading style the user asks about — "
            f"do not re-analyse from scratch. Reference his specific findings.\n"
        )

    # Market snapshot from /api/sebastian/context
    if req.context.get("market_snapshot"):
        snap = req.context["market_snapshot"]
        mover_summary = snap.get("mover_summary", "")
        if mover_summary and mover_summary != "No tickers above 5% threshold at this time.":
            extra_context += f"\nACTIVE MOVERS (>5% today):\n{mover_summary}\n"
        hot = snap.get("hot_sectors", [])
        if hot:
            sectors_str = ", ".join(s.get("sector","") for s in hot[:5])
            extra_context += f"\nHOT SECTORS: {sectors_str}\n"

    # Catalyst focus from asset card or catalyst panel
    if req.context.get("asset_context"):
        extra_context += f"\nFOCUSED ASSET CONTEXT: {str(req.context['asset_context'])[:400]}\n"
    if req.context.get("catalyst"):
        cat = req.context["catalyst"]
        extra_context += (
            f"\nFOCUSED CATALYST: {cat.get('title') or cat.get('headline', '')}\n"
            f"Wave: {cat.get('wave')} | Direction: {cat.get('direction')} | "
            f"Confidence: {cat.get('confidence', 0):.0f}%\n"
            f"Assets: {', '.join((cat.get('assets') or [])[:5])}\n"
        )
        verif = cat.get("verification") or {}
        if verif:
            verification = json.dumps(verif, indent=2)

    # Top catalysts list from context snapshot
    if req.context.get("top_catalysts"):
        cats = req.context["top_catalysts"][:5]
        if cats:
            cat_lines = []
            for c in cats:
                cat_lines.append(
                    f"  [{c.get('wave','?').upper()} | {c.get('direction','?')} | "
                    f"{c.get('confidence',0):.0f}%] {c.get('title','')[:60]}"
                )
            extra_context += f"\nTOP ACTIVE CATALYSTS:\n" + "\n".join(cat_lines) + "\n"

    # ── 4. Call PersonaChatBot.chat() — all prompt logic in persona_bot.py ──
    try:
        persona = get_persona_chat_bot()
        response_text = await persona.chat(
            question=req.question.strip(),
            history=req.history,
            live_prices=live_prices_str,
            catalysts=catalysts_str,
            verification=verification,
            extra_context=extra_context,
            mode=req.mode,
        )

        if not response_text:
            raise HTTPException(502, "Empty response from Persona Bot")

        log.info(f"Persona Bot responded to {current_user['email']}: {len(response_text)} chars")

        return {
            "response":       response_text,
            "agent":          "persona",
            "prices_fetched": len(price_lines) if 'price_lines' in dir() else 0,
            "model":          ANTHROPIC_MODEL,
            "ts":             int(time.time()),
        }

    except HTTPException:
        raise
    except Exception as e:
        log.error(f"Persona Bot endpoint error: {e}")
        raise HTTPException(500, f"Persona Bot error: {str(e)}")


# ── Sebastian Context endpoint ─────────────────────────────────
# Returns Sebastian's live market snapshot as structured JSON.
# Persona Bot reads this to contextualise all its responses.

@app.get("/api/sebastian/context", tags=["Sebastian"])
async def sebastian_context(current_user: dict = Depends(get_current_user)):
    """
    Returns Sebastian's current market snapshot as structured JSON.
    Called by the frontend to feed context into Persona Bot.
    Includes: live prices with mover flags, active catalysts,
    Perplexity verdicts, sector themes.
    """
    # ── 1. Fetch all core prices ───────────────────────────────
    client  = await get_client()
    results = await asyncio.gather(
        *[get_price(t, client) for t in SEBASTIAN_CORE_TICKERS],
        return_exceptions=True,
    )

    # ── 2. Cap size classification ─────────────────────────────
    # Rough market cap buckets by ticker — approximate only
    LARGE_CAP = {
        "NVDA","MSFT","GOOGL","META","AAPL","AMZN","TSLA",
        "JPM","GS","BAC","XOM","CVX","LMT","RTX","BA","GD",
        "SPY","QQQ","GLD","SLV","BTC-USD","ETH-USD",
        "GC=F","CL=F","BZ=F","SHEL","COP",
    }
    MID_CAP = {
        "AMD","COIN","NOC","FCX","NEM","UPS","FDX",
        "ZIM","FRO","STNG","DAC","SBLK",
        "HG=F","NG=F","BP",
    }
    # Everything else = small cap

    def cap_size(symbol: str) -> str:
        s = symbol.upper()
        if s in LARGE_CAP: return "large"
        if s in MID_CAP:   return "mid"
        return "small"

    # ── 3. Build price records + flag movers ───────────────────
    all_prices   = []
    movers_5pct  = {"large": [], "mid": [], "small": []}

    for r_data in results:
        if not isinstance(r_data, dict) or not r_data.get("price"):
            continue
        symbol   = r_data["symbol"]
        price    = r_data["price"]
        chg      = r_data.get("change_pct", 0) or 0
        cap      = cap_size(symbol)
        currency = r_data.get("currency", "USD")
        state    = r_data.get("market_state", "REGULAR")
        name     = r_data.get("name", symbol)

        record = {
            "symbol":   symbol,
            "name":     name,
            "price":    price,
            "change_pct": round(chg, 2),
            "currency": currency,
            "cap_size": cap,
            "market_state": state,
        }
        all_prices.append(record)

        if abs(chg) >= 5.0:
            movers_5pct[cap].append({
                "symbol":     symbol,
                "change_pct": round(chg, 2),
                "direction":  "up" if chg > 0 else "down",
                "price":      price,
                "currency":   currency,
            })

    # Sort movers by absolute change descending
    for cap in movers_5pct:
        movers_5pct[cap].sort(key=lambda x: abs(x["change_pct"]), reverse=True)

    # ── 4. Fetch active catalysts ──────────────────────────────
    catalysts_raw = []
    try:
        r = await get_redis()
        if r:
            keys = []
            for pattern in ("catalyst:*", "cos:catalyst:*", "mb:catalyst:*"):
                found = await r.keys(pattern)
                if found:
                    keys.extend(found)
                    break
            for key in keys[:20]:
                try:
                    val = await r.get(key)
                    if val:
                        cat = json.loads(val)
                        if cat.get("status") not in ("dismissed", "expired"):
                            catalysts_raw.append({
                                "title":      (cat.get("title") or cat.get("headline", ""))[:80],
                                "wave":       cat.get("wave", "spark"),
                                "direction":  cat.get("direction", "neutral"),
                                "confidence": round(cat.get("confidence", 0), 1),
                                "assets":     (cat.get("assets") or [])[:5],
                                "sectors":    (cat.get("sectors") or [])[:3],
                                "verified":   cat.get("verified", False),
                                "perplexity_verdict": (cat.get("verification") or {}).get("verdict", ""),
                                "perplexity_score":   (cat.get("verification") or {}).get("score", None),
                                "summary":    (cat.get("summary", ""))[:200],
                                "renewals":   cat.get("renewals", 0),
                            })
                except Exception:
                    continue

        wave_order = {"regime": 5, "structural": 4, "escalation": 3, "confirmed": 2, "spark": 1}
        catalysts_raw.sort(
            key=lambda c: (wave_order.get(c.get("wave", "spark"), 0), c.get("confidence", 0)),
            reverse=True,
        )
    except Exception as e:
        log.warning(f"sebastian/context catalyst error: {e}")

    # ── 5. Sector themes from top catalysts ───────────────────
    sector_counts: dict = {}
    for cat in catalysts_raw:
        for sec in cat.get("sectors", []):
            sector_counts[sec] = sector_counts.get(sec, 0) + 1
    hot_sectors = sorted(sector_counts.items(), key=lambda x: x[1], reverse=True)[:5]

    # ── 6. Build human-readable mover summary for Sebastian ───
    mover_lines = []
    for cap in ("large", "mid", "small"):
        for m in movers_5pct[cap]:
            d = "↑" if m["direction"] == "up" else "↓"
            mover_lines.append(
                f"  {m['symbol']} {d}{abs(m['change_pct']):.1f}% ({cap}-cap) @ {m['currency']} {m['price']}"
            )
    mover_summary = "\n".join(mover_lines) if mover_lines else "No tickers above 5% threshold at this time."

    return {
        "ts":             int(time.time()),
        "prices":         all_prices,
        "movers_5pct":    movers_5pct,
        "mover_summary":  mover_summary,
        "catalysts":      catalysts_raw[:10],
        "hot_sectors":    [{"sector": s, "count": c} for s, c in hot_sectors],
        "total_tickers":  len(all_prices),
        "total_catalysts": len(catalysts_raw),
    }


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
