import requests
import os
import pandas as pd
import numpy as np
import logging
import traceback
import time
from datetime import datetime

# --- 0. 環境修正：設定台北時區 ---
os.environ['TZ'] = 'Asia/Taipei'
try:
    time.tzset() 
except:
    pass

# --- 1. 基礎配置 ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

TG_TOKEN = os.getenv("TG_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
LOG_FILE = "active_trades.csv"

# 監控清單
ALL_MONITOR = [
    "BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP", "BNB-USDT-SWAP",
    "SUI-USDT-SWAP", "AVAX-USDT-SWAP", "XRP-USDT-SWAP", "LINK-USDT-SWAP",
    "APT-USDT-SWAP", "OP-USDT-SWAP", "ARB-USDT-SWAP", "NEAR-USDT-SWAP",
    "PEPE-USDT-SWAP", "WIF-USDT-SWAP", "ORDI-USDT-SWAP", "TON-USDT-SWAP",
    "FET-USDT-SWAP", "TIA-USDT-SWAP", "PENDLE-USDT-SWAP", "RNDR-USDT-SWAP"
]

# --- 2. 數據獲取與分析 ---

def fetch_okx_kline(instId, bar='15m', limit='100'):
    try:
        url = f"https://www.okx.com/api/v5/market/candles?instId={instId}&bar={bar}&limit={limit}"
        res = requests.get(url, timeout=10).json()
        if 'data' not in res or not res['data']: return None
        df = pd.DataFrame(res['data'], columns=['ts','o','h','l','c','v','volCcy','volCcyQuote','confirm'])
        df[['o','h','l','c','v']] = df[['o','h','l','c','v']].astype(float)
        return df[df['confirm'] == "1"].iloc[::-1].reset_index(drop=True)
    except: return None

def get_market_metrics(instId):
    base_id = instId.replace("-SWAP", "")
    metrics = {"ls_ratio": 1.0, "funding": 0.0, "cvd_bias": "中性"}
    try:
        ls_res = requests.get(f"https://www.okx.com/api/v5/rubik/stat/contracts/long-short-account-ratio?instId={base_id}", timeout=5).json()
        if 'data' in ls_res: metrics['ls_ratio'] = float(ls_res['data'][0]['ratio'])
        fr_res = requests.get(f"https://www.okx.com/api/v5/public/funding-rate?instId={instId}", timeout=5).json()
        if 'data' in fr_res: metrics['funding'] = float(fr_res['data'][0]['fundingRate'])
        if metrics['ls_ratio'] < 0.95: metrics['cvd_bias'] = "🟢 大戶吸籌"
        elif metrics['ls_ratio'] > 1.2: metrics['cvd_bias'] = "🔴 散戶派發"
        return metrics
    except: return metrics

def calculate_atr(df):
    if len(df) < 15: return 0
    hl, hc, lc = df['h']-df['l'], np.abs(df['h']-df['c'].shift()), np.abs(df['l']-df['c'].shift())
    tr = np.max(pd.concat([hl, hc, lc], axis=1), axis=1)
    return tr.rolling(window=14).mean().iloc[-1]

# --- 3. SMC 策略核心 (含 TP123 計算) ---

def find_smc_setup(df):
    if df is None or len(df) < 40: return None
    atr = calculate_atr(df)
    vol_sma = df['v'].rolling(10).mean().iloc[-1]
    
    for i in range(len(df)-2, len(df)-12, -1):
        k0, k1, k2 = df.iloc[i-1], df.iloc[i], df.iloc[i+1]
        
        # 多頭 BOS
        if k2['c'] > df['h'].iloc[i-15:i].max() and k2['v'] > vol_sma:
            entry = (k2['l'] + k0['h']) / 2 if k2['l'] > k0['h'] else k1['o']
            sl = k1['l'] - (0.5 * atr)
            risk = abs(entry - sl)
            return {
                "side": "LONG", "entry": entry, "sl": sl,
                "tp1": entry + (risk * 1.5),
                "tp2": entry + (risk * 2.5),
                "tp3": entry + (risk * 4.0)
            }
        # 空頭 BOS
        if k2['c'] < df['l'].iloc[i-15:i].min() and k2['v'] > vol_sma:
            entry = (k2['h'] + k0['l']) / 2 if k2['h'] < k0['l'] else k1['o']
            sl = k1['h'] + (0.5 * atr)
            risk = abs(sl - entry)
            return {
                "side": "SHORT", "entry": entry, "sl": sl,
                "tp1": entry - (risk * 1.5),
                "tp2": entry - (risk * 2.5),
                "tp3": entry - (risk * 4.0)
            }
    return None

# --- 4. 發送與過濾 ---

def is_high_probability(setup, instId, df_1h):
    score = 0
    now = datetime.now()
    met = get_market_metrics(instId)
    if (15 <= now.hour <= 18) or (20 <= now.hour <= 23): score += 1
    ema50 = df_1h['c'].ewm(span=50).mean().iloc[-1]
    if (setup['side'] == 'LONG' and df_1h['c'].iloc[-1] > ema50) or \
       (setup['side'] == 'SHORT' and df_1h['c'].iloc[-1] < ema50): score += 1
    return score >= 1, score, met

def send_tg(msg):
    if not TG_TOKEN or not CHAT_ID: return
    try: requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"}, timeout=10)
    except: pass

def run_oracle():
    if not os.path.exists(LOG_FILE): pd.DataFrame(columns=["instId","side","status","entry","sl","tp1","tp2","tp3"]).to_csv(LOG_FILE, index=False)
    try: trades_df = pd.read_csv(LOG_FILE)
    except: trades_df = pd.DataFrame(columns=["instId","side","status","entry","sl","tp1","tp2","tp3"])
    
    active_ids = trades_df['instId'].tolist() if not trades_df.empty else []
    updated_trades = []

    for instId in ALL_MONITOR:
        df_1h = fetch_okx_kline(instId, bar='1H')
        if df_1h is None or len(df_1h) < 50: continue
        
        # 自動切換週期
        ema50 = df_1h['c'].ewm(span=50).mean().iloc[-1]
        regime_tf = '1H' if abs(df_1h['c'].iloc[-1] - ema50)/ema50 > 0.025 else '15m'
        df = fetch_okx_kline(instId, bar=regime_tf)
        if df is None: continue
        
        if instId not in active_ids:
            setup = find_smc_setup(df)
            if setup:
                is_good, score, met = is_high_probability(setup, instId, df_1h)
                if is_good:
                    side_label = "🟢 多單 (LONG)" if setup['side'] == "LONG" else "🔴 空單 (SHORT)"
                    msg = f"🚀 *Alpha Oracle 訊號* 🚀\n"
                    msg += f"──────────────────\n"
                    msg += f"🪙 幣種：#{instId.split('-')[0]}\n"
                    msg += f"📈 方向：{side_label} | {regime_tf}\n"
                    msg += f"──────────────────\n"
                    msg += f"📍 *進場位：{setup['entry']:.4f}*\n\n"
                    msg += f"💰 *止盈 TP1：{setup['tp1']:.4f}* (1.5R)\n"
                    msg += f"💰 *止盈 TP2：{setup['tp2']:.4f}* (2.5R)\n"
                    msg += f"💰 *止盈 TP3：{setup['tp3']:.4f}* (4.0R)\n\n"
                    msg += f"🚫 *止損位：{setup['sl']:.4f}*\n"
                    msg += f"──────────────────\n"
                    msg += f"📊 籌碼面：LS {met['ls_ratio']} | {met['cvd_bias']}"
                    send_tg(msg)
                    setup.update({"instId": instId, "status": "WAITING"})
                    updated_trades.append(setup)
        else:
            t = trades_df[trades_df['instId'] == instId].iloc[0].to_dict()
            curr_p = df['c'].iloc[-1]
            if t['status'] == "WAITING":
                if (t['side']=="LONG" and curr_p <= t['entry']) or (t['side']=="SHORT" and curr_p >= t['entry']):
                    t['status'] = "ACTIVE"
                    send_tg(f"🔔 *成交*：#{instId.split('-')[0]} 已進入進場區域")
                updated_trades.append(t)
            elif t['status'] == "ACTIVE":
                is_sl = (curr_p <= t['sl'] if t['side']=="LONG" else curr_p >= t['sl'])
                is_tp3 = (curr_p >= t['tp3'] if t['side']=="LONG" else curr_p <= t['tp3'])
                if is_sl or is_tp3:
                    send_tg(f"🏁 *結算*：#{instId.split('-')[0]} 已完成交易")
                else: updated_trades.append(t)

    pd.DataFrame(updated_trades).to_csv(LOG_FILE, index=False)

if __name__ == "__main__":
    run_oracle()
