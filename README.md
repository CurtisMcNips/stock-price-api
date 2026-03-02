# Market Brain v2

Conviction-driven market intelligence system. Catalysts over prices.

## Stack

- **Backend**: FastAPI + Redis (Railway)
- **Frontend**: Single-file React (static/index.html)
- **AI**: Claude via Anthropic API (MARI chat)
- **Data**: Yahoo Finance

## Repo structure

```
/
├── app.py                  # FastAPI backend — auth, prices, CoS, AI chat
├── static/
│   └── index.html          # React frontend — catalyst intelligence dashboard
├── requirements.txt
├── railway.toml
└── .gitignore
```

## Environment variables (Railway)

| Variable | Description |
|---|---|
| `REDIS_URL` | Redis connection string (auto-set by Railway Redis plugin) |
| `SECRET_KEY` | Random string for token signing |
| `ANTHROPIC_API_KEY` | Your Anthropic API key for MARI chat |

## API endpoints

### Auth
- `POST /api/auth/register`
- `POST /api/auth/login`
- `POST /api/auth/logout`
- `GET  /api/auth/me`

### Chief of Staff (Catalyst Intelligence)
- `POST   /api/cos/signal` — ingest a signal from a bot or manually
- `GET    /api/cos/catalysts` — all active catalysts
- `GET    /api/cos/catalyst/{id}` — single catalyst detail
- `POST   /api/cos/attention` — set Watch / Focus / Actioned
- `DELETE /api/cos/catalyst/{id}` — dismiss
- `GET    /api/cos/brief/{asset}` — catalyst brief for an asset

### Prices
- `GET /api/price/{symbol}`
- `GET /api/prices?symbols=AAPL,NVDA`
- `GET /api/universe`
- `GET /api/search?q=nvidia`
- `WS  /ws/{symbol}`

### AI
- `POST /api/ai-chat`

## Deploy

1. Push to GitHub
2. Connect repo to Railway
3. Add Redis plugin
4. Set env vars
5. Deploy

## Catalyst wave system

```
SPARK → CONFIRMED → ESCALATION → STRUCTURAL → REGIME
```

Confidence decays automatically. Signals from research bots merge into catalysts by asset/sector + direction within a 6-hour window.
