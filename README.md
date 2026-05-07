# STIX — Systematic Trading Intelligence eXpert

> A quantitative swing trading platform for the Indonesia Stock Exchange (IDX).
> Strategy selected by algorithm. Validated over 10 years of market data.

![Python](https://img.shields.io/badge/Python-3.10+-3776AB?style=flat&logo=python&logoColor=white)
![Django](https://img.shields.io/badge/Django-4.2-092E20?style=flat&logo=django&logoColor=white)
![Backtest](https://img.shields.io/badge/Backtest-10yr%20%7C%2056.3%25%20WR-00d9ff?style=flat)

---

## What is STIX?

STIX is a systematic, rules-based trading platform that screens Indonesia Stock Exchange (IDX) equities using a 7-filter entry system combining Smart Money Concepts (SMC), multi-timeframe analysis, and institutional broker flow data. Every signal is grounded in a validated 10-year backtest — not discretion, not guesswork.

**The math works. The system runs it.**

---

## Backtest Performance (10-Year, 376 IDX Stocks)

| Metric | Result |
|--------|--------|
| Total Trades | 142 |
| Win Rate | 56.3% |
| Total P&L | +15.75R |
| Profit Factor | 1.99 |
| Avg Win | +0.97R |
| Backtest Period | 2014 – 2024 |

> R = Risk unit. 1R = 1% of portfolio risked per trade.

---

## Strategy: Daily V3.1 — 7-Filter Entry System

All 7 filters must pass before a signal is generated:

| # | Filter | Logic |
|---|--------|-------|
| 1 | **IHSG Market Gate** | IHSG regime score ≥ 50 (Bull or Strong Bull only) |
| 2 | **Daily SMC Bias** | Break of Structure (BOS) or Change of Character (CHoCH) detected |
| 3 | **POI Demand Zone** | Price within ±5% of identified demand zone |
| 4 | **Rejection Candle** | Green close or hammer — buyers defending the zone |
| 5 | **Weekly MTF Alignment** | 26-week rolling window SMC bias = Bullish |
| 6 | **Trend + Volume** | Close above MA20 & volume ≥ 1.1× 10-day average |
| 7 | **Smart Money Flow** | Broker flow confirms institutional/foreign accumulation via GoAPI |

**Exit Strategy (Partial Positions):**
- TP1 = +1R → exit ⅓ position, move SL to breakeven
- TP2 = +2R → exit ⅓ position, move SL to TP1
- TP3 = +3R → exit final ⅓

---

## Features

- **Live Screener** — Daily scan of 376 IDX stocks across Non-FCA and FCA universes
- **IHSG Regime Gate** — Market-condition-aware filtering (Bull / Ranging / Bear)
- **Trade Log** — Signal tracking with entry, SL, TP, and outcome recording
- **Backtest Dashboard** — Walk-forward 10-year backtest with grade breakdown (A/B/C/D)
- **Broker Flow Analysis** — Institutional vs retail flow via GoAPI integration
- **Smart Quota Management** — GoAPI requests only triggered for top-tier candidates

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Backend | Python 3.10+, Django 4.2 |
| Data — OHLCV | Yahoo Finance (`yfinance`) |
| Data — Broker Flow | [GoAPI](https://goapi.io) |
| Frontend | Vanilla JS, HTML5, CSS3 |
| Charts | Plotly.js |
| Database | SQLite (local dev) |

---

## Getting Started

### Prerequisites
- Python 3.10+
- A GoAPI key — get one at [goapi.io](https://goapi.io)

### Installation

```bash
# 1. Clone the repository
git clone https://github.com/DwilaksanaAbdullahRasyid/IDXScreener.git
cd IDXScreener

# 2. Set up environment variables
cp .env.example .env
# Edit .env and add your GOAPI_KEY

# 3. Start the application (Windows)
run.bat
```

`run.bat` will automatically:
- Create a Python virtual environment
- Install all dependencies from `requirements.txt`
- Launch the Django server at `http://127.0.0.1:8000`

### Manual Setup (Linux/macOS)

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python manage.py runserver
```

---

## Environment Variables

Create a `.env` file in the project root (see `.env.example`):

```env
GOAPI_KEY=your_goapi_key_here
```

> The `.env` file is excluded from version control. Never commit secrets.

---

## Project Structure

```
IDXScreener/
├── dashboard/
│   ├── analysis.py          # Core SMC detection, broker flow, IHSG scoring
│   ├── backtest.py          # Walk-forward 10-year backtest engine (V3.1)
│   ├── backtest_dual.py     # Dual-universe backtest (Non-FCA + FCA)
│   ├── strategy_config.py   # Strategy parameters and baseline metrics
│   ├── trade_log.py         # Trade logging and status tracking
│   ├── views.py             # Django view controllers
│   ├── urls.py              # URL routing
│   └── models.py            # Database models
├── templates/
│   └── dashboard/           # HTML templates (landing, screener, trade log, backtest)
├── screener/
│   ├── settings.py          # Django configuration
│   └── urls.py              # Root URL config
├── .env.example             # Environment variable template
├── requirements.txt         # Python dependencies
├── manage.py                # Django management entrypoint
└── run.bat                  # Windows one-click launcher
```

---

## Signal Grading

Each signal is graded A–D based on confluence quality:

| Grade | Conditions Met | Position Size | Expected Edge |
|-------|---------------|---------------|---------------|
| A | 6–7 / 7 signals tight | 100% | Highest WR, largest win |
| B | 4–5 / 7 signals | 75% | Solid trade |
| C | 2–3 / 7 signals | 50% | Marginal |
| D | 0–1 / 7 signals | Skip | No edge |

---

## Disclaimer

This software is for educational and research purposes only. It does not constitute financial advice. Past backtest performance does not guarantee future results. Always conduct your own due diligence before making any investment decisions.
