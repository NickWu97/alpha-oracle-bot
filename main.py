import requests
import os
import pandas as pd
import numpy as np
import logging
from datetime import datetime, timedelta

# 配置日誌
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
    except Exception as e:
        logging.error(f"TG Error: {e}")

def fetch_okx_pro(instId, bar="15m", limit="100"):
    """只抓取已確認的 K 棒，防止插針誤判"""
    try:
        url = f"https://www.okx.com/api/v5/market/candles?instId={instId}&bar={bar}&limit={limit}"
        res = requests.get(url, timeout=10).json()
        df = pd.DataFrame(res['data'], columns=['ts', 'o', 'h', 'l', 'c', 'v', 'volCcy', 'volCcyQuote', 'confirm'])
        df[['o', 'h', 'l', 'c', 'v', 'volCcy']] = df[['o', 'h', 'l', 'c', 'v', 'volCcy']].astype(float)
        df = df[df['confirm'] == "1"].copy()
        return df.iloc[::-1].reset_index(drop=True)
    except: return None

def get_sentiment_pro(instId):
    """燃料與籌碼核心邏輯"""
    try:
        # 1. LS Ratio (人數比)
        ls_res = requests.get(f"https://www.okx.com/api/v5/rubik/stat/contracts/long-short-account-ratio?instId={instId}&period=5m").json()
        ls_data = ls_res.get('data', [])
        ls_curr, ls_prev = float(ls_data[0][1]), float(ls_data[2][1])
        # 2. CVD (現貨趨勢) 與 流動性過濾
        base = instId.split('-')[0]
        s_df = fetch_okx_pro(f"{base}-USDT", bar="5m", limit="20")
        cvd_trend = "UP" if s_df['c'].iloc[-1] > s_df['c'].iloc[-10] else "DOWN"
        # 3. OI Fuel (燃料)
        oi_res = requests.get(f"https://www.okx.com/api/v5/rubik/stat/contracts/open-interest-volume?instId={instId}&period=5m").json()
        oi_data = oi_res.get('data', [])
        fuel = float(oi_data[0][1]) < float(oi_data[2][1]) if len(oi_data) > 2 else False
        return ls_curr, ls_prev, cvd_trend, fuel
    except: return 1.0, 1.0, "NEU", False

def main():
    now_tw = datetime.utcnow() + timedelta(hours=8)
    trades = pd.read_csv(LOG_FILE).to_dict('records') if os.path.exists(LOG_FILE) else []
    still_active, history = [], []

    # --- 流程 A：監控現有持倉 (包含自動保本邏輯) ---
    for t in trades:
        df = fetch_okx_pro(t['instId'], "15m", "5")
        if df is None or df.empty: still_active.append(t); continue
        
        curr_p = df['c'].iloc[-1]
        hi, lo = df['h'].max(), df['l'].min()
        
        # 止損判定
        if (t['side'] == "LONG" and lo <= t['sl']) or (t['side'] == "SHORT" and hi >= t['sl']):
            send_tg(f"❌ *結算：止損離場*\n💰 #{t['instId']} | 價格: `{curr_p}`"); history.append(t); continue

        # TP1 達成 -> 自動保本
        if t.get('tp1_hit', 0) == 0:
            if (t['side'] == "LONG" and hi >= t['tp1']) or (t['side'] == "SHORT" and lo <= t['tp1']):
                t['tp1_hit'] = 1; t['sl'] = t['entry'] 
                send_tg(f"🔹 *TP1 達成：已自動保本*\n💰 #{t['instId']} | 止損移至: `{t['sl']}`")
        
        # TP3 達成 -> 獲利了結
        if (t['side'] == "LONG" and hi >= t['tp3']) or (t['side'] == "SHORT" and lo <= t['tp3']):
            send_tg(f"🚀 *TP3 終極止盈！*\n💰 #{t['instId']} | 完美結算"); history.append(t)
        else:
            still_active.append(t)

    # --- 流程 B：掃描新訊號 (正式版嚴格邏輯) ---
    current_ids = [x['instId'] for x in still_active]
    for instId in COINS:
        if instId in current_ids: continue
        
        df_4h = fetch_okx_pro(instId, "4H", "200")
        if df_4h is None: continue
        ema200 = df_4h['c'].ewm(span=200, adjust=False).mean().iloc[-1]
        
        ls_c, ls_p, cvd, fuel = get_sentiment_pro(instId)
        df_15 = fetch_okx_pro(instId, "15m", "50")
        curr_p = df_15['c'].iloc[-1]
        atr = (df_15['h'] - df_15['l']).rolling(14).mean().iloc[-1]

        # 正式版四重過濾
        is_long = (curr_p > ema200) and (curr_p > df_15['h'].iloc[-20:-2].max()) and (cvd == "UP") and (ls_c < ls_p) and fuel
        is_short = (curr_p < ema200) and (curr_p < df_15['l'].iloc[-20:-2].min()) and (cvd == "DOWN") and (ls_c > ls_p) and fuel

        if is_long or is_short:
            side = "LONG" if is_long else "SHORT"
            sl = curr_p - (atr * 1.5) if is_long else curr_p + (atr * 1.5)
            tp1, tp3 = curr_p + atr, curr_p + atr * 4
            new_trade = {"instId": instId, "side": side, "entry": curr_p, "sl": sl, "tp1": tp1, "tp3": tp3, "tp1_hit": 0}
            still_active.append(new_trade)
            send_tg(f"🎯 *正式版訊號：新單入場*\n💎 #{instId} | {side}\n📍 進場: `{curr_p}`\n🚫 止損: `{sl}`")

    # 儲存
    pd.DataFrame(still_active).to_csv(LOG_FILE, index=False)
    if history: pd.DataFrame(history).to_csv(HISTORY_FILE, mode='a', header=not os.path.exists(HISTORY_FILE), index=False)

if __name__ == "__main__":
    main()
