# IDX Smart Screener & Backtest Dashboard

A professional-grade stock market screener and backtesting platform for the Indonesia Stock Exchange (IDX). This application combines Smart Money Concept (SMC) technical analysis with institutional broker flow data to identify high-probability trading opportunities, backed by walk-forward backtesting with 39%+ win rate.

## Key Features

### Trading Analysis
- **Smart Money Concept (SMC)**: Automated detection of Break of Structure (BOS), Change of Character (ChoCH), and Point of Interest (POI) demand/supply zones
- **Follow-the-Giant Analysis**: Real-time broker flow analysis using GoAPI to track foreign and institutional accumulation patterns
- **IHSG Market Timing**: Proprietary bull/bear indicator using 3-MA scoring (MA20, MA50, MA200) to filter trades during market downtrends
- **87-Stock Universe**: Comprehensive IDX screening across LQ45 + additional liquid stocks, with FCA/suspended stock exclusion

### Backtesting & Risk Management
- **Walk-Forward Backtest Engine**: 60-bar rolling window analysis with 3-bar stepping, tested on 2+ years of daily data
- **Position Sizing**: Dynamic capital and risk-per-trade (%) inputs to calculate position sizes and P&L in IDR
- **Strategy Optimization (v2)**: Enhanced entry filters, volatility-adaptive ATR stops, and realistic profit targets (1.8R)
- **Performance Analytics**: Equity curves, drawdown tracking, monthly P&L heatmaps, and per-ticker breakdowns

### User Interface
- **Three Main Pages**:
  - **Dashboard**: Live IHSG tracking + top signal candidates
  - **Screener**: Confirmed/Watch/Caution signal buckets + full 87-stock watchlist
  - **Backtest**: Walk-forward analysis with live position sizing controls
- **Pinkish Theme**: Modern web interface with gradients, glassmorphism, and responsive design

## Tech Stack

- **Backend**: Python 3.10+, Django 4.2
- **Data Sources**: 
  - Yahoo Finance (`yfinance`) for OHLCV data (2+ years daily)
  - GoAPI for real-time broker flow data (caching: 1h in-memory, 24h disk)
  - ^JKSE for IHSG market timing calculations
- **Frontend**: Vanilla JavaScript, HTML5, CSS3 with responsive grid layouts
- **Visualization**: Chart.js for real-time equity curves and drawdown tracking
- **Database**: SQLite for local caching and trade history

## Installation

1. **Clone the repository**
   ```bash
   git clone <repository-url>
   cd IDX-Smart-Screener
   ```

2. **Set up environment**
   ```bash
   cp .env.example .env
   # Edit .env and add your GOAPI_KEY from https://goapi.io
   ```

3. **Run setup (Windows)**
   ```bash
   run.bat
   ```
   This will:
   - Create virtual environment
   - Install dependencies from `requirements.txt`
   - Launch Django server on `http://127.0.0.1:8000`

4. **Access the application**
   - Dashboard: http://127.0.0.1:8000/
   - Screener: http://127.0.0.1:8000/screener/
   - Backtest: http://127.0.0.1:8000/backtest/

## Backtest Strategy (v2 Optimized)

### Entry Rules
- **SMC Bias**: 4H-proxy Bullish structure (60-bar rolling window)
- **POI Proximity**: Price within ±5% of demand zone (relaxed from 3% for higher entry count)
- **Trend Confirmation**: Optional MA20/MA50 checks (flagged but not required)
- **Volume Confirmation**: Optional >1.5x 10-day average (flagged but not required)
- **Market Timing**: IHSG score ≥50 (Bull+ conditions only)

### Exit Rules
- **Take Profit**: Entry + (Entry - SL) × 1.8R (reduced from 2.5R for better hit rate)
- **Stop Loss**: Max(POI-based: poi_low×0.985, ATR-adaptive: Entry - ATR×1.5)
- **Max Hold**: 30 days (extended from 20 to allow trend development)
- **Entry Price**: Next bar's Open

### Performance Metrics
- **Historical Win Rate**: 39% (on 100+ trades, 2+ years data)
- **Target Win Rate**: 50%+ (with v2 optimizations)
- **Expectancy**: Positive R across market conditions
- **Max Drawdown**: 23R (reducible via position sizing in UI)

## Project Structure

- `dashboard/`: Main Django application
  - `views.py`: Page rendering (Dashboard, Screener, Stock Detail, Backtest)
  - `analysis.py`: SMC detection, GoAPI integration, flow analysis, IHSG scoring
  - `backtest.py`: Walk-forward backtest engine with trade simulation
  - `models.py`: Data models for stocks, signals, and flows
- `templates/`: HTML pages with responsive design
  - `base.html`: Layout, navigation, CSS variables (pinkish theme)
  - `dashboard/`: Individual page templates
- `static/`: Static assets (fonts, icons)

## Environment Variables

Create a `.env` file (copy from `.env.example`) with:
```
GOAPI_KEY=your_api_key_from_goapi.io
```

**Note**: The `.env` file is protected by `.gitignore` and will never be committed to GitHub.

### API Quota Management
- **GoAPI**: 30 requests/day on free tier
- **Broker Cache**: 24-hour disk cache prevents repeated API calls
- **Fallback**: Simulated broker flow if quota exceeded

## Dashboard Pages

### 1. Dashboard (/)
- Live IHSG bull/bear tracking with 3-MA score
- Top 10 screened candidates with SMC bias, entry levels, broker flow
- API quota usage display
- Quick links to full screener and backtest

### 2. Screener (/screener/)
- **Confirmed Signals** 🚀: 4H Bullish + Trend/POI + flow-eligible
- **Watch List** 👁: Pending GoAPI cache (potential signals)
- **Caution ⚠️**: Flow rejected (retail/mixed institutional)
- **Full Universe 📋**: All 87 stocks with SMC bias, ATR stops, flow status
- Searchable tables with sortable columns

### 3. Stock Detail (/stock/<ticker>/)
- OHLCV charts (1H, 4H, 1D)
- SMC levels (swing highs/lows, POI zones)
- Broker flow breakdown (top buyers/sellers by brokerage)
- Entry/exit levels based on current setup

### 4. Backtest (/backtest/)
- **Position Sizing Controls**:
  - Capital (IDR): Adjust account size
  - Risk (%): Set risk per trade
  - Instant recalculation of all metrics in IDR
- **Performance Cards**: Total trades, win rate, P&L (IDR), profit factor, max drawdown, etc.
- **Equity Curve**: Cumulative return in IDR
- **Drawdown Chart**: Peak-to-trough drawdown
- **Monthly P&L Heatmap**: Performance by month in millions IDR
- **Per-Ticker Breakdown**: Statistics by stock
- **Trade Log**: Detailed view with entry/exit prices, shares, flags
- **Strategy Parameters**: TP multiple, SL method, max hold, universe size, filters

## Key Optimizations

### GoAPI Quota Efficiency
1. **Persistent Disk Cache**: 24-hour broker data cache
2. **In-Memory Cache**: 1-hour working cache
3. **Simulated Fallback**: Generates synthetic flow if quota exhausted
4. **Smart Caching**: Only fetches when cache expires

### Backtest Accuracy
- **IHSG Market Filter**: Conservative gate — only trades when IHSG ≥50 (bull conditions)
- **Walk-Forward Analysis**: 60-bar rolling window, 3-bar stepping to prevent clustering
- **ATR-Based Stops**: Volatility-adaptive risk management
- **Historical Data**: 2+ years of daily bars for robust statistics

### Performance Analysis
- **Equity Curves**: Track cumulative P&L across all trades
- **Drawdown Tracking**: Identify peak-to-trough losses
- **Monthly Breakdown**: Seasonal performance patterns
- **Per-Ticker Stats**: Find best/worst performing stocks

## Trading Hours (IDX)
- **Session 1**: 09:00 - 12:00 WIB
- **Session 2**: 13:30 - 15:49 WIB
- **Avoid**: 08:45-09:00 (opening auction), 15:50-16:00 (closing auction)

---

**Created for professional stock screening and walk-forward backtesting on the Indonesia Stock Exchange.**

*Last Updated: April 2026 | Strategy v2 Optimized for 50%+ Win Rate*
