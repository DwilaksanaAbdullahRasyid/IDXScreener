# IDX Smart Screener & Dashboard

A professional-grade stock market screener for the Indonesia Stock Exchange (IDX). This application combines technical analysis with institutional data to identify high-probability trading opportunities.

## 🚀 Features

- **Smart Money Concept (SMC)**: Automated detection of Break of Structure (BOS), Change of Character (CHoCH), and Point of Interest (POI) zones.
- **Follow-the-Giant Analysis**: Real-time broker flow analysis using GoAPI to track foreign and institutional accumulation.
- **IHSG Bull Index**: A proprietary market health indicator based on technical moving averages.
- **LQ45 Scanner**: Batch scanning of Indonesia's most liquid stocks.
- **Pinkish Dashboard**: A modern, high-performance web interface with a custom pink/magenta aesthetic.

## 🛠 Tech Stack

- **Backend**: Python 3.10+, Django 4.2.
- **Data Sources**: Yahoo Finance (`yfinance`) for OHLCV, GoAPI for broker summary data.
- **Frontend**: Vanilla JavaScript, HTML5, CSS3 (with rich gradients and glassmorphism).
- **Visualization**: Plotly.js for interactive financial charting.

## 📦 Installation

1. Clone the repository.
2. Create a `.env` file from `.env.example` and add your `GOAPI_KEY`.
3. Run the setup and crawler script:
   ```bash
   run.bat
   ```
   *This will create the virtual environment, install dependencies, and launch the Django server.*

## 📂 Project Structure

- `dashboard/`: Main application logic including views and financial analysis.
- `templates/index.html`: The interactive dashboard frontend.
- `dashboard/analysis.py`: Core logic for SMC detection and GoAPI integration.
- `app.py`: Legacy Streamlit prototype for fallback/reference.

## 🔑 Environment Variables

The application requires a GoAPI key for live broker flow data.
- `GOAPI_KEY`: Your private API key from [GoAPI](https://goapi.io).

---
*Created for automated stock screening and advanced technical analysis on the IDX.*
