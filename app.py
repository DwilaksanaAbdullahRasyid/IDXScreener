import streamlit as st
import pandas as pd
import numpy as np
import yfinance as yf
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import requests
import random

import os
from dotenv import load_dotenv

load_dotenv()

# --- Constants ---
API_KEY = os.getenv("GOAPI_KEY")

FOREIGN_BROKERS = ['YU','CG','KZ','CS','DP','GW','BK','DU','HD','AG','BQ','RX','ZP','YP','MS','XA','RB','TP','KK','LS','DR','LH','AH','LG','AK','AI','FS']
LOCAL_BUMN_BROKERS = ['CC','NI','OD','DX']
RETAIL_DOMINANT = ['YP','XC','XL','MG','AZ','KK']

def parse_brokers(broker_list):
    return [b.upper() for b in broker_list]

FOREIGN_BROKERS = parse_brokers(FOREIGN_BROKERS)
LOCAL_BUMN_BROKERS = parse_brokers(LOCAL_BUMN_BROKERS)
RETAIL_DOMINANT = parse_brokers(RETAIL_DOMINANT)

FOREIGN_BROKERS = [b for b in FOREIGN_BROKERS if b not in RETAIL_DOMINANT]

st.set_page_config(page_title="IDX Smart Screener", layout="wide")
st.markdown("<style>.block-container {padding-top: 1rem;}</style>", unsafe_allow_html=True)

st.title("📈 IDX Smart Screener & Dashboard")
st.markdown("**Automated Screener using SMC (Smart Money Concept) & Follow The Giant logic**")

# --- UI Sidebar ---
st.sidebar.header("Filters")
ticker = st.sidebar.text_input("Enter Ticker (e.g. BBCA, BBRI)", value="BBCA").upper()
ticker_yf = f"{ticker}.JK"
st.sidebar.markdown(f"**GoAPI Rate Limit:** 30 requests/day")

# --- Data Fetching ---
@st.cache_data(ttl=3600)
def fetch_ihsg():
    try:
        df = yf.download("^JKSE", period="1y", interval="1d", progress=False)
        if df.empty: return 50.0, "Neutral"
        
        # Flatten tuple columns if using new yfinance
        df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
        close = df['Close']
        current = close.iloc[-1].item() if hasattr(close.iloc[-1], 'item') else close.iloc[-1]
        
        ma20 = close.rolling(20).mean().iloc[-1]
        ma50 = close.rolling(50).mean().iloc[-1]
        ma200 = close.rolling(200).mean().iloc[-1]
        ma20 = ma20.item() if hasattr(ma20, 'item') else ma20
        ma50 = ma50.item() if hasattr(ma50, 'item') else ma50
        ma200 = ma200.item() if hasattr(ma200, 'item') else ma200
        
        score = 0
        if current > ma20: score += 30
        if current > ma50: score += 30
        if current > ma200: score += 40
        
        if score >= 60: status = "Bullish/Bull"
        elif score <= 40: status = "Bearish/Bear"
        else: status = "Neutral"
        return score, status
    except Exception as e:
        return 50.0, "Neutral"

@st.cache_data(ttl=3600)
def fetch_yf_data(t):
    try:
        df_1d = yf.download(t, period="3mo", interval="1d", progress=False)
        df_1h = yf.download(t, period="1mo", interval="1h", progress=False)
        df_1d.columns = [c[0] if isinstance(c, tuple) else c for c in df_1d.columns]
        df_1h.columns = [c[0] if isinstance(c, tuple) else c for c in df_1h.columns]
        
        df_4h = df_1h.resample('4H').agg({'Open':'first', 'High':'max', 'Low':'min', 'Close':'last', 'Volume':'sum'}).dropna()
        return df_1d, df_4h, df_1h
    except:
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame()

@st.cache_data(ttl=3600)
def get_broker_summary(t):
    import datetime
    today = datetime.date.today().strftime('%Y-%m-%d')
    url = f"https://api.goapi.io/v1/stock/idx/{t}/broker_summary?date={today}"
    headers = {"X-API-KEY": API_KEY}
    buy = []
    total_vol = 0
    try:
        r = requests.get(url, headers=headers, timeout=5)
        if r.status_code == 200:
            res = r.json()
            if "data" in res and "buy" in res["data"]:
                buy = res['data']['buy']
                total_vol = sum([b.get('vol', 0) for b in buy])
                return buy, total_vol
    except:
        pass
        
    st.warning("⚠️ GoAPI failed or rate limited (30/day limit breached). Using simulated data for Follow The Giant demonstration.")
    brokers = [random.choice(FOREIGN_BROKERS), random.choice(RETAIL_DOMINANT), random.choice(LOCAL_BUMN_BROKERS), random.choice(FOREIGN_BROKERS), random.choice(FOREIGN_BROKERS)]
    total_vol = random.randint(500_000, 2_000_000)
    for b in set(brokers):
        buy.append({"broker": b, "netVal": random.randint(1_000_000_000, 10_000_000_000), "vol": random.randint(10_000, int(total_vol/2))})
    buy.sort(key=lambda x: x['vol'], reverse=True)
    return buy, total_vol

def extract_smc(df):
    if df.empty or len(df) < 10: return "Neutral", 0, 0, (0,0), 0, [], []
    highs = df['High'].values
    lows = df['Low'].values
    
    swing_highs, swing_lows = [], []
    window = 5
    for i in range(window, len(df)-window):
        if np.max(highs[i-window:i+window+1]) == highs[i]:
            swing_highs.append((df.index[i], highs[i]))
        if np.min(lows[i-window:i+window+1]) == lows[i]:
            swing_lows.append((df.index[i], lows[i]))
            
    bias = "Neutral"
    bos, choch = 0, 0
    poi_zone = (0, 0)
    idm = 0
    
    if len(swing_highs) >= 2 and len(swing_lows) >= 2:
        if swing_highs[-1][1] > swing_highs[-2][1]: 
            bias = "Bullish"
            bos = swing_highs[-2][1]
            choch = swing_lows[-2][1]
            poi_zone = (swing_lows[-1][1], swing_lows[-1][1] + (swing_highs[-1][1]-swing_lows[-1][1])*0.2)
            idm = swing_lows[-2][1] if len(swing_lows) > 1 else 0
        elif swing_highs[-1][1] < swing_highs[-2][1]: 
            bias = "Bearish"
            bos = swing_lows[-2][1]
            choch = swing_highs[-2][1]
            poi_zone = (swing_highs[-1][1] - (swing_highs[-1][1]-swing_lows[-1][1])*0.2, swing_highs[-1][1])
            idm = swing_highs[-2][1] if len(swing_highs) > 1 else 0

    return bias, bos, choch, poi_zone, idm, swing_highs, swing_lows

def plot_chart(df, title, smc_data):
    fig = go.Figure(data=[go.Candlestick(x=df.index, open=df['Open'], high=df['High'], low=df['Low'], close=df['Close'], name='Candlestick')])
    
    bias, bos, choch, poi, idm, sh, sl = smc_data
    if bos: fig.add_hline(y=bos, line_dash="dash", line_color="green", annotation_text=f"BOS ({bos:.1f})")
    if choch: fig.add_hline(y=choch, line_dash="dash", line_color="red", annotation_text=f"CHoCH ({choch:.1f})")
    if poi[1] > 0: fig.add_hrect(y0=poi[0], y1=poi[1], line_width=0, fillcolor="rgba(0,0,255,0.2)", annotation_text="POI Zone")
    
    fig.update_layout(title=title, xaxis_rangeslider_visible=False, height=400, template="plotly_dark", margin=dict(l=20, r=20, t=40, b=20))
    return fig

# --- Execution ---
ihsg_score, ihsg_status = fetch_ihsg()
col1, col2, col3 = st.columns(3)
col1.metric("IHSG Bull Tracker", f"{ihsg_score}%", status="normal")
col2.metric("IHSG Status", ihsg_status, "Condition")

df_1d, df_4h, df_1h = fetch_yf_data(ticker_yf)

if df_1d.empty:
    st.error(f"Cannot fetch data for {ticker}. Please check ticker.")
else:
    # 6. Trend validation & Volume
    ma20_1d = df_1d['Close'].rolling(20).mean().iloc[-1]
    avg_vol_10d = df_1d['Volume'].rolling(10).mean().iloc[-1]
    current_px = df_1d['Close'].iloc[-1]
    current_vol = df_1d['Volume'].iloc[-1]
    trend_valid = current_px > ma20_1d
    vol_valid = current_vol > (1.5 * avg_vol_10d)
    
    col3.metric("Last Price (1D)", f"Rp {current_px:,.0f}", f"MA20: {ma20_1d:,.0f}")
    
    # Analyze SMC
    smc_4h = extract_smc(df_4h)
    smc_1h = extract_smc(df_1h)
    
    # 3 & 4. Broker Summary Check
    st.markdown("### Follow The Giant Analysis (Accumulation Validation)")
    buy_list, tot_vol = get_broker_summary(ticker)
    
    if len(buy_list) >= 3:
        top_3 = buy_list[:3]
        top_b_names = [b['broker'] for b in top_3]
        top_3_vol = sum([b.get('vol', 0) for b in top_3])
        
        acc_score = top_3_vol / tot_vol if tot_vol > 0 else 0
        
        is_foreign = [b in FOREIGN_BROKERS for b in top_b_names]
        is_retail = [b in RETAIL_DOMINANT for b in top_b_names]
        is_bumn = [b in LOCAL_BUMN_BROKERS for b in top_b_names]
        
        message = ""
        st_color = "white"
        
        if sum(is_foreign) >= 2 and set(['AK', 'BK', 'KZ']).intersection(top_b_names):
            message = "High Probability Rally Detected: Top buyers are Tier 1 Foreign Funds (AK/BK/KZ)."
            st_color = "green"
        elif sum(is_retail) >= 2:
            message = "CAUTION (Noise Filter): Short-term bounce triggered by Retail/Fast Trade. Unstable."
            st_color = "red"
        elif sum(is_foreign) > 0 and sum(is_bumn) > 0:
            message = "STRONGEST SIGNAL: Broker Convergence! Foreign Flow & Local Institution buying together."
            st_color = "gold"
        else:
            message = "Standard Market Activity."
            
        cA, cB, cC = st.columns(3)
        cA.metric("Top 3 Buyers", ", ".join(top_b_names))
        cB.metric("Accumulation Score", f"{acc_score*100:.1f}%")
        
        if acc_score > 0.5: cC.success("🐋 Massive Accumulation (Whale is at work!)")
        elif acc_score > 0.3: cC.info("Accumulation Detected")
        else: cC.warning("Normal Activity")
        
        st.markdown(f"<h4 style='color:{st_color}'>{message}</h4>", unsafe_allow_html=True)
        
        # Valid trade check
        if sum(is_foreign) == 0:
            st.error("❌ No Trade: Top 3 buyers do not include foreign brokers.")
        else:
            st.success("✅ Trade Validation Passed: Foreign flow detected.")
            
    st.markdown("---")
    
    # 7. SMC Trade Output formatting
    st.markdown("### Smart Money Concept (SMC) Strategy Mapping - 1H/4H")
    
    t_bias, t_bos, t_choch, t_poi, t_idm, _, _ = smc_1h
    if t_bias != "Neutral":
        col_smc1, col_smc2 = st.columns(2)
        col_smc1.markdown(f"**Trend Bias:** {t_bias}")
        col_smc1.markdown(f"**Current Structure:** Last BOS at `{t_bos:,.0f}`, Last CHoCH at `{t_choch:,.0f}`")
        col_smc1.markdown(f"**POI Zone:** 1H Order Block range `{t_poi[0]:,.0f} - {t_poi[1]:,.0f}`")
        col_smc2.markdown(f"**Liquidity to Watch:** IDM nearest around `{t_idm:,.0f}`")
        
        rc_en = sum(t_poi)/2 if sum(t_poi)>0 else current_px
        rc_sl = t_poi[0]*0.98 if t_bias=="Bullish" else t_poi[1]*1.02
        rc_tp = current_px + ((rc_en - rc_sl) * 2) if t_bias=="Bullish" else current_px - ((rc_sl - rc_en) * 2)
        col_smc2.markdown(f"**Trade Plan:** Entry `{rc_en:,.0f}`, SL `{rc_sl:,.0f}`, TP `{rc_tp:,.0f}`")
        col_smc2.markdown(f"**Risk/Reward Ratio:** Minimum `1:2`")
        
    else:
        st.write("Not enough market structure on 1H to establish SMC setup.")
        
    st.markdown("---")
    
    # Plots
    st.plotly_chart(plot_chart(df_1d, f"{ticker} - 1 Day Chart", (None, 0, 0, (0,0), 0, [], [])), use_container_width=True)
    
    colP1, colP2 = st.columns(2)
    with colP1:
        st.plotly_chart(plot_chart(df_4h, f"{ticker} - 4 Hour Chart", smc_4h), use_container_width=True)
    with colP2:
        st.plotly_chart(plot_chart(df_1h, f"{ticker} - 1 Hour Chart", smc_1h), use_container_width=True)
