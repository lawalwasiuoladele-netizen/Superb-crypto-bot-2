# Superb Crypto-Bot-2

Automated Bybit trading bot with:
- Multi-account support (store via dashboard)
- Compounding trades (uses available USDT per account)
- Precision-aware quantity handling
- Time-based profit windows: 30% (0–8m), 25% (8–11m), 20% (11–15m), 15% (15–20m), forced sell after 20m
- Daily 50-trades limit (resets at local midnight)
- Web dashboard: API input, Start/Stop, live logs (SSE)
- Option to run locally or deploy to Render for 24/7 hosting

## Quick Start (local)

1. Install Python 3.9+.
2. Create a folder and place `server.py`, `bot.py`, `templates/index.html`, `requirements.txt`.
3. Install dependencies:
   ```bash
   pip install -r requirements.txt
