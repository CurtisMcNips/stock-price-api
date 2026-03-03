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

# ── Config ────────────────────────────────────────────────────
REDIS_URL       = os.environ.get("REDIS_URL", "redis://localhost:6379")
SECRET_KEY      = os.environ.get("SECRET_KEY", "change-me-in-production-railway-env")
CACHE_TTL       = 5
TOKEN_TTL       = 60 * 60 * 24 * 30   # 30 days
REQUEST_TIMEOUT = 8

# Anthropic / Sebastian
ANTHROPIC_KEY   = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_URL   = "https://api.anthropic.com/v1/messages"
ANTHROPIC_MODEL = "claude-sonnet-4-20250514"

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
    mode:         str,
    live_prices:  str,
    catalysts:    str,
    extra_context: str = "",
) -> str:
    """
    Build Sebastian's full system prompt with live context injected server-side.
    The API key never leaves the server — all context assembly happens here.
    """
    mode_instruction = {
        "beginner": (
            "The user is new to markets. Always lead with a plain-English one-sentence explanation "
            "before any technical detail. Define jargon the first time you use it. Be warm and encouraging."
        ),
        "expert": (
            "The user is an experienced trader. Skip the basics. Lead with technical depth — "
            "wave state mechanics, confidence thresholds, bot alignment, signal strength. "
            "Use proper market terminology throughout."
        ),
        "auto": (
            "Lead with a plain one-sentence summary that anyone can understand, "
            "then provide full technical depth underneath. "
            "This serves both beginners and experienced traders reading the same response."
        ),
    }.get(mode, "")

    extra_block = f"\nADDITIONAL CONTEXT:\n{extra_context}\n" if extra_context else ""

    return f"""You are Sebastian — the user-facing analyst for Market Brain, a live market intelligence platform.

{mode_instruction}

CORE RULES:
- Never tell users to buy or sell. Explain the picture clearly and let them decide.
- Always distinguish between what the intelligence engine has decided (fact) and your interpretation (view).
- Keep responses focused: 3-5 short paragraphs maximum unless detail is explicitly requested.
- When asked about a specific asset, reference its live price from the data below.
- When asked about opportunities, reference the catalyst data below.
- Never invent prices, signals, wave states, or catalyst data. If something isn't provided, say so.
- End every substantive answer with one concrete thing to watch next.
- This is not financial advice — be clear about that without being repetitive.

WAVE STATE GUIDE (for explaining to users):
- spark: single early signal, unconfirmed — interesting but not actionable yet
- confirmed: 2+ bot sources aligned, confidence ≥42% — worth tracking seriously
- escalation: 2+ bots + verification or cross-asset, confidence ≥58% — building momentum
- structural: 3+ bots + verified + cross-asset, confidence ≥72%, age ≥2h — significant shift
- regime: 4+ bots + verified + cross-asset + geo/macro, confidence ≥85%, age ≥6h — major event

LIVE MARKET PRICES (fetched {time.strftime("%H:%M UTC")}):
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
    """
    if not ANTHROPIC_KEY:
        raise HTTPException(503, "Sebastian is not configured — ANTHROPIC_API_KEY missing from environment")

    if not req.question or not req.question.strip():
        raise HTTPException(400, "Question cannot be empty")

    # ── 1. Fetch live prices ───────────────────────────────────
    live_prices_str = "Prices temporarily unavailable."
    try:
        # Merge core tickers with any extra tickers the frontend sent
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
        log.info(f"Sebastian: fetched {len(price_lines)} live prices for {current_user['email']}")
    except Exception as e:
        log.warning(f"Sebastian price fetch error: {e}")

    # ── 2. Fetch active catalysts from Redis ───────────────────
    catalysts_str = await _fetch_catalysts_for_sebastian()

    # ── 3. Extra context from frontend (e.g. asset detail view) ─
    extra_context = ""
    if req.context.get("asset_context"):
        extra_context = str(req.context["asset_context"])[:500]
    if req.context.get("catalyst"):
        cat = req.context["catalyst"]
        extra_context += (
            f"\nFocused catalyst: {cat.get('title') or cat.get('headline', '')}\n"
            f"Wave: {cat.get('wave')} | Direction: {cat.get('direction')} | "
            f"Confidence: {cat.get('confidence', 0):.0f}%\n"
            f"Assets: {', '.join((cat.get('assets') or [])[:5])}"
        )

    # ── 4. Build system prompt ─────────────────────────────────
    system = _build_sebastian_system(req.mode, live_prices_str, catalysts_str, extra_context)

    # ── 5. Build message history ───────────────────────────────
    messages = []
    for msg in req.history[-12:]:   # last 12 turns of context
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
                    "max_tokens": 1000,
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
                "prices_fetched": len([l for l in live_prices_str.split("\n") if l.strip().startswith("  ")]),
                "model":          ANTHROPIC_MODEL,
                "ts":             int(time.time()),
            }

    except HTTPException:
        raise
    except httpx.TimeoutException:
        log.warning("Sebastian: Anthropic request timed out")
        raise HTTPException(504, "Sebastian timed out — try again")
    except Exception as e:
        log.error(f"Sebastian endpoint error: {e}")
        raise HTTPException(500, f"Sebastian error: {str(e)}")


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
