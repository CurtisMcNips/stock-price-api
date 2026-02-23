import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from typing import Dict, List, Optional

import httpx
import redis.asyncio as aioredis
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Query
from fastapi.middleware.cors import CORSMiddleware

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

REDIS_URL = "redis://localhost:6379"
CACHE_TTL = 5
WS_POLL_INTERVAL = 3
REQUEST_TIMEOUT = 8

YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
YAHOO_FALLBACK_URL = "https://query2.finance.yahoo.com/v8/finance/chart/{symbol}"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
}

_memory_cache: Dict[str, dict] = {}
redis_client: Optional[aioredis.Redis] = None


async def get_redis() -> Optional[aioredis.Redis]:
    global redis_client
    if redis_client:
        try:
            await redis_client.ping()
            return redis_client
        except Exception:
            redis_client = None
    try:
        import os
        url = os.environ.get("REDIS_URL", REDIS_URL)
        redis_client = await aioredis.from_url(url, decode_responses=True, socket_timeout=2)
        await redis_client.ping()
        log.info("Redis connected")
        return redis_client
    except Exception as e:
        log.warning(f"Redis unavailable ({e}) - using in-memory cache")
        return None


@asynccontextmanager
async def lifespan(app: FastAPI):
    await get_redis()
    yield
    if redis_client:
        await redis_client.aclose()


app = FastAPI(
    title="Stock Price API",
    description="Real-time stock prices via Yahoo Finance. No API keys needed.",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def normalise_symbol(symbol: str) -> str:
    symbol = symbol.upper().strip()
    exchange_map = {
        "LON:": ".L",
        "EPA:": ".PA",
        "ETR:": ".DE",
        "AMS:": ".AS",
        "TSX:": ".TO",
        "ASX:": ".AX",
    }
    for prefix, suffix in exchange_map.items():
        if symbol.startswith(prefix):
            return symbol[len(prefix):] + suffix
    return symbol


async def cache_get(key: str) -> Optional[dict]:
    r = await get_redis()
    if r:
        try:
            val = await r.get(key)
            return json.loads(val) if val else None
        except Exception:
            pass
    entry = _memory_cache.get(key)
    if entry and (time.time() - entry["_ts"]) < CACHE_TTL:
        return entry["data"]
    return None


async def cache_set(key: str, data: dict):
    r = await get_redis()
    if r:
        try:
            await r.setex(key, CACHE_TTL, json.dumps(data))
            return
        except Exception:
            pass
    _memory_cache[key] = {"data": data, "_ts": time.time()}


async def fetch_yahoo(symbol: str, client: httpx.AsyncClient) -> Optional[dict]:
    urls = [
        YAHOO_URL.format(symbol=symbol),
        YAHOO_FALLBACK_URL.format(symbol=symbol),
    ]
    for url in urls:
        try:
            r = await client.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            if r.status_code != 200:
                continue
            data = r.json()
            result = data.get("chart", {}).get("result", [])
            if not result:
                continue
            meta = result[0].get("meta", {})
            price = meta.get("regularMarketPrice") or meta.get("previousClose")
            if not price:
                continue
            prev_close = meta.get("previousClose") or meta.get("chartPreviousClose") or price
            change = price - prev_close
            change_pct = (change / prev_close * 100) if prev_close else 0
            market_state = meta.get("marketState", "CLOSED")
            if market_state == "PRE":
                price = meta.get("preMarketPrice") or price
            elif market_state == "POST":
                price = meta.get("postMarketPrice") or price
            return {
                "symbol": symbol,
                "price": round(float(price), 4),
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
                "timestamp": int(time.time()),
                "source": "yahoo_finance",
            }
        except httpx.TimeoutException:
            log.warning(f"Timeout fetching {symbol}")
        except Exception as e:
            log.warning(f"Error fetching {symbol}: {e}")
    return None


_last_known: Dict[str, dict] = {}
_http_client: Optional[httpx.AsyncClient] = None


async def get_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(
            limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
            timeout=REQUEST_TIMEOUT,
        )
    return _http_client


async def get_price(symbol: str, client: httpx.AsyncClient) -> dict:
    cache_key = f"price:{symbol}"
    cached = await cache_get(cache_key)
    if cached:
        cached["cached"] = True
        return cached
    data = await fetch_yahoo(symbol, client)
    if data:
        _last_known[symbol] = data
        await cache_set(cache_key, data)
        data["cached"] = False
        return data
    if symbol in _last_known:
        stale = dict(_last_known[symbol])
        stale["stale"] = True
        stale["cached"] = False
        stale["warning"] = "Using last known price - live fetch failed"
        return stale
    return {
        "symbol": symbol,
        "price": None,
        "error": "Price temporarily unavailable",
        "retry_after": 5,
        "timestamp": int(time.time()),
        "source": "unavailable",
    }


@app.get("/")
async def root():
    return {"status": "ok", "docs": "/docs", "api": "/api/price/AAPL"}


@app.get("/health")
async def health():
    r = await get_redis()
    return {
        "status": "healthy",
        "redis": "connected" if r else "unavailable (using memory cache)",
        "timestamp": int(time.time()),
    }


@app.get("/api/price/{symbol}", tags=["Prices"])
async def get_single_price(symbol: str):
    symbol = normalise_symbol(symbol)
    client = await get_client()
    return await get_price(symbol, client)


@app.get("/api/prices", tags=["Prices"])
async def get_multiple_prices(
    symbols: str = Query(..., description="Comma-separated symbols e.g. AAPL,TSLA,MSFT"),
    delay_ms: int = Query(0, description="Delay between requests in ms (0=parallel)"),
):
    raw = [s.strip() for s in symbols.split(",") if s.strip()]
    if not raw:
        raise HTTPException(400, "No symbols provided")
    if len(raw) > 50:
        raise HTTPException(400, "Maximum 50 symbols per request")
    sym_list = [normalise_symbol(s) for s in raw]
    client = await get_client()
    if delay_ms == 0:
        results = await asyncio.gather(*[get_price(s, client) for s in sym_list])
    else:
        results = []
        for s in sym_list:
            results.append(await get_price(s, client))
            if delay_ms:
                await asyncio.sleep(delay_ms / 1000)
    return {
        "symbols": sym_list,
        "count": len(results),
        "timestamp": int(time.time()),
        "data": {r["symbol"]: r for r in results},
    }


@app.get("/api/search", tags=["Prices"])
async def search_symbol(q: str = Query(..., description="Company name or ticker")):
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(
                f"https://query1.finance.yahoo.com/v1/finance/search?q={q}&quotesCount=8",
                headers=HEADERS,
                timeout=8,
            )
            data = r.json()
            quotes = data.get("quotes", [])
            return {
                "query": q,
                "results": [
                    {
                        "symbol": q["symbol"],
                        "name": q.get("shortname") or q.get("longname"),
                        "exchange": q.get("exchDisp"),
                        "type": q.get("typeDisp"),
                    }
                    for q in quotes if q.get("symbol")
                ],
            }
    except Exception as e:
        raise HTTPException(500, f"Search failed: {e}")


class ConnectionManager:
    def __init__(self):
        self.active: Dict[str, List[WebSocket]] = {}

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
            data["ws"] = True
            await websocket.send_json(data)
            await asyncio.sleep(WS_POLL_INTERVAL)
    except WebSocketDisconnect:
        log.info(f"WS disconnected: {symbol}")
    except Exception as e:
        log.error(f"WS error for {symbol}: {e}")
    finally:
        manager.disconnect(websocket, symbol)


if __name__ == "__main__":
    import os
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=False, log_level="info")
