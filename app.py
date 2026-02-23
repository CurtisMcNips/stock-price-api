"""
Market Brain API v3 — Stock Price API + User Auth + Admin System
FastAPI backend with JWT authentication, user accounts, persistent portfolios,
admin roles, discount codes, asset management, and system-wide analytics.
"""

import asyncio
import json
import logging
import os
import time
import hashlib
import hmac
import base64
import secrets
import string
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Dict, List, Optional, Any

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
CACHE_TTL       = 5
TOKEN_TTL       = 60 * 60 * 24 * 30   # 30 days
REQUEST_TIMEOUT = 8
SUPER_ADMIN_EMAIL = "mark_smalley@hotmail.co.uk"

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
_memory_discount_codes: Dict[str, dict] = {}
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
            else:   await r.set(key, value)
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


# ── Auth helpers ──────────────────────────────────────────────
def hash_password(password: str) -> str:
    salt = os.urandom(16)
    key  = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 100_000)
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

def make_discount_code(length: int = 8) -> str:
    chars = string.ascii_uppercase + string.digits
    return "MB-" + "".join(secrets.choice(chars) for _ in range(length))


# ── User persistence ──────────────────────────────────────────
async def save_user(email: str, user: dict):
    await rset(f"user:{email}", json.dumps(user))
    _memory_users[email] = user

async def load_user(email: str) -> Optional[dict]:
    val = await rget(f"user:{email}")
    if val: return json.loads(val)
    return _memory_users.get(email)

async def load_all_users() -> List[dict]:
    keys = await rkeys("user:*")
    users = []
    for k in keys:
        val = await rget(k)
        if val:
            u = json.loads(val)
            users.append({k: v for k, v in u.items() if k != "password_hash"})
    if not users:
        users = [{k: v for k, v in u.items() if k != "password_hash"}
                 for u in _memory_users.values()]
    return users

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

async def load_all_portfolios() -> Dict[str, dict]:
    keys = await rkeys("portfolio:*")
    result = {}
    for k in keys:
        email = k.replace("portfolio:", "")
        val   = await rget(k)
        if val: result[email] = json.loads(val)
    if not result:
        result = dict(_memory_portfolios)
    return result

async def get_current_user(authorization: Optional[str] = Header(None)) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Not authenticated")
    token = authorization[7:]
    email = await load_token(token)
    if not email:
        raise HTTPException(401, "Invalid or expired token")
    user  = await load_user(email)
    if not user:
        raise HTTPException(401, "User not found")
    return user

async def require_admin(current_user: dict = Depends(get_current_user)) -> dict:
    if current_user.get("role") not in ("admin", "super_admin"):
        raise HTTPException(403, "Admin access required")
    return current_user

async def require_super_admin(current_user: dict = Depends(get_current_user)) -> dict:
    if current_user.get("role") != "super_admin":
        raise HTTPException(403, "Super admin access required")
    return current_user

# ── Discount code persistence ─────────────────────────────────
async def save_discount_code(code_id: str, code: dict):
    await rset(f"discount:{code_id}", json.dumps(code))
    _memory_discount_codes[code_id] = code

async def load_discount_code(code_id: str) -> Optional[dict]:
    val = await rget(f"discount:{code_id}")
    if val: return json.loads(val)
    return _memory_discount_codes.get(code_id)

async def load_all_discount_codes() -> List[dict]:
    keys = await rkeys("discount:*")
    codes = []
    for k in keys:
        val = await rget(k)
        if val: codes.append(json.loads(val))
    if not codes:
        codes = list(_memory_discount_codes.values())
    return codes


# ── Super-admin seed ──────────────────────────────────────────
async def seed_super_admin():
    """Ensure the designated super_admin account exists and has the right role."""
    email    = SUPER_ADMIN_EMAIL.lower().strip()
    existing = await load_user(email)
    if existing:
        if existing.get("role") != "super_admin":
            existing["role"] = "super_admin"
            await save_user(email, existing)
            log.info(f"Promoted {email} to super_admin")
    else:
        log.info(f"Super admin account {email} not yet registered — will be promoted on first login")


# ── Lifespan ──────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    await get_redis()
    await seed_super_admin()
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
    trades:       list
    watchItems:   list
    balance:      float
    startBalance: float

class RoleUpdate(BaseModel):
    email: str
    role:  str   # "user" | "admin" | "super_admin"

class DiscountCodeCreate(BaseModel):
    type:          str    # "percent" | "fixed"
    value:         float
    max_uses:      int    = 0    # 0 = unlimited
    expires_at:    Optional[int] = None
    product:       str    = "all"
    custom_code:   Optional[str] = None
    description:   str    = ""

class DiscountCodeUpdate(BaseModel):
    disabled: Optional[bool] = None
    max_uses: Optional[int]  = None
    expires_at: Optional[int] = None

class AssetUpdate(BaseModel):
    ticker:      str
    name:        str
    sector:      str
    sub:         str
    cap:         str
    vol:         str
    visible:     bool = True
    description: str  = ""
    pros:        List[str] = []
    cons:        List[str] = []
    sources:     List[str] = []


# ── Auth endpoints ────────────────────────────────────────────
@app.post("/api/auth/register", tags=["Auth"])
async def register(req: RegisterRequest):
    email = req.email.lower().strip()
    if len(req.password) < 6:
        raise HTTPException(400, "Password must be at least 6 characters")
    existing = await load_user(email)
    if existing:
        raise HTTPException(409, "Email already registered")
    role = "super_admin" if email == SUPER_ADMIN_EMAIL.lower() else "user"
    user = {
        "name":          req.name.strip(),
        "email":         email,
        "password_hash": hash_password(req.password),
        "role":          role,
        "created_at":    int(time.time()),
    }
    await save_user(email, user)
    token = make_token(email)
    await save_token(token, email)
    return {"token": token, "user": {"name": user["name"], "email": email, "role": role}}

@app.post("/api/auth/login", tags=["Auth"])
async def login(req: LoginRequest):
    email = req.email.lower().strip()
    user  = await load_user(email)
    if not user or not verify_password(req.password, user["password_hash"]):
        raise HTTPException(401, "Invalid email or password")
    # Auto-promote super admin on login
    if email == SUPER_ADMIN_EMAIL.lower() and user.get("role") != "super_admin":
        user["role"] = "super_admin"
        await save_user(email, user)
    token = make_token(email)
    await save_token(token, email)
    return {"token": token, "user": {"name": user["name"], "email": email, "role": user.get("role", "user")}}

@app.post("/api/auth/logout", tags=["Auth"])
async def logout(authorization: Optional[str] = Header(None)):
    if authorization and authorization.startswith("Bearer "):
        await delete_token(authorization[7:])
    return {"ok": True}

@app.get("/api/auth/me", tags=["Auth"])
async def me(current_user: dict = Depends(get_current_user)):
    return {"name": current_user["name"], "email": current_user["email"], "role": current_user.get("role", "user")}


# ── Portfolio endpoints ───────────────────────────────────────
@app.get("/api/portfolio", tags=["Portfolio"])
async def get_portfolio(current_user: dict = Depends(get_current_user)):
    return await load_portfolio(current_user["email"])

@app.post("/api/portfolio", tags=["Portfolio"])
async def save_portfolio_endpoint(data: PortfolioSave, current_user: dict = Depends(get_current_user)):
    await save_portfolio(current_user["email"], data.dict())
    return {"ok": True}


# ── Admin: User management ────────────────────────────────────
@app.get("/api/admin/users", tags=["Admin"])
async def admin_list_users(admin: dict = Depends(require_admin)):
    users = await load_all_users()
    portfolios = await load_all_portfolios()
    result = []
    for u in users:
        email = u.get("email", "")
        p = portfolios.get(email, {})
        trades     = p.get("trades", [])
        watches    = p.get("watchItems", [])
        closed     = [t for t in trades if t.get("status") != "open"]
        wins       = [t for t in closed if (t.get("finalPnLPct") or 0) > 0]
        expired_w  = [w for w in watches if w.get("status") == "expired"]
        watch_hits = [w for w in expired_w if w.get("dirCorrect")]
        result.append({
            **u,
            "trade_count":    len(trades),
            "watch_count":    len(watches),
            "closed_trades":  len(closed),
            "win_rate":       round(len(wins)/len(closed)*100, 1) if closed else 0,
            "watch_accuracy": round(len(watch_hits)/len(expired_w)*100, 1) if expired_w else 0,
            "balance":        p.get("balance", 1000),
            "start_balance":  p.get("startBalance", 1000),
            "total_pnl":      round(p.get("balance", 1000) - p.get("startBalance", 1000), 2),
        })
    return {"users": result, "total": len(result)}

@app.get("/api/admin/users/{email}/portfolio", tags=["Admin"])
async def admin_get_user_portfolio(email: str, admin: dict = Depends(require_admin)):
    user = await load_user(email.lower())
    if not user:
        raise HTTPException(404, "User not found")
    portfolio = await load_portfolio(email.lower())
    return {"user": {k: v for k, v in user.items() if k != "password_hash"}, "portfolio": portfolio}

@app.post("/api/admin/users/role", tags=["Admin"])
async def admin_update_role(req: RoleUpdate, admin: dict = Depends(require_super_admin)):
    if req.role not in ("user", "admin", "super_admin"):
        raise HTTPException(400, "Invalid role")
    user = await load_user(req.email.lower())
    if not user:
        raise HTTPException(404, "User not found")
    user["role"] = req.role
    await save_user(req.email.lower(), user)
    return {"ok": True, "email": req.email, "role": req.role}


# ── Admin: System-wide analytics ─────────────────────────────
@app.get("/api/admin/analytics", tags=["Admin"])
async def admin_analytics(admin: dict = Depends(require_admin)):
    portfolios = await load_all_portfolios()
    all_trades   = []
    all_watches  = []
    for p in portfolios.values():
        all_trades.extend(p.get("trades", []))
        all_watches.extend(p.get("watchItems", []))

    closed_trades  = [t for t in all_trades  if t.get("status") != "open"]
    expired_watches= [w for w in all_watches if w.get("status") == "expired"]

    def trade_stats(items):
        if not items: return {"count": 0, "win_rate": 0, "avg_pnl_pct": 0}
        wins = [t for t in items if (t.get("finalPnLPct") or 0) > 0]
        avg  = sum(t.get("finalPnLPct", 0) for t in items) / len(items)
        return {"count": len(items), "win_rate": round(len(wins)/len(items)*100, 1), "avg_pnl_pct": round(avg, 2)}

    def watch_stats(items):
        if not items: return {"count": 0, "hit_rate": 0, "avg_chg": 0}
        hits = [w for w in items if w.get("dirCorrect")]
        avg  = sum(w.get("actualChg", 0) for w in items) / len(items)
        return {"count": len(items), "hit_rate": round(len(hits)/len(items)*100, 1), "avg_chg": round(avg, 2)}

    # By sector
    sectors = list(set(t.get("sector","Unknown") for t in closed_trades + expired_watches))
    by_sector = {}
    for s in sectors:
        st = [t for t in closed_trades  if t.get("sector") == s]
        sw = [w for w in expired_watches if w.get("sector") == s]
        by_sector[s] = {"trades": trade_stats(st), "watches": watch_stats(sw)}

    # By cap
    caps = ["Nano","Micro","Small","Mid","Large"]
    by_cap = {}
    for c in caps:
        st = [t for t in closed_trades  if t.get("cap") == c]
        sw = [w for w in expired_watches if w.get("cap") == c]
        by_cap[c] = {"trades": trade_stats(st), "watches": watch_stats(sw)}

    # By timeframe
    tfs = list(set(t.get("timeframe","") for t in closed_trades + expired_watches))
    by_timeframe = {}
    for tf in tfs:
        if not tf: continue
        st = [t for t in closed_trades  if t.get("timeframe") == tf]
        sw = [w for w in expired_watches if w.get("timeframe") == tf]
        by_timeframe[tf] = {"trades": trade_stats(st), "watches": watch_stats(sw)}

    return {
        "overview": {
            "total_users":    len(portfolios),
            "total_trades":   len(all_trades),
            "total_watches":  len(all_watches),
            "closed_trades":  len(closed_trades),
            "expired_watches":len(expired_watches),
        },
        "global_trades":  trade_stats(closed_trades),
        "global_watches": watch_stats(expired_watches),
        "by_sector":      by_sector,
        "by_cap":         by_cap,
        "by_timeframe":   by_timeframe,
    }


# ── Admin: Discount codes ─────────────────────────────────────
@app.get("/api/admin/discount-codes", tags=["Admin"])
async def list_discount_codes(admin: dict = Depends(require_admin)):
    codes = await load_all_discount_codes()
    return {"codes": codes, "total": len(codes)}

@app.post("/api/admin/discount-codes", tags=["Admin"])
async def create_discount_code(req: DiscountCodeCreate, admin: dict = Depends(require_admin)):
    code_str = req.custom_code.upper() if req.custom_code else make_discount_code()
    code_id  = code_str
    code = {
        "id":          code_id,
        "code":        code_str,
        "type":        req.type,
        "value":       req.value,
        "max_uses":    req.max_uses,
        "uses":        0,
        "expires_at":  req.expires_at,
        "product":     req.product,
        "description": req.description,
        "disabled":    False,
        "created_at":  int(time.time()),
        "created_by":  admin["email"],
    }
    existing = await load_discount_code(code_id)
    if existing:
        raise HTTPException(409, "Code already exists")
    await save_discount_code(code_id, code)
    return code

@app.patch("/api/admin/discount-codes/{code_id}", tags=["Admin"])
async def update_discount_code(code_id: str, req: DiscountCodeUpdate, admin: dict = Depends(require_admin)):
    code = await load_discount_code(code_id)
    if not code:
        raise HTTPException(404, "Code not found")
    if req.disabled is not None: code["disabled"]    = req.disabled
    if req.max_uses is not None: code["max_uses"]    = req.max_uses
    if req.expires_at is not None: code["expires_at"] = req.expires_at
    await save_discount_code(code_id, code)
    return code

@app.delete("/api/admin/discount-codes/{code_id}", tags=["Admin"])
async def delete_discount_code(code_id: str, admin: dict = Depends(require_admin)):
    code = await load_discount_code(code_id)
    if not code:
        raise HTTPException(404, "Code not found")
    await rdel(f"discount:{code_id}")
    _memory_discount_codes.pop(code_id, None)
    return {"ok": True}

@app.post("/api/discount-codes/validate", tags=["Discount"])
async def validate_discount_code(code: str = Query(...), current_user: dict = Depends(get_current_user)):
    entry = await load_discount_code(code.upper())
    if not entry:
        raise HTTPException(404, "Invalid code")
    if entry.get("disabled"):
        raise HTTPException(400, "Code is disabled")
    if entry.get("expires_at") and time.time() > entry["expires_at"]:
        raise HTTPException(400, "Code has expired")
    if entry["max_uses"] > 0 and entry["uses"] >= entry["max_uses"]:
        raise HTTPException(400, "Code has reached its usage limit")
    return {"valid": True, "type": entry["type"], "value": entry["value"], "product": entry["product"]}


# ── Admin: System config (super_admin only) ───────────────────
@app.get("/api/admin/config", tags=["Admin"])
async def get_system_config(admin: dict = Depends(require_super_admin)):
    val = await rget("system:config")
    if val: return json.loads(val)
    return {
        "features": {
            "virtual_trading": True,
            "watchlist":       True,
            "admin_dashboard": True,
            "live_prices":     True,
            "discount_codes":  True,
        },
        "risk_thresholds": {"max_position_pct": 25, "default_stop_pct": 7},
        "cache_ttl":  CACHE_TTL,
        "max_assets": 500,
    }

@app.post("/api/admin/config", tags=["Admin"])
async def update_system_config(config: dict, admin: dict = Depends(require_super_admin)):
    await rset("system:config", json.dumps(config))
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
            data   = r.json()
            result = data.get("chart", {}).get("result", [])
            if not result: continue
            meta  = result[0].get("meta", {})
            price = meta.get("regularMarketPrice") or meta.get("previousClose")
            if not price: continue
            prev_close  = meta.get("previousClose") or meta.get("chartPreviousClose") or price
            change      = price - prev_close
            change_pct  = (change / prev_close * 100) if prev_close else 0
            market_state= meta.get("marketState", "CLOSED")
            if market_state == "PRE":  price = meta.get("preMarketPrice")  or price
            elif market_state == "POST": price = meta.get("postMarketPrice") or price
            return {
                "symbol": symbol, "price": round(float(price), 4),
                "change": round(float(change), 4), "change_pct": round(float(change_pct), 4),
                "prev_close": round(float(prev_close), 4),
                "currency": meta.get("currency", "USD"), "market_state": market_state,
                "exchange": meta.get("exchangeName", ""),
                "name": meta.get("shortName") or meta.get("longName") or symbol,
                "volume":              meta.get("regularMarketVolume"),
                "day_high":            meta.get("regularMarketDayHigh"),
                "day_low":             meta.get("regularMarketDayLow"),
                "fifty_two_week_high": meta.get("fiftyTwoWeekHigh"),
                "fifty_two_week_low":  meta.get("fiftyTwoWeekLow"),
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
        ("LON:","\.L"),("EPA:",".PA"),("ETR:",".DE"),
        ("AMS:",".AS"),("TSX:",".TO"),("ASX:",".AX"),
    ]:
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
    if not raw:         raise HTTPException(400, "No symbols")
    if len(raw) > 50:  raise HTTPException(400, "Max 50 symbols")
    sym_list = [normalise_symbol(s) for s in raw]
    client   = await get_client()
    if delay_ms == 0:
        results = await asyncio.gather(*[get_price(s, client) for s in sym_list])
    else:
        results = []
        for s in sym_list:
            results.append(await get_price(s, client))
            await asyncio.sleep(delay_ms / 1000)
    return {"symbols": sym_list, "count": len(results), "timestamp": int(time.time()),
            "data": {r["symbol"]: r for r in results}}

@app.get("/api/search", tags=["Prices"])
async def search_symbol(q: str = Query(...)):
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(
                f"https://query1.finance.yahoo.com/v1/finance/search?q={q}&quotesCount=8",
                headers=HEADERS, timeout=8,
            )
            quotes = r.json().get("quotes", [])
            return {"query": q, "results": [
                {"symbol": q["symbol"], "name": q.get("shortname") or q.get("longname"), "exchange": q.get("exchDisp")}
                for q in quotes if q.get("symbol")
            ]}
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
    except Exception as e:      log.error(f"WS error {symbol}: {e}")
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
