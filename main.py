import requests
import os
import pandas as pd
import numpy as np
import logging
from datetime import datetime, timedelta

# 1. 系統配置
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
TG_TOKEN = os.getenv("TG_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
COINS = ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP", "SUI-USDT-SWAP", "XRP-USDT-SWAP", "ASI-USDT-SWAP"]
LOG_FILE = "active_trades.csv"
HISTORY_FILE = "trade_history.csv"

def send_tg(msg):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"}, timeout=15)
    except: pass

def fetch_okx(instId, bar="15m", limit="300"):
    try:
        url = f"https://www.okx.com/api/v5/market/candles?instId={instId}&bar={bar}&limit={limit}"
        res = requests.get(url, timeout=10).json()
        if 'data' not in res or not res['data']: return None
        df = pd.DataFrame(res['data'], columns=['ts', 'o', 'h', 'l', 'c', 'v', 'volCcy', 'volCcyQuote', 'confirm'])
        df[['o', 'h', 'l', 'c', 'v']] = df[['o', 'h', 'l', 'c', 'v']].astype(float)
        df = df[df['confirm'] == "1"].copy()
        return df.iloc[::-1].reset_index(drop=True)
    except: return None

def get_sentiment(instId):
    try:
        # LS Ratio
        ls_res = requests.get(f"https://www.okx.com/api/v5/rubik/stat/contracts/long-short-account-ratio?instId={instId}&period=5m").json()
        ls_c, ls_p = float(ls_res['data'][0][1]), float(ls_res['data'][2][1])
        # CVD Trend (5m)
        base = instId.split('-')[0]
        s_df = fetch_okx(f"{base}-USDT", bar="5m", limit="20")
        cvd_up = s_df['c'].iloc[-1] > s_df['c'].iloc[-10] if s_df is not None else False
        # OI Fuel
        oi_res = requests.get(f"https://www.okx.com/api/v5/rubik/stat/contracts/open-interest-volume?instId={instId}&period=5m").json()
        fuel = float(oi_res['data'][0][1]) < float(oi_res['data'][2][1]) if len(oi_res.get('data', [])) > 2 else False
        return ls_c, ls_p, cvd_up, fuel
    except: return 1.0, 1.0, False, False

def main():
    now = datetime.utcnow() + timedelta(hours=8)
    if not os.path.exists(LOG_FILE):
        pd.DataFrame(columns=["instId", "side", "entry", "sl", "tp1", "tp3", "tp1_hit"]).to_csv(LOG_FILE, index=False)
    
    trades = pd.read_csv(LOG_FILE).to_dict('records')
    still_active, history = [], []

    # A. 監控與自動保本
    for t in trades:
        df = fetch_okx(t['instId'], "15m", "10")
        if df is None: still_active.append(t); continue
        curr_p, hi, lo = df['c'].iloc[-1], df['h'].max(), df['l'].min()
        
        # 止損
        if (t['side'] == "LONG" and lo <= t['sl']) or (t['side'] == "SHORT" and hi >= t['sl']):
            send_tg(f"❌ *結算：止損離場*\n💰 #{t['instId']} | 價格: `{curr_p}`")
            t['exit_p'] = curr_p; history.append(t); continue
        # TP1 保本
        if t.get('tp1_hit') == 0:
            if (t['side'] == "LONG" and hi >= t['tp1']) or (t['side'] == "SHORT" and lo <= t['tp1']):
                t['tp1_hit'] = 1; t['sl'] = t['entry']
                send_tg(f"🔹 *TP1 達成：已自動保本*\n💰 #{t['instId']} | 止損移至: `{t['sl']}`")
        # TP3 止盈
        if (t['side'] == "LONG" and hi >= t['tp3']) or (t['side'] == "SHORT" and lo <= t['tp3']):
            send_tg(f"🚀 *TP3 終極止盈！*\n💰 #{t['instId']} | 獲利了結"); history.append(t)
        else:
            still_active.append(t)

    # B. 掃描新訊號
    current_ids = [x['instId'] for x in still_active]
    for instId in COINS:
        if instId in current_ids: continue
        df_4h = fetch_okx(instId, "4H", "300")
        if df_4h is None or len(df_4h) < 200: continue
        
        ema200 = df_4h['c'].ewm(span=200, adjust=False).mean().iloc[-1]
        ls_c, ls_p, cvd_up, fuel = get_sentiment(instId)
        df_15 = fetch_okx(instId, "15m", "50")
        if df_15 is None: continue
        
        curr_p = df_15['c'].iloc[-1]
        atr = (df_15['h'] - df_15['l']).rolling(14).mean().iloc[-1]
        h_max, l_min = df_15['h'].iloc[-20:-2].max(), df_15['l'].iloc[-20:-2].min()

        # 核心策略觸發
        long_cond = (curr_p > ema200) and (curr_p > h_max) and cvd_up and (ls_c < ls_p) and fuel
        short_cond = (curr_p < ema200) and (curr_p < l_min) and (not cvd_up) and (ls_c > ls_p) and fuel

        if long_cond or short_cond:
            side = "LONG" if long_cond else "SHORT"
            sl = curr_p - (atr * 1.5) if long_cond else curr_p + (atr * 1.5)
            tp1, tp3 = curr_p + atr if long_cond else curr_p - atr, curr_p + atr*4 if long_cond else curr_p - atr*4
            
            send_tg(f"🎯 *Alpha 燃料狙擊*\n💎 #{instId.split('-')[0]} | {side}\n📍 進場: `{curr_p}`\n🚫 止損: `{sl:.4f}`\n🟣 TP3: `{tp3:.4f}`\n⛽ 燃料: `🔥 噴發中`")
            still_active.append({"instId": instId, "side": side, "entry": curr_p, "sl": sl, "tp1": tp1, "tp3": tp3, "tp1_hit": 0})

    pd.DataFrame(still_active).to_csv(LOG_FILE, index=False)
    if history: pd.DataFrame(history).to_csv(HISTORY_FILE, mode='a', header=not os.path.exists(HISTORY_FILE), index=False)

if __name__ == "__main__":
    main()
