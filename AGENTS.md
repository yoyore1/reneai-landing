# AGENTS.md

## Cursor Cloud specific instructions

### Overview

This is a Binance-Polymarket BTC 5-minute arbitrage bot with two components:
- **Python backend** (`bot/`): Trading engine using asyncio + aiohttp + websockets. Runs on port 8899.
- **React frontend** (`src/`): Create React App dashboard. Dev server on port 3000; production build served by the Python backend.

### Key commands

See `README.md` for full details. Quick reference:
- **Python deps**: `pip install -r requirements.txt`
- **Node deps**: `npm install` (lockfile: `package-lock.json`)
- **React dev server**: `npm start` (port 3000)
- **React build**: `npm run build` (output in `build/`, served by bot at port 8899)
- **Lint**: `npx eslint src/`
- **Tests**: `CI=true npx react-scripts test --watchAll=false --passWithNoTests` (no test files exist currently)
- **Bot (headless)**: `python3 -m bot --headless` or with flags: `--s3-only`, `--test`, `--test-inverse`, `--test-perfect`

### Known issues in the codebase

1. **`cfg.s3_only` missing**: `bot/config.py` Config class lacks `s3_only` attribute, but `bot/main.py:298` references `cfg.s3_only`. The CLI works if `--s3-only` is passed (Python `or` short-circuits), but fails without it due to `AttributeError`.
2. **`DashboardServer.__init__` signature mismatch**: `bot/main.py:238` passes 7 positional args (`feed, poly, strat, strat2, strat3, strat4`) plus `host`/`port`/`on_shutdown` kwargs, but `bot/server.py:32` expects only 5 positional args (`feed, strategy, strategy2, strategy3, strategy4`) and no `on_shutdown` kwarg. This causes `TypeError: got multiple values for argument 'host'`.
3. **Binance API geo-restriction**: Both the REST API (`api.binance.com`) and WebSocket (`stream.binance.com`) return HTTP 451 from cloud VM environments. The bot will start but won't receive live BTC prices. Polymarket APIs work fine.

### React frontend and backend WebSocket

The React app connects to the backend WebSocket at `ws://{window.location.host}/ws`. In development, the React dev server runs on port 3000 while the bot backend runs on port 8899. To make the frontend connect to the backend during development, either:
- Add a `"proxy": "http://localhost:8899"` to `package.json`, or
- Modify the `WS_URL` in `src/App.js` to point to `ws://localhost:8899/ws`

For production, `npm run build` creates static files in `build/` that the Python bot serves directly on port 8899, so the WebSocket URL resolves correctly.

### Environment config

Copy `.env.example` to `.env` for DRY_RUN mode (no API keys required). The bot defaults to `DRY_RUN=true` (paper trading).
