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

def fetch_okx_pro(instId, bar="15m", limit="300"): # 提高 limit 確保夠算 EMA200
    try:
        url = f"https://www.okx.com/api/v5/market/candles?instId={instId}&bar={bar}&limit={limit}"
        res = requests.get(url, timeout=10).json()
        if 'data' not in res or not res['data']:
            return None
        df = pd.DataFrame(res['data'], columns=['ts', 'o', 'h', 'l', 'c', 'v', 'volCcy', 'volCcyQuote', 'confirm'])
        df[['o', 'h', 'l', 'c', 'v', 'volCcy']] = df[['o', 'h', 'l', 'c', 'v', 'volCcy']].astype(float)
        # 過濾未確認 K 棒
        df = df[df['confirm'] == "1"].copy()
        return df.iloc[::-1].reset_index(drop=True)
    except Exception as e:
        logging.error(f"Fetch Error {instId}: {e}")
        return None

def get_sentiment_pro(instId):
    try:
        ls_res = requests.get(f"https://www.okx.com/api/v5/rubik/stat/contracts/long-short-account-ratio?instId={instId}&period=5m").json()
        ls_data = ls_res.get('data', [])
        if not ls_data: return 1.0, 1.0, "NEU", False
        ls_curr, ls_prev = float(ls_data[0][1]), float(ls_data[2][1])
        
        base = instId.split('-')[0]
        s_df = fetch_okx_pro(f"{base}-USDT", bar="5m", limit="20")
        cvd_trend = "UP" if (s_df is not None and not s_df.empty and s_df['c'].iloc[-1] > s_df['c'].iloc[-10]) else "DOWN"
        
        oi_res = requests.get(f"https://www.okx.com/api/v5/rubik/stat/contracts/open-interest-volume?instId={instId}&period=5m").json()
        oi_data = oi_res.get('data', [])
        fuel = float(oi_data[0][1]) < float(oi_data[2][1]) if len(oi_data) > 2 else False
        return ls_curr, ls_prev, cvd_trend, fuel
    except:
        return 1.0, 1.0, "NEU", False

def main():
    now_tw = datetime.utcnow() + timedelta(hours=8)
    logging.info(f"--- 啟動掃描: {now_tw.strftime('%Y-%m-%d %H:%M:%S')} ---")
    
    if os.path.exists(LOG_FILE):
        try: trades = pd.read_csv(LOG_FILE).to_dict('records')
        except: trades = []
    else: trades = []
    
    still_active, history = [], []

    # 1. 監控持倉
    for t in trades:
        df = fetch_okx_pro(t['instId'], "15m", "10")
        if df is None or df.empty:
            still_active.append(t); continue
        
        curr_p = df['c'].iloc[-1]
        hi, lo = df['h'].max(), df['l'].min()
        
        if (t['side'] == "LONG" and lo <= t['sl']) or (t['side'] == "SHORT" and hi >= t['sl']):
            send_tg(f"❌ *結算：止損離場*\n💰 #{t['instId']} | 價格: `{curr_p}`")
            t['status'] = "LOSS"; history.append(t); continue

        if t.get('tp1_hit', 0) == 0:
            if (t['side'] == "LONG" and hi >= t['tp1']) or (t['side'] == "SHORT" and lo <= t['tp1']):
                t['tp1_hit'] = 1; t['sl'] = t['entry'] 
                send_tg(f"🔹 *TP1 達成：自動保本*\n💰 #{t['instId']} | 止損移至: `{t['sl']}`")
        
        if (t['side'] == "LONG" and hi >= t['tp3']) or (t['side'] == "SHORT" and lo <= t['tp3']):
            send_tg(f"🚀 *TP3 終極止盈！*\n💰 #{t['instId']} | 完美結算")
            t['status'] = "WIN"; history.append(t)
        else:
            still_active.append(t)

    # 2. 掃描新訊號
    current_ids = [x['instId'] for x in still_active]
    for instId in COINS:
        if instId in current_ids: continue
        
        # 關鍵修正：增加數據長度判斷
        df_4h = fetch_okx_pro(instId, "4H", "300")
        if df_4h is None or len(df_4h) < 200:
            logging.warning(f"⚠️ {instId} 數據不足 (只有 {len(df_4h) if df_4h is not None else 0} 根)，跳過。")
            continue
        
        ema200 = df_4h['c'].ewm(span=200, adjust=False).mean().iloc[-1]
        ls_c, ls_p, cvd, fuel = get_sentiment_pro(instId)
        
        df_15 = fetch_okx_pro(instId, "15m", "100")
        if df_15 is None or len(df_15) < 20: continue
        
        curr_p = df_15['c'].iloc[-1]
        atr = (df_15['h'] - df_15['l']).rolling(14).mean().iloc[-1]
        
        # 正式版嚴格過濾
        h_max = df_15['h'].iloc[-20:-2].max()
        l_min = df_15['l'].iloc[-20:-2].min()

        is_long = (curr_p > ema200) and (curr_p > h_max) and (cvd == "UP") and (ls_c < ls_p) and fuel
        is_short = (curr_p < ema200) and (curr_p < l_min) and (cvd == "DOWN") and (ls_c > ls_p) and fuel

        if is_long or is_short:
            side = "LONG" if is_long else "SHORT"
            sl = curr_p - (atr * 1.5) if is_long else curr_p + (atr * 1.5)
            tp1, tp3 = curr_p + atr, curr_p + atr * 4
            new_trade = {"instId": instId, "side": side, "entry": curr_p, "sl": sl, "tp1": tp1, "tp3": tp3, "tp1_hit": 0}
            still_active.append(new_trade)
            send_tg(f"🎯 *Alpha 訊號：新單入場*\n💎 #{instId} | {side}\n📍 進場: `{curr_p}`\n🚫 止損: `{sl}`")

    # 3. 儲存
    pd.DataFrame(still_active).to_csv(LOG_FILE, index=False)
    if history:
        pd.DataFrame(history).to_csv(HISTORY_FILE, mode='a', header=not os.path.exists(HISTORY_FILE), index=False)
    logging.info("--- 掃描結束 ---")

if __name__ == "__main__":
    main()
