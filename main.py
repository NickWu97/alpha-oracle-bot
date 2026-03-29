import requests
import os
import pandas as pd
import numpy as np
import logging
from datetime import datetime, timedelta

# 1. 系統日誌與環境變數
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
TG_TOKEN = os.getenv("TG_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

# 2. 監控配置
COINS = ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP", "SUI-USDT-SWAP", "XRP-USDT-SWAP", "ASI-USDT-SWAP"]
LOG_FILE = "active_trades.csv"      # 當前持倉
HISTORY_FILE = "trade_history.csv" # 歷史戰績

def send_tg(msg):
    """安全發送訊息至 Telegram"""
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    try:
        res = requests.post(url, json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"}, timeout=15)
        res.raise_for_status()
    except Exception as e:
        logging.error(f"TG 發送失敗: {e}")

def fetch_okx(instId, bar="15m", limit="300"):
    """抓取 K 線並確保數據完整性"""
    try:
        url = f"https://www.okx.com/api/v5/market/candles?instId={instId}&bar={bar}&limit={limit}"
        res = requests.get(url, timeout=10).json()
        if 'data' not in res or not res['data']: return None
        df = pd.DataFrame(res['data'], columns=['ts', 'o', 'h', 'l', 'c', 'v', 'volCcy', 'volCcyQuote', 'confirm'])
        df[['o', 'h', 'l', 'c', 'v']] = df[['o', 'h', 'l', 'c', 'v']].astype(float)
        # 僅取已收盤的 K 棒 (confirm == "1")
        df = df[df['confirm'] == "1"].copy()
        return df.iloc[::-1].reset_index(drop=True)
    except Exception as e:
        logging.error(f"API 抓取錯誤 ({instId}): {e}")
        return None

def get_sentiment(instId):
    """偵測籌碼面：LS Ratio、CVD 與 OI 燃料"""
    try:
        # A. 多空人數比 (LS Ratio)
        ls_res = requests.get(f"https://www.okx.com/api/v5/rubik/stat/contracts/long-short-account-ratio?instId={instId}&period=5m").json()
        ls_data = ls_res.get('data', [])
        if not ls_data: return 1.0, 1.0, False, False
        ls_curr, ls_prev = float(ls_data[0][1]), float(ls_data[2][1])

        # B. CVD 趨勢 (使用 5m 現貨數據判斷)
        base = instId.split('-')[0]
        s_df = fetch_okx(f"{base}-USDT", bar="5m", limit="20")
        cvd_up = s_df['c'].iloc[-1] > s_df['c'].iloc[-10] if s_df is not None and len(s_df) > 10 else False

        # C. 持倉量燃料 (OI Fuel)
        oi_res = requests.get(f"https://www.okx.com/api/v5/rubik/stat/contracts/open-interest-volume?instId={instId}&period=5m").json()
        oi_data = oi_res.get('data', [])
        # 燃料定義：價格噴發但 OI 下降 (空頭爆倉)
        fuel = float(oi_data[0][1]) < float(oi_data[2][1]) if len(oi_data) > 2 else False
        
        return ls_curr, ls_prev, cvd_up, fuel
    except:
        return 1.0, 1.0, False, False

def main():
    now_tw = datetime.utcnow() + timedelta(hours=8)
    logging.info(f"--- 啟動正式版掃描: {now_tw.strftime('%Y-%m-%d %H:%M:%S')} ---")
    
    # 初始化/讀取檔案
    if not os.path.exists(LOG_FILE):
        pd.DataFrame(columns=["instId", "side", "entry", "sl", "tp1", "tp3", "tp1_hit"]).to_csv(LOG_FILE, index=False)
    
    trades = pd.read_csv(LOG_FILE).to_dict('records')
    still_active, finished = [], []

    # --- 流程 1：持倉監控與自動保本 ---
    for t in trades:
        df = fetch_okx(t['instId'], "15m", "10")
        if df is None or df.empty:
            still_active.append(t); continue
        
        curr_p, hi, lo = df['c'].iloc[-1], df['h'].max(), df['l'].min()
        side = t['side']

        # 止損判定
        if (side == "LONG" and lo <= t['sl']) or (side == "SHORT" and hi >= t['sl']):
            send_tg(f"❌ *結算：止損離場 (SL)*\n💰 #{t['instId']} | 離場價: `{curr_p}`")
            t['status'] = "LOSS"; finished.append(t); continue

        # TP1 達成 -> 自動將止損移至進場位 (保本)
        if t.get('tp1_hit', 0) == 0:
            if (side == "LONG" and hi >= t['tp1']) or (side == "SHORT" and lo <= t['tp1']):
                t['tp1_hit'] = 1; t['sl'] = t['entry'] 
                send_tg(f"🔹 *TP1 達成：已自動保本*\n💰 #{t['instId']} | 止損已鎖定在進場價: `{t['sl']}`")
        
        # TP3 達成 -> 最終獲利了結
        if (side == "LONG" and hi >= t['tp3']) or (side == "SHORT" and lo <= t['tp3']):
            send_tg(f"🚀 *TP3 終極止盈！*\n💰 #{t['instId']} | 燃料行情完美收割")
            t['status'] = "WIN"; finished.append(t)
        else:
            still_active.append(t)

    # --- 流程 2：掃描新訊號 (嚴格過濾) ---
    current_ids = [x['instId'] for x in still_active]
    for instId in COINS:
        if instId in current_ids: continue
        
        # A. 4H 長線趨勢過濾
        df_4h = fetch_okx(instId, "4H", "300")
        if df_4h is None or len(df_4h) < 200: continue
        ema200 = df_4h['c'].ewm(span=200, adjust=False).mean().iloc[-1]
        
        # B. 籌碼面過濾 (LS/CVD/Fuel)
        ls_c, ls_p, cvd_up, fuel = get_sentiment(instId)
        
        # C. 15m 價格結構過濾
        df_15 = fetch_okx(instId, "15m", "100")
        if df_15 is None or len(df_15) < 30: continue
        curr_p = df_15['c'].iloc[-1]
        atr = (df_15['h'] - df_15['l']).rolling(14).mean().iloc[-1]
        h_max, l_min = df_15['h'].iloc[-20:-2].max(), df_15['l'].iloc[-20:-2].min()

        # D. 最終策略判定
        # 做多：在 EMA200 之上 + 突破前高 + CVD 向上 + 散戶人數撤退 + 燃料點燃
        long_cond = (curr_p > ema200) and (curr_p > h_max) and cvd_up and (ls_c < ls_p) and fuel
        # 做空：在 EMA200 之下 + 突破前低 + CVD 向下 + 散戶人數追空 + 燃料點燃
        short_cond = (curr_p < ema200) and (curr_p < l_min) and (not cvd_up) and (ls_c > ls_p) and fuel

        if long_cond or short_cond:
            side = "LONG" if long_cond else "SHORT"
            # 點位計算：ATR 1.5倍止損，1倍利潤 TP1，4倍利潤 TP3
            sl = curr_p - (atr * 1.5) if long_cond else curr_p + (atr * 1.5)
            tp1 = curr_p + atr if long_cond else curr_p - atr
            tp3 = curr_p + (atr * 4) if long_cond else curr_p - (atr * 4)
            
            new_trade = {"instId": instId, "side": side, "entry": curr_p, "sl": sl, "tp1": tp1, "tp3": tp3, "tp1_hit": 0}
            still_active.append(new_trade)
            
            send_tg(f"🎯 *Alpha 燃料狙擊：新訊號*\n"
                    f"💎 幣種：#{instId.split('-')[0]} | {side}\n"
                    f"📍 進場位：`{curr_p:.4f}`\n"
                    f"🚫 止損位：`{sl:.4f}`\n"
                    f"🟣 TP3 目標：`{tp3:.4f}`\n"
                    f"⛽ 燃料狀態：`🔥 點燃` | 📊 CVD：`{'🟢' if cvd_up else '🔴'}`")

    # --- 流程 3：紀錄存檔 ---
    pd.DataFrame(still_active).to_csv(LOG_FILE, index=False)
    if finished:
        pd.DataFrame(finished).to_csv(HISTORY_FILE, mode='a', header=not os.path.exists(HISTORY_FILE), index=False)
    logging.info(f"掃描結束，當前持倉數: {len(still_active)}")

if __name__ == "__main__":
    main()
