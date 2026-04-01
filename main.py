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

# --- 1. 基礎配置與 30 幣種清單 ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

TG_TOKEN = os.getenv("TG_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
LOG_FILE = "active_trades.csv"

# 監控清單
KING_COINS = ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP", "BNB-USDT-SWAP"]
MAJOR_ALTS = ["SUI-USDT-SWAP", "AVAX-USDT-SWAP", "XRP-USDT-SWAP", "LINK-USDT-SWAP", "APT-USDT-SWAP", "OP-USDT-SWAP", "ARB-USDT-SWAP", "NEAR-USDT-SWAP"]
HOT_ALTS = ["PEPE-USDT-SWAP", "WIF-USDT-SWAP", "ORDI-USDT-SWAP", "TON-USDT-SWAP", "FET-USDT-SWAP", "TIA-USDT-SWAP", "PENDLE-USDT-SWAP", "RNDR-USDT-SWAP"]
ALL_MONITOR = KING_COINS + MAJOR_ALTS + HOT_ALTS

# --- 2. 數據獲取與分析函數 ---

def fetch_okx_kline(instId, bar='15m', limit='100'):
    try:
        url = f"https://www.okx.com/api/v5/market/candles?instId={instId}&bar={bar}&limit={limit}"
        res = requests.get(url, timeout=10).json()
        if 'data' not in res or not res['data']: return None
        df = pd.DataFrame(res['data'], columns=['ts','o','h','l','c','v','volCcy','volCcyQuote','confirm'])
        df[['o','h','l','c','v']] = df[['o','h','l','c','v']].astype(float)
        return df[df['confirm'] == "1"].iloc[::-1].reset_index(drop=True)
    except:
        return None

def get_market_metrics(instId):
    base_id = instId.replace("-SWAP", "")
    metrics = {"ls_ratio": 1.0, "funding": 0.0, "cvd_bias": "中性"}
    try:
        ls_res = requests.get(f"https://www.okx.com/api/v5/rubik/stat/contracts/long-short-account-ratio?instId={base_id}", timeout=5).json()
        if 'data' in ls_res and ls_res['data']:
            metrics['ls_ratio'] = float(ls_res['data'][0]['ratio'])
        fr_res = requests.get(f"https://www.okx.com/api/v5/public/funding-rate?instId={instId}", timeout=5).json()
        if 'data' in fr_res and fr_res['data']:
            metrics['funding'] = float(fr_res['data'][0]['fundingRate'])
        if metrics['ls_ratio'] < 0.95: metrics['cvd_bias'] = "🟢 大戶吸籌 (CVD+)"
        elif metrics['ls_ratio'] > 1.20: metrics['cvd_bias'] = "🔴 散戶派發 (CVD-)"
        return metrics
    except:
        return metrics

def calculate_atr(df):
    if len(df) < 15: return 0
    hl, hc, lc = df['h']-df['l'], np.abs(df['h']-df['c'].shift()), np.abs(df['l']-df['c'].shift())
    tr = np.max(pd.concat([hl, hc, lc], axis=1), axis=1)
    return tr.rolling(window=14).mean().iloc[-1]

# --- 3. SMC 策略核心 ---

def find_smc_setup(df, regime_tf):
    if df is None or len(df) < 40: return None
    atr = calculate_atr(df)
    # 趨勢模式給予更高盈虧比要求
    min_r = 2.5 if regime_tf == '1H' else 1.8
    vol_sma = df['v'].rolling(10).mean().iloc[-1]
    
    for i in range(len(df)-2, len(df)-12, -1):
        k0, k1, k2 = df.iloc[i-1], df.iloc[i], df.iloc[i+1]
        
        # 多頭 BOS
        if k2['c'] > df['h'].iloc[i-15:i].max() and k2['v'] > vol_sma:
            entry = (k2['l'] + k0['h']) / 2 if k2['l'] > k0['h'] else k1['o']
            sl = k1['l'] - (0.5 * atr)
            tp = entry + (abs(entry - sl) * min_r)
            return {"side": "LONG", "entry": entry, "sl": sl, "tp": tp, "r_ratio": round(min_r, 2)}
            
        # 空頭 BOS
        if k2['c'] < df['l'].iloc[i-15:i].min() and k2['v'] > vol_sma:
            entry = (k2['h'] + k0['l']) / 2 if k2['h'] < k0['l'] else k1['o']
            sl = k1['h'] + (0.5 * atr)
            tp = entry - (abs(sl - entry) * min_r)
            return {"side": "SHORT", "entry": entry, "sl": sl, "tp": tp, "r_ratio": round(min_r, 2)}
    return None

# --- 4. 發送與過濾邏輯 ---

def is_high_probability(setup, instId, df_1h):
    score = 0
    now = datetime.now()
    met = get_market_metrics(instId)
    if (15 <= now.hour <= 18) or (20 <= now.hour <= 23): score += 1
    ema50 = df_1h['c'].ewm(span=50).mean().iloc[-1]
    if setup['side'] == 'LONG':
        if met['ls_ratio'] < 1.05 and met['funding'] < 0.0003: score += 1
        if df_1h['c'].iloc[-1] > ema50: score += 1
    else:
        if met['ls_ratio'] > 1.10 and met['funding'] > -0.0001: score += 1
        if df_1h['c'].iloc[-1] < ema50: score += 1
    return score >= 2, score, met

def send_tg(msg):
    if not TG_TOKEN or not CHAT_ID: return
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", 
                      json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"}, timeout=10)
    except:
        pass

# --- 5. 主執行邏輯 ---

def run_oracle():
    cols = ["instId","side","status","entry","sl","tp","r_ratio","locked"]
    if not os.path.exists(LOG_FILE):
        pd.DataFrame(columns=cols).to_csv(LOG_FILE, index=False)
    
    try:
        trades_df = pd.read_csv(LOG_FILE)
    except:
        trades_df = pd.DataFrame(columns=cols)
    
    active_ids = trades_df['instId'].tolist() if not trades_df.empty else []
    updated_trades = []

    for instId in ALL_MONITOR:
        df_1h = fetch_okx_kline(instId, bar='1H')
        if df_1h is None or len(df_1h) < 50: continue
        
        ema50 = df_1h['c'].ewm(span=50).mean().iloc[-1]
        bias = abs(df_1h['c'].iloc[-1] - ema50) / ema50
        regime_tf = '1H' if bias > 0.025 else '15m'
        df = fetch_okx_kline(instId, bar=regime_tf)
        if df is None: continue
        
        # 新訊號偵測
        if instId not in active_ids:
            setup = find_smc_setup(df, regime_tf)
            if setup:
                is_good, score, met = is_high_probability(setup, instId, df_1h)
                if is_good:
                    side_emoji = "🟢 多單 (LONG)" if setup['side'] == "LONG" else "🔴 空單 (SHORT)"
                    msg = f"🔥 *Alpha Oracle 訊號發射* 🔥\n"
                    msg += f"──────────────────\n"
                    msg += f"🪙 幣種：#{instId.split('-')[0]}\n"
                    msg += f"📈 方向：{side_emoji}\n"
                    msg += f"⏰ 週期：{regime_tf}\n"
                    msg += f"──────────────────\n"
                    msg += f"📍 *進場位：{setup['entry']:.4f}*\n"
                    msg += f"💰 *止盈點：{setup['tp']:.4f}*\n"
                    msg += f"🚫 *止損點：{setup['sl']:.4f}*\n"
                    msg += f"⚖️ 盈虧比：{setup['r_ratio']}R\n"
                    msg += f"──────────────────\n"
                    msg += f"📊 數據面過濾：\n"
                    msg += f"├ 多空比：{met['ls_ratio']}\n"
                    msg += f"├ 資費：{met['funding']:.4%}\n"
                    msg += f"└ CVD：{met['cvd_bias']}"
                    send_tg(msg)
                    setup.update({"instId": instId, "status": "WAITING", "locked": 0})
                    updated_trades.append(setup)
        else:
            # 持倉更新邏輯 (成交、結算)
            t = trades_df[trades_df['instId'] == instId].iloc[0].to_dict()
            curr_p = df['c'].iloc[-1]
            if t['status'] == "WAITING":
                if (t['side']=="LONG" and curr_p <= t['entry']) or (t['side']=="SHORT" and curr_p >= t['entry']):
                    t['status'] = "ACTIVE"
                    send_tg(f"🔔 *成交通知*：#{instId.split('-')[0]} 已進入進場範圍！")
                updated_trades.append(t)
            elif t['status'] == "ACTIVE":
                is_sl = (curr_p <= t['sl'] if t['side']=="LONG" else curr_p >= t['sl'])
                is_tp = (curr_p >= t['tp'] if t['side']=="LONG" else curr_p <= t['tp'])
                if is_sl or is_tp:
                    res = "💰 獲利平倉 (TP)" if is_tp else "❌ 止損離場 (SL)"
                    send_tg(f"🏁 *結算通知*：#{instId.split('-')[0]}\n結果：{res}")
                else:
                    updated_trades.append(t)

    pd.DataFrame(updated_trades).to_csv(LOG_FILE, index=False)

if __name__ == "__main__":
    run_oracle()
