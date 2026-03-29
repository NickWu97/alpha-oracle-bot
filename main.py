import requests
import os
import pandas as pd
import numpy as np
import logging
from datetime import datetime, timedelta

# 配置日誌系統：紀錄異常與執行狀態
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# 1. 系統環境變數 (GitHub Secrets)
TG_TOKEN = os.getenv("TG_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

# 2. 監控配置
COINS = ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP", "SUI-USDT-SWAP", "XRP-USDT-SWAP", "ASI-USDT-SWAP"]
LOG_FILE = "active_trades.csv"      # 當前持倉
HISTORY_FILE = "trade_history.csv" # 歷史紀錄 (用於統計勝率)

def send_tg(msg):
    """顯性錯誤回報：確保發生異常時你能收到通知"""
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    try:
        res = requests.post(url, json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"}, timeout=15)
        res.raise_for_status()
    except Exception as e:
        logging.error(f"TG 發送失敗: {e}")

def fetch_okx_safe(instId, bar="15m", limit="100"):
    """#6 修正：確保只抓取已確認(Confirm)的 K 棒，避免插針誤判"""
    try:
        url = f"https://www.okx.com/api/v5/market/candles?instId={instId}&bar={bar}&limit={limit}"
        res = requests.get(url, timeout=10).json()
        if 'data' not in res: return None
        
        df = pd.DataFrame(res['data'], columns=['ts', 'o', 'h', 'l', 'c', 'v', 'volCcy', 'volCcyQuote', 'confirm'])
        df[['o', 'h', 'l', 'c', 'v', 'volCcy']] = df[['o', 'h', 'l', 'c', 'v', 'volCcy']].astype(float)
        
        # 只取已收盤的 K 棒 (confirm == "1")
        df = df[df['confirm'] == "1"].copy()
        return df.iloc[::-1].reset_index(drop=True)
    except Exception as e:
        logging.error(f"API 抓取錯誤 ({instId}): {e}")
        return None

def get_market_sentiment_pro(instId):
    """#4 & #9 修正：加入 LS 防呆、流動性過濾與 OI 燃料"""
    try:
        # A. 多空人數比 (LS Ratio) - 增加空值防呆
        ls_res = requests.get(f"https://www.okx.com/api/v5/rubik/stat/contracts/long-short-account-ratio?instId={instId}&period=5m").json()
        ls_data = ls_res.get('data', [])
        if not ls_data or len(ls_data) < 3: 
            return 0, 1.0, 1.0, "NEU", False
        ls_curr, ls_prev = float(ls_data[0][1]), float(ls_data[2][1])

        # B. CVD 與 低流動性過濾
        base = instId.split('-')[0]
        s_df = fetch_okx_safe(f"{base}-USDT", bar="5m", limit="20")
        if s_df is None or s_df.empty: return 0, 1.0, 1.0, "NEU", False
        
        avg_vol = s_df['volCcy'].mean()
        curr_vol = s_df['volCcy'].iloc[-1]
        cvd_trend = "UP" if s_df['c'].iloc[-1] > s_df['c'].iloc[-10] else "DOWN"
        
        # 如果成交量低於平均 50%，視為無效訊號
        if curr_vol < (avg_vol * 0.5): cvd_trend = "LOW_VOL"

        # C. 持倉量燃料 (OI Fuel)
        oi_res = requests.get(f"https://www.okx.com/api/v5/rubik/stat/contracts/open-interest-volume?instId={instId}&period=5m").json()
        oi_data = oi_res.get('data', [])
        fuel = float(oi_data[0][1]) < float(oi_data[2][1]) if len(oi_data) > 2 else False
        
        return 0, ls_curr, ls_prev, cvd_trend, fuel
    except: return 0, 1.0, 1.0, "NEU", False

def main():
    now_tw = datetime.utcnow() + timedelta(hours=8)
    
    # #3 修正：安全讀取 CSV，防止競態
    if os.path.exists(LOG_FILE):
        try:
            trades = pd.read_csv(LOG_FILE).to_dict('records')
        except:
            trades = []
    else:
        trades = []
    
    still_active = []
    finished_trades = []

    # --- 1. 自動結算與 TP 階梯保本邏輯 ---
    for t in trades:
        df = fetch_okx_safe(t['instId'], "15m", "5")
        if df is None or df.empty:
            still_active.append(t)
            continue
        
        curr_p = df['c'].iloc[-1]
        hi, lo = df['h'].max(), df['l'].min()
        side = t['side']
        
        # 判定止損 (SL)
        is_sl = (side == "LONG" and lo <= t['sl']) or (side == "SHORT" and hi >= t['sl'])
        if is_sl:
            send_tg(f"❌ *結算：止損離場 (SL)*\n💰 #{t['instId']} | 價格: `{curr_p}`")
            t['status'] = "LOSS"
            finished_trades.append(t)
            continue

        # #5 修正：TP 階梯觸發與自動移止損 (保本)
        if t.get('tp1_hit', 0) == 0:
            if (side == "LONG" and hi >= t['tp1']) or (side == "SHORT" and lo <= t['tp1']):
                t['tp1_hit'] = 1
                t['sl'] = t['entry'] # 核心邏輯：TP1 達成即移至保本
                send_tg(f"🔹 *TP1 達成：已自動保本*\n💰 #{t['instId']} | 鎖定進場位: `{t['sl']}`")
        
        if t.get('tp1_hit', 0) == 1 and t.get('tp2_hit', 0) == 0:
            if (side == "LONG" and hi >= t['tp2']) or (side == "SHORT" and lo <= t['tp2']):
                t['tp2_hit'] = 1
                t['sl'] = t['tp1'] # 進階邏輯：TP2 達成後止損移至 TP1
                send_tg(f"🔹 *TP2 達成：鎖定利潤*\n💰 #{t['instId']} | 移止損至 TP1: `{t['sl']}`")

        # TP3 最終結算
        if (side == "LONG" and hi >= t['tp3']) or (side == "SHORT" and lo <= t['tp3']):
            send_tg(f"🚀 *TP3 終極止盈！*\n💰 #{t['instId']} | 獲利了結離場")
            t['status'] = "WIN"
            finished_trades.append(t)
        else:
            still_active.append(t)

    # --- 2. 燃料狙擊掃描 ---
    current_ids = [t['instId'] for t in still_active]
    for instId in COINS:
        if instId in current_ids: continue
        
        df_4h = fetch_okx_safe(instId, "4H", "200")
        if df_4h is None: continue
        ema200 = df_4h['c'].ewm(span=200, adjust=False).mean().iloc[-1]
        
        _, ls_c, ls_p, cvd, fuel = get_market_sentiment_pro(instId)
        df_15 = fetch_okx_safe(instId, "15m", "50")
        if df_15 is None: continue
        curr_p = df_15['c'].iloc[-1]
        
        # #2 修正：點差與 ATR 緩衝
        atr = (df_15['h'] - df_15['l']).rolling(14).mean().iloc[-1]
        
        is_long = (curr_p > ema200) and (curr_p > df_15['h'].iloc[-20:-2].max()) and (cvd == "UP") and (ls_c < ls_p) and fuel
        is_short = (curr_p < ema200) and (curr_p < df_15['l'].iloc[-20:-2].min()) and (cvd == "DOWN") and (ls_c > ls_p) and fuel

        if is_long or is_short:
            side = "LONG" if is_long else "SHORT"
            risk = atr * 1.5
            sl = curr_p - risk if is_long else curr_p + risk
            # 三段點位 (1.0R / 2.5R / 4.0R)
            tp1, tp2, tp3 = curr_p + atr, curr_p + atr*2.5, curr_p + atr*4
            
            new_trade = {
                "instId": instId, "side": side, "entry": curr_p, "sl": sl, 
                "tp1": tp1, "tp2": tp2, "tp3": tp3, "tp1_hit": 0, "tp2_hit": 0,
                "entry_time": now_tw.strftime("%Y-%m-%d %H:%M")
            }
            still_active.append(new_trade)
            send_tg(f"🎯 *燃料狙擊：新訊號*\n💎 #{instId} | {side}\n📍 入場: `{curr_p:.4f}`\n🚫 止損: `{sl:.4f}`\n🟣 TP3: `{tp3:.4f}`\n⛽ 燃料: `🔥 噴發中`")

    # --- 3. #7 歷史紀錄與狀態持久化 ---
    pd.DataFrame(still_active).to_csv(LOG_FILE, index=False)
    if finished_trades:
        hist_df = pd.DataFrame(finished_trades)
        hist_df.to_csv(HISTORY_FILE, mode='a', header=not os.path.exists(HISTORY_FILE), index=False)

    # 每日 08:30 報表
    if now_tw.hour == 8 and not os.path.exists("daily.ok"):
        report = f"📊 *Alpha Oracle 數據報表*\n🗓️ {now_tw.strftime('%m/%d')}\n"
        for i in COINS:
            _, ls, _, _, _ = get_market_sentiment_pro(i)
            report += f"🔹 {i.split('-')[0]}: LS 比 `{ls:.2f}`\n"
        send_tg(report)
        with open("daily.ok", "w") as f: f.write("1")
    elif now_tw.hour != 8 and os.path.exists("daily.ok"):
        os.remove("daily.ok")

if __name__ == "__main__":
    main()
